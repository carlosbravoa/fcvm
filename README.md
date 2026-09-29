# fcvm: disposable, isolated microVMs with container ergonomics, for people and agents

`fcvm` runs workloads in Firecracker microVMs, each with its own kernel, with
the convenience of a container tool: images, layers, volumes, ports, `exec`,
snapshots and fork. It's self-hosted, starts VMs without root, and puts a real
VM boundary around code you don't trust, with optional jailing and network
isolation on top. Use it from the CLI, a web console, or an MCP client.

Images come from anywhere: any OCI registry (Docker Hub, ghcr.io, quay.io,
...), a local `docker save`, a Dockerfile, a committed VM, or a full-OS base
built from scratch (Ubuntu 26.04 is the included example).

**Build it yourself**
- **Kernel**: fetches the newest kernel from kernel.org (stable by default),
  starts from `allnoconfig` and applies a small fragment
  (`kernel/microvm-x86_64.config`). The result is a ~27 MB monolithic `vmlinux`
  that boots in about 0.5 s.
- **Ubuntu 26.04 base image**: minbase + systemd + ssh, built rootless with
  `mmdebstrap`.
- **Container import**: pulls any image from Docker Hub (or ghcr.io, quay.io,
  ...) with digest verification and flattens its layers. A small static init
  (`init/fc-init.c`, booted from an initramfs) runs the image's entrypoint
  with its env, workdir and user.
- **Dockerfile builds** (`fcvm build`, a Dockerfile subset), with `RUN`
  steps executed in a VM.

**Run it like containers**
- Shared read-only images with a writable layer per VM, `commit` to new
  layers, named volumes and live host directories.
- Published ports, and `exec` with exit codes, timeouts and separate
  stdout/stderr.
- `cp`, and `--json` output.
- Snapshots of running VMs, forked into copies in ~150 ms.

**Isolation** (details: [docs/security.md](docs/security.md))
- **Jailed VMs**: Firecracker's jailer through a small root helper. Each VM
  gets its own uid, a chroot, cgroup limits and its own network namespace.
- **Network isolation**: VMs can't reach each other or host services,
  anti-spoofing pins every VM to its MAC and address, and guests get no
  IPv6.
- **Local-only ports**: published ports bind to `127.0.0.1` unless you ask
  otherwise.
- **Egress allowlists** (`--allow @pypi,github.com`) through a logging proxy,
  or no network at all.

**For agents and people**
- **MCP server** (`fcvm mcp`): sandboxes as tools. They're jailed by
  default, and the server can pin their network policy.
- **Web console** (`fcvm serve`): VMs, images, builds, stats, a browser
  terminal and files, local-only with a token.

Only `host-setup`, `net-up` and `jail-setup` need sudo; VMs themselves start
without it. Building images uses user namespaces and `mkfs.ext4 -d
<tarball>`, so file ownership is kept without root.

## Quick start

```sh
./fcvm host-setup          # apt packages, KVM access (sudo, once)
./fcvm net-up              # bridge + taps + NAT (sudo, again after every reboot)
./fcvm all                 # firecracker + kernel + init + Ubuntu base image

./fcvm create dev ubuntu-26.04
./fcvm start dev           # boots in the background
./fcvm shell dev           # root shell; `exit` leaves the VM running
./fcvm run ubuntu-26.04    # throwaway sandbox: shell, and `exit` deletes the VM

./fcvm import nginx:latest
./fcvm run nginx-latest -d -p 8080:80    # throwaway VM, deleted when stopped
curl http://localhost:8080/              # or the VM's own IP: http://172.30.0.10/
./fcvm run alpine-latest -- sh -c 'exit 3'; echo $?   # prints 3

# a sandbox for work: prepare once, commit, start copies from that state
./fcvm create box python-3.13-slim --idle -v cache:/root/.cache
./fcvm start box && ./fcvm cp ./myproject box:/work
./fcvm exec -w /work box pip install -r requirements.txt
./fcvm stop box && ./fcvm commit box myproject-env
./fcvm run myproject-env -- python /work/main.py
```

## Commands

| command | what it does |
|---|---|
| `host-setup` | installs build/runtime packages, checks `/dev/kvm` access |
| `jail-setup [--remove]` | installs (or removes) `fcvm-jaild`, the root helper that runs VMs under the Firecracker jailer (sudo) |
| `net-up` / `net-down` | bridges `fcbr0` (172.30.0.1/24, NAT) and `fcbr1` (172.30.1.1/24, restricted), 64 taps each, nftables rules, ufw rules |
| `firecracker` | downloads the latest Firecracker release into `bin/` (checksum-verified) |
| `kernel [stable\|mainline\|longterm\|X.Y.Z]` | builds `kernels/vmlinux-X.Y.Z`; `kernels/vmlinux` points at the newest |
| `init` | builds `build/fc-init` (static) and `build/initramfs.cpio`, which every VM boots with |
| `base [NAME]` | builds `images/ubuntu-26.04.ext4` |
| `import REF [NAME]` | registry image, or a local one (`docker-archive:F.tar`, `oci:DIR`, `oci-archive:F.tar`, or just a path) → `images/NAME.ext4` + `NAME.json` |
| `build [-t NAME] [-f FILE] [--build-arg K=V] [--no-cache] [--net none\|--allow H,...] [CONTEXT]` | builds an image from a Dockerfile subset, cached per step. The result is the FROM image plus one layer |
| `images [--all] [--json]`, `ls [--all] [--json]` | lists images (with what uses each) / VMs (state, exit code, memory used/allocated, disk use, network, ports, volumes). `--all` includes the build cache and build VMs |
| `inspect VM` | the VM's details as JSON |
| `create VM IMAGE [opts] [-- CMD...]` | VM on the shared image plus its own writable layer. `--vcpus N`, `--mem MiB`, `--disk SIZE` (layer size, default 8G sparse), `-p [BIND:]HOST:GUEST` (repeatable), `-v VOLUME:/PATH[:ro]` (named volume, created on first use) or `-v /HOST/DIR:/PATH[:ro]` (live host directory), repeatable, `--net none`, `--allow HOSTS`, `--idle` (container: run nothing, stay up for `exec`), `--copy` (private full copy instead), `-- CMD` (replaces the image's CMD and keeps its ENTRYPOINT, as `docker run` does), `--entrypoint CMD` (`""` clears it) |
| `start [-a] VM` | boots in the background, like `docker start`. `-a` attaches the console |
| `run IMAGE [-d] [opts] [-- CMD...]` | throwaway VM, deleted when it stops. **App images**: attached like `docker run`: you see the output, Ctrl-C goes to the app, Ctrl-] detaches, and fcvm exits with the container's exit code. **System images**: boots and opens `fcvm shell` (or runs CMD); when the shell or CMD ends, the VM is stopped and deleted. `-d`: background |
| `stop VM` | Ctrl-Alt-Del (graceful), killed after 20 s |
| `exec [-i] [-t] [-u USER] [-w DIR] [-e K=V]... [--timeout S] VM CMD...` | runs a command in a running VM, like `docker exec`. Exits with its status, or 124 on timeout |
| `cp [-L] SRC DST` | copies files or directories into or out of a running VM (`VM:PATH` on one side), like `docker cp` |
| `commit VM IMAGE` | saves a stopped VM's changes as a new image, a read-only layer on its image |
| `rmi IMAGE` | deletes an image nothing depends on |
| `squash IMAGE NEW` | merges IMAGE's committed layers into one, on the same base image |
| `prune` | deletes the build cache and leftover build VMs |
| `mount VM /HOST/DIR:/PATH[:ro]`, `umount VM /PATH` | adds or removes a live host directory: immediately on a running VM, from the next start on a stopped one |
| `snapshot VM NAME` | saves a running VM's memory, device state and disk. The VM keeps running (paused ~0.75 s per GB of RAM) |
| `snapshot ls [--json]`, `snapshot rm NAME` | lists / deletes snapshots |
| `fork SNAPSHOT [NAME] [-n N]` | starts running VM(s) from a snapshot in ~120 ms each, with their own disk, IP, MAC and hostname |
| `volume create NAME [SIZE]`, `volume ls [--json]`, `volume rm NAME` | named volumes: persistent ext4 disks attached with `-v` |
| `egress VM [--allow H,...] [--deny H,...] [-n N \| -f]` | a restricted VM's allowlist and its allowed/denied requests. Changes apply live |
| `mcp` | MCP server on stdio, for agents |
| `serve [--port 8686]` | web console on `http://127.0.0.1:8686`, local only. Prints a login URL |
| `shell [-u USER] VM` | interactive shell in a running VM (bash, else sh). Same as `exec -it VM` |
| `console VM` | attaches to the live serial console. Ctrl-] detaches and the VM keeps running |
| `logs [-f] VM` | console output of the current or last boot |
| `ssh VM`, `rm VM` | connects with ssh, deletes |

Settings (kernel channel, Ubuntu suite, packages, subnet, default vCPU/memory,
extra kernel args) live at the top of `lib/common.sh`. Override them in
`fcvm.conf` or the environment, e.g. `KERNEL_CHANNEL=longterm ./fcvm kernel`
or `VM_KERNEL_ARGS=loglevel=7 ./fcvm run alpine-latest`.

## Image types: app and system

Every fcvm image, of either type, boots as a full Firecracker microVM under
KVM: its own kernel, virtual CPUs, memory, disks and network card. No Docker,
containerd or runc is involved. The type only says what runs as PID 1:

| | **app** | **system** |
|---|---|---|
| comes from | container images (Docker Hub, registries, `docker save`), and builds and commits on them | the Ubuntu base (`fcvm base`), and builds and commits on it |
| PID 1 | `fc-init` runs the image's ENTRYPOINT + CMD as its user, with its env and workdir, like `docker run` | `fc-init` sets up the root and hands PID 1 to systemd: journald, udev, dbus, sshd, a login prompt |
| lifetime | as long as that process; its exit code is fcvm's | until stopped or shut down from inside |
| boot / memory | ~0.5 s, tens of MB | ~2.3 s, more |
| `run IMAGE` | attached to the app's output | opens a shell; `exit` deletes the VM |
| `-- CMD`, `--idle`, `--entrypoint` | yes | no |
| good for | apps, one-off commands, agent sandboxes | long-lived dev machines, services, cron |

Compared with Docker running the same image, an app VM has the same
filesystem, command, env and user. The difference is isolation: Docker shares
the host kernel (namespaces, cgroups), while an app VM has its own guest
kernel behind hardware virtualization. It costs about 0.5 s of boot and a few
tens of MB, which is why this suits running untrusted code. Images made before
the rename (`container`, `systemd`) are migrated automatically.

**Kernel.** Every VM, app or system, runs the one guest kernel built by
`fcvm kernel` (`kernels/vmlinux`, the newest build; 7.2.8 at the time of
writing), each VM its own instance of it. Images contain no kernel, not even
the Ubuntu base, and whatever kernel an image was built for doesn't matter,
as with Docker. So `uname -r` shows the fcvm kernel in every VM, and
upgrading means `fcvm kernel`, with no image rebuilds, taking effect at each
VM's next boot. The kernel is monolithic with no loadable modules, so
`modprobe` does nothing: features come from `kernel/microvm-x86_64.config`
(virtio, ext4, overlayfs, cgroups v2, namespaces, nftables, ...). Add to that
fragment, rebuild, and restart VMs to get more.

**Where the kernel comes from.** You build it, on your machine, with
`fcvm kernel` (`lib/build-kernel.sh`). It isn't Ubuntu's kernel or one of
Firecracker's prebuilt CI kernels:

1. Picks the newest release of `KERNEL_CHANNEL` (`stable` by default, or
   `longterm`, `mainline`, or an exact version such as `fcvm kernel 6.18.54`)
   from `https://www.kernel.org/releases.json`.
2. Downloads the official source tarball from `cdn.kernel.org` into `cache/`.
3. Checks its SHA-256 against kernel.org's `sha256sums.asc`. That catches
   corruption, but it isn't a proof of origin, because the PGP signature isn't
   verified yet (roadmap E3).
4. Extracts it to `build/linux-X.Y.Z/`, runs `make allnoconfig`, applies
   `kernel/microvm-x86_64.config`, and reports any requested option that
   didn't make it into the final `.config`.
5. Compiles `vmlinux` with the host's gcc, in about 2.5 minutes on 12 cores.
6. Installs it:

```
kernels/
  vmlinux -> vmlinux-7.2.8     # what every VM boots (the newest build)
  vmlinux-7.2.8                # ~27 MB uncompressed ELF
  config-7.2.8                 # the exact .config it was built with
```

`kernels/`, `build/` and `cache/` are gitignored. The repository holds the
recipe (script and config fragment), not the binary, so each machine builds
its own. Earlier builds stay in `kernels/`. To roll back, point the symlink
at one of them (`ln -sfn vmlinux-OLD kernels/vmlinux`) and restart the VMs.
`FORCE=1 fcvm kernel` rebuilds a version that is already built, for example
after editing the config fragment.

## How it fits together

```
kernel.org ──► build-kernel.sh ──► kernels/vmlinux ─┐
Ubuntu archive ─► mmdebstrap ─► tar ─► mkfs.ext4 -d ─► images/ubuntu-26.04.ext4 ─┐
registry ─► oci_import.py ─► flattened tar + /.fcvm ─► mkfs.ext4 -d ─► images/*.ext4 ─┤
                                                                         vm.sh create/start
                                                  vms/<vm>/{disk.ext4, fc.json} ─► firecracker
```

**Boot and root filesystem.** Every VM boots the same kernel with
`build/initramfs.cpio`, which holds only `fc-init` as `/init`. Images carry
no init or agent, so upgrading `fc-init` never needs an image rebuild.
`fc-init` assembles the root from drives named on the kernel command line,
then switches into it (the same moves as `switch_root`). After that it either
runs the app image's command or, for system images (`fcvm.exec=/sbin/init`),
execs systemd as PID 1. Drives are attached in order, `vda`, `vdb`, ...:

| drive | what | argument |
|---|---|---|
| base image | read-only, shared by all VMs | `fcvm.root=` |
| writable layer | `vms/<vm>/rw.ext4`: sparse ext4 holding overlayfs `upper/` and `work/` | `fcvm.rw=` |
| committed layers | read-only, topmost first | `fcvm.layers=` |
| volumes | `volumes/<name>.ext4` | `fcvm.vols=DEV:PATH[:ro]` |

`create` takes about 0.1 s and 6 MB. `import`, `base` and `rmi` refuse to
touch an image that VMs or other images still use. `--copy` gives a VM a
private full copy of the image instead, with no layers.

**Commit and layers.** `fcvm commit VM IMAGE` copies the stopped VM's
writable layer (sparse, typically a few MB) and registers it as a new image
whose parent is the VM's image. A VM from that image stacks
`lowerdir=layer:...:base` under its own writable layer, which is the Docker
model: deletions are overlayfs whiteouts and keep working across layers.
Commit is rootless and instant because nothing is merged. Per-VM settings
aren't saved: a command given with `create -- CMD` or `--idle` stays with the
VM, and the new image keeps its parent's command. A `--copy` VM's disk becomes
a standalone image instead.

**Squash.** Every layer is a separate virtual disk, and Firecracker on x86
has about 19 device slots in total, shared by disks, network, vsock and
entropy. So layer chains have to stay short. `fcvm squash` merges layers in
a throwaway VM that boots only the initramfs: `fc-init` merge mode, with the
layer disks attached read-only and an empty output disk. It applies each
layer's `upper/` in order and keeps whiteouts and opaque directories, so
deletions of files in the base image survive. Owners, modes, setuid bits,
xattrs (such as file capabilities), hardlinks, symlinks, device nodes and
timestamps are preserved. A three-layer chain merges in about a second.

**Identity scrubbing.** Committing a VM that has booted systemd removes its
machine-id, SSH host keys, random seed and journal from the layer. The base
image's blank versions show through again, and every VM from the new image
generates its own at first boot.

**Volumes.** Named ext4 disks in `volumes/`, attached with `-v NAME:/PATH`
and created on first use (`VOLUME_SIZE`, default 10G sparse). They outlive
VMs. A new, empty volume takes the owner and mode of the directory it covers,
as Docker volumes do, so non-root images can write to it. A read-write volume
can be attached to only one running VM at a time; `:ro` volumes can be
shared.

**Host directories.** `-v /host/dir:/path[:ro]` (any value starting with
`/`, `./`, `../` or `~`) mounts a host directory live, both ways, like a bind
mount:

```sh
./fcvm run python-3.13-slim -v ./myproject:/work -- python /work/main.py
./fcvm create dev2 ubuntu-26.04 -v ~/src:/src && ./fcvm start dev2   # edit on the host, run in the VM
```

Firecracker has no virtio-fs or 9p device, so fcvm builds the mount from
parts the kernel already has:
1. At boot, `fc-init` opens a vsock connection to the host.
2. Firecracker hands that connection to `vms/<vm>/vsock.sock_<port>`, where
   `lib/share9p.py` (a small 9P2000.L server, standard library only, one per
   VM, running as you) serves the directory.
3. `fc-init` gives the connection to the guest kernel's own 9P client
   (`mount -t 9p -o trans=fd`).

There's no FUSE and no extra binary in the image, and it works with
`--net none`. Behaviour:
- **Ownership:** files appear owned by the image's user (root for system
  images), so non-root images can write. On the host they belong to you, and
  `chown` in the guest is accepted and ignored.
- **Confinement:** the server keeps every operation inside the shared
  directory. Symlinks are resolved by the guest, so a link to `/etc` points
  at the guest's `/etc`, not the host's.
- **Changes:** made on either side, they're visible on the other at once;
  there's no cache to go stale. git, editors and servers work (tested:
  `git init/add/commit`, nginx serving files edited on the host).
- **Speed:** ~280 MB/s write and ~350 MB/s read for large files, and about
  1 ms per small-file operation. That's fine for source trees. Keep huge
  many-file trees such as `node_modules` or virtualenvs on the VM's own disk
  or a named volume.
- **Snapshots:** VMs with host directories can't be snapshotted, because the
  live connection can't be cloned into a fork.

Host directories can also be added to or removed from a **running** VM:
`fcvm mount VM /host/dir:/path[:ro]` starts a server for the new directory
and asks the agent to mount it, and `fcvm umount VM /path` reverses it. Both
are saved to the VM's configuration, so they apply at the next start too.
The web console's Files tab does the same.

`fcvm cp` remains the way to copy things in or out of a running VM without
a mount.

**Exit codes.** When the container's main process exits, `fc-init` writes its
status (exit code, or 128+signal, as Docker does) to `/.fcvm/exit-status`.
That file lands on the VM's writable layer. After Firecracker exits, the host
reads it back with `debugfs` without mounting anything. `fcvm start`/`run`
exit with that code, and `fcvm ls` shows `exited(N)`.

**Published ports.** `-p` is handled by `lib/portfwd.py`, a small asyncio TCP
relay that runs as your user. It listens on the host port and forwards to the
VM's IP. It watches the Firecracker PID and exits with the VM. A port that is
already taken stops the VM from starting. The guest sees connections coming
from the bridge (172.30.0.1), not from the real client. With ufw active,
allowing a published port from the LAN still needs `sudo ufw allow 8080/tcp`.

**Consoles.** Firecracker's serial port is its stdin/stdout. `fcvm start`
runs it under `lib/console.py`, a small relay that owns the PTY. The relay
writes everything to `vms/<vm>/console.log`, keeps a scrollback, and serves
clients on `vms/<vm>/console.sock`, so consoles attach and detach (Ctrl-])
without affecting the VM. When Firecracker exits, the relay runs
`fcvm _reap <vm>`, which records the container's exit code for a waiting
`run`, stops the port forwarder, and deletes throwaway VMs. System images
have no serial autologin: the console shows boot messages and a `login:`
prompt, and you get in with `fcvm shell` or `fcvm ssh`.

**exec / shell.** Every VM has a vsock device, and `fc-init` runs an exec
agent on vsock port 1024. In app VMs it is forked by PID 1. In system
images it runs as `fcvm-agent.service` (`/.fcvm/bin/fc-init --agent`, a copy
`fc-init` puts on a tmpfs at boot). The host side
(`lib/exec_client.py`) connects through Firecracker's vsock Unix socket
(`vms/<vm>/vsock.sock`, `CONNECT 1024`). Each connection runs one command, as
the image's user with its env and workdir, as `docker exec` does. It runs on
a PTY with raw mode and window-size updates (`-t`), or on pipes with separate
stdout/stderr (no `-t`), and returns the exit code. No network or sshd is
needed, so it works for any image, including distroless ones (as long as the
command exists). `-w` and `-e` set the working directory and extra
environment. `--timeout` drops the connection, and the agent then kills the
command's process group, exiting 124. The request carries a protocol version,
so a mismatch between host and VM fails clearly.

`-u USER` works as in `docker exec -u`: `name`, `uid`, `name:group` or
`uid:gid`. It is resolved inside the guest from its own `/etc/passwd` and
`/etc/group`, so users created in the VM after import work too, along with
their supplementary groups. An unknown name fails with Docker's message and
exit code 126. `HOME` comes from the user's passwd entry. The working
directory is the image's `WORKDIR`, or the user's home when there is none
(system images). The session's PTY is handed to the user, as `login` does,
so `sudo`, `less` and the like can open `/dev/tty`.

```sh
./fcvm shell web                       # root@nginx:/#
./fcvm shell -u root unpriv            # root shell in an image whose USER is non-root
./fcvm exec -u postgres db psql        # as another user
./fcvm exec web nginx -t               # one-off command
./fcvm cp ./site web:/usr/share/nginx/html   # tar over exec; needs sh and tar in the image
```

**App VMs.** The importer keeps the image config (Entrypoint, Cmd, Env,
WorkingDir, User) in `/.fcvm/`. `fc-init` mounts `/proc`, `/sys`, `/dev`, devpts, cgroup2 and friends, writes
`/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf`, drops to the image
user, and execs the entrypoint. It forwards signals and reaps zombies. When
the main process exits, it reboots the guest, and with `reboot=k` Firecracker
then exits. `fcvm stop` sends Ctrl-Alt-Del, which `fc-init` turns into SIGTERM
for the app.

**Networking.** The kernel configures `eth0` from the `ip=` boot argument,
so no DHCP or network manager runs in the guest. The Ubuntu image links
`/etc/resolv.conf` to `/proc/net/pnp`. `fcvm start` takes the first free tap
of the VM's pool and holds a lock on it for the life of the VM. Each VM gets
one of three network modes at `create`/`run`:

| mode | option | what the VM can reach |
|---|---|---|
| full | (default) | everything, through NAT on `fcbr0` (`172.30.0.(10+i)`). DNS is the host's upstream resolvers (`NET_DNS=auto`) |
| none | `--net none` | nothing: no network card. Needs no tap |
| restricted | `--allow HOST,*.DOMAIN,HOST:PORT,@PRESET` | only the egress proxy on `fcbr1` (`172.30.1.(10+i)`) |

**Egress control (restricted mode).** `fcbr1` has no NAT and no forwarding.
nftables lets its VMs reach only `172.30.1.1:3128`, and its taps are
bridge-isolated, so restricted VMs can't see each other either. On that port,
`lib/egress_proxy.py` (one per user, started on demand, rootless) accepts
`CONNECT` for HTTPS and absolute-URL requests for plain HTTP. It identifies
the VM by source address and checks the host name against the VM's
allowlist: `vms/.egress/<ip>.json`, re-read on every request. `fc-init`
exports `http(s)_proxy` to the container's command and every exec session,
and system images get them through `systemd.setenv=`. So pip, npm, apt,
apk, git, curl and Go work unchanged. Names are resolved on the host, so
split-DNS and VPN names work too. Every decision goes to
`vms/<vm>/egress.log`. A denied plain-HTTP request gets a 403 that names the
command to allow it; for HTTPS, clients just report the refused tunnel.

```sh
./fcvm run python-3.13-slim --allow @pypi -- pip install requests       # works
./fcvm create box alpine-latest --idle --allow @alpine,github.com        # restricted VM
./fcvm create box alpine-latest --net none                                # no network at all
./fcvm egress box                     # allowlist + recent ALLOW/DENY log
./fcvm egress box --allow example.com # live change, no restart
```

A request outside the allowlist fails with a 403 from the proxy, for example
`urlopen error Tunnel connection failed: 403 Forbidden` from Python, and
shows up as `DENY` in `fcvm egress`.

Presets live in `lib/egress-presets.conf` (`@pypi`, `@npm`, `@github`,
`@golang`, `@crates`, `@ubuntu`, `@debian`, `@alpine`, ...); add your own
mirrors there. Only HTTP(S) and proxy-aware traffic can be allowed. Anything
else (raw TCP, UDP, ICMP, direct DNS) is refused, which is the point.
Wildcards follow the usual rule: `*.github.com` doesn't match `github.com`
itself.

**Isolation** (set up by `fcvm net-up`; the full story, with how to verify
it, is in [docs/security.md](docs/security.md#network-isolation)):
- VMs can't reach each other, neither across the bridge (isolated ports) nor
  routed through the host. `NET_ISOLATE=0` lets VMs on `fcbr0` talk.
- VMs can't reach services on the host (`172.30.0.1`, or any other host
  address): only replies and ping are let in. `NET_HOST_ACCESS=1` opens them.
- Anti-spoofing: each tap (and each jailed VM's veth) passes only IPv4 and ARP
  from its own MAC and address (`nft list table bridge fcvm`).
- No IPv6 for guests: it's disabled on the bridges and taps and dropped at
  the ports.
- Published ports (`-p 8080:80`) listen on `127.0.0.1`. Use
  `-p 0.0.0.0:8080:80` to publish on every interface.

**Kernel config notes** (learned the hard way on 7.2 + Firecracker 1.17):
- `CONFIG_PCI=y` is required even for virtio-mmio guests. Firecracker's ACPI
  DSDT describes a PCI root bridge, and without PCI support the DSDT fails to
  load, device IRQs aren't set up, and virtio probes fail with -22.
  One boot message is expected and harmless: `PCI: Fatal: No config space
  access function found`. It is a side effect of `pci=off`.
- `CONFIG_VIRTIO_MMIO_CMDLINE_DEVICES` is off. Devices are found through ACPI,
  and the `virtio_mmio.device=` args Firecracker still passes would register
  every device twice.
- `SERIAL_8250_NR_UARTS=1`: Firecracker has a single UART.
- The build prints any fragment option that didn't make it into the final
  `.config` (renamed symbol or unmet dependency). Check this after moving to a
  new kernel series.

## Jailed VMs

A summary. [docs/security.md](docs/security.md#jailed-vms-in-detail) covers
the helper, what it validates, cleanup and troubleshooting in full.

```sh
./fcvm jail-setup                            # once, with sudo; re-run after updating fcvm or `fcvm firecracker`
./fcvm create box alpine-latest --idle --jail   # or JAIL=1 for every new VM; the web console has a checkbox
./fcvm start box && ./fcvm exec box id
```

Without `--jail`, Firecracker runs as you. KVM and Firecracker's seccomp
filters are then the only barrier between a guest and your files. With
`--jail`, each VM runs under Firecracker's own jailer, the production setup
AWS uses:

| | rootless (default) | jailed |
|---|---|---|
| Firecracker runs as | you | its own uid/gid (900000 + slot), `NoNewPrivs`, seccomp |
| can see | everything you can | its chroot `/srv/jailer/firecracker/<id>/root`: its disks, `/dev/kvm`, `/dev/net/tun`, its sockets |
| limits | none | cgroup v2: `cpu.max` = vCPUs, `memory.max` = memory + 256 MiB VMM overhead, `pids.max` = 128, `no-file` = 4096 |
| network | pool tap on the bridge, owned by you | own network namespace: `tap0` (owned by the VM's uid) and a veth pair whose host end, `fcv<i>` / `fcrv<i>`, joins the bridge |

How it works:
- **The helper.** The jailer has to start as root, so `fcvm jail-setup`
  installs `fcvm-jaild`, a small systemd service. It listens on
  `/run/fcvm/jaild.sock`, and only your uid may connect (socket mode 0600 plus
  a peer-credential check).
- **Validation.** A launch request names the VM's files, and the helper
  re-validates all of them. Each must resolve inside your fcvm tree
  (`images/`, `vms/<vm>/`, `volumes/`, `kernels/`, `build/`) and be a
  regular file you own. Only the VM's own disk and volumes can be writable.
- **Root-owned binaries.** The helper runs root-owned copies of itself,
  `jailer` and `firecracker` from `/usr/local/lib/fcvm`, never the
  user-writable files in this tree. `start` warns if the installed
  Firecracker differs from `bin/`.
- **Disks.** They're bind-mounted into the chroot, images read-only. The
  fcvm tree may be on a `nodev` filesystem, where the jailer's device nodes
  wouldn't work, so hard links can't be used. The helper's private mount
  namespace keeps these mounts out of the host's mount table.
- **Access by ACL.** The VM's uid gets its writable files through ACLs, and
  you keep ownership, so exit codes, `commit` and `cp` work as before. A
  default ACL on the chroot, plus mask fixes after the jailer's `chmod` and
  Firecracker's socket creation, lets both you and the VM use the API,
  vsock and host-directory sockets. `vms/<vm>/fc.sock` and `vsock.sock`
  link into the chroot, so every command works unchanged: exec, shell,
  console, stop, ports, egress, host directories (live mounts too), the
  web console.
- **Network namespace.** The jailer enters `fcvm-<id>`, which holds only
  `lo`, `tap0`, `veth0` and a bridge. The VMM can't see or touch the host's
  interfaces, and the veth's host end gets the same isolation and
  anti-spoofing rules as a tap.
- **Snapshots and fork** work as for rootless VMs. Firecracker writes the
  snapshot inside its chroot, and the helper moves it into `snapshots/`,
  owned by you. A fork is a fresh jail whose paths (`/drive0.ext4`,
  `/vsock.sock`, `tap0`) are the same as the source's, so the snapshot
  loads without overrides. A snapshot of a jailed VM forks jailed.
- **Stats.** The web console reads a jailed VM's disk I/O from its cgroup's
  `io.stat` (`/proc/<pid>/io` of another uid isn't readable) and its traffic
  from the veth.
- **Cleanup.** When Firecracker exits, the helper unmounts, drops the ACLs,
  deletes the network namespace (tap and veth go with it) and deletes the
  chroot. Stopping or restarting the service stops its jailed VMs; on
  start, it sweeps whatever a previous run (or a crash) left behind.
- **Defaults.** Sandboxes the MCP server creates are jailed whenever
  fcvm-jaild is installed (`FCVM_MCP_JAIL=0` opts out). The web console's
  create form has the box checked. The CLI stays opt-in (`--jail` / `JAIL=1`),
  because jailing needs the root helper.

No PID namespace (`--new-pid-ns`): Firecracker would run as pid 1 of a
namespace the helper can only reach through the jailer's fork, so fcvm would
lose the real pid it uses for liveness, stats and `stop`. The jail already
runs as its own uid, so the VMM can't signal or ptrace your processes or
other VMs, and `/proc` isn't mounted in the chroot. A PID namespace would add
little on top of that.

## Snapshots and fork

`fcvm snapshot` captures a running VM whole: its memory (processes, page
cache, anything in RAM), device state and writable disk, all taken while it
is paused, so they are consistent. `fcvm fork` starts new VMs from that
exact moment. Processes keep running from where they were, and warm caches
and loaded dependencies stay warm.

```sh
./fcvm create box python-3.13-slim --idle && ./fcvm start box
./fcvm exec box sh -c 'pip install -q numpy pandas && python -c "import pandas"'   # prepare once
./fcvm snapshot box ready          # box keeps running
./fcvm fork ready try -n 3         # try-1, try-2, try-3: running in ~0.6 s total
./fcvm exec try-2 python -c 'import pandas; print(pandas.__version__)'
./fcvm rm try-2 && ./fcvm fork ready try-2   # roll back: a fresh copy of the prepared state
```

What each fork gets:
- **Its own disk:** a copy of the snapshot's writable layer. Images stay
  shared and read-only.
- **Network:** its own tap, IP and MAC in the snapshot's network mode (full,
  restricted with the same allowlist, or none). Published ports aren't
  carried over, since they would conflict with the source.
- **Hostname:** its own, set after restore by the exec agent over vsock,
  with plain ioctls, so it works in any image.
- **Clock:** Firecracker's `clock_realtime` advances the guest clock by the
  time since the snapshot.
- **Randomness:** the kernel is built with VMGenID. Firecracker bumps the
  generation ID on restore and the guest kernel reseeds its RNG, so forks
  don't share random state. User-space programs that keep their own random
  state in memory (a process-local PRNG seeded before the snapshot) will
  still repeat it, which is inherent to cloning a running process.
- **Memory:** loaded on demand from the snapshot's memory file and shared
  copy-on-write between forks. A fork starts at about 20 MB of host RAM.

Numbers on this machine (1 GiB VM): the snapshot pauses the source for about
0.75 s, most of it writing the memory file. The memory file is then made
sparse (1 GiB → ~50 MB on disk for an idle VM). A fork takes about 120 ms
including re-addressing.

**Limits:**
- VMs with read-write volumes can't be snapshotted, because two forks can't
  share a writable volume. Use `:ro` volumes, or copy data in.
- A snapshot is tied to the Firecracker version and CPU model it was taken
  on. After `fcvm firecracker` upgrades, old snapshots may not load; fork
  says so.
- A stopped fork is an ordinary VM: `fcvm start` cold-boots it from its own
  disk. The hostname set at fork time is runtime state, so an app VM comes
  back with its image's hostname, as any VM created from that image would.
- A snapshot keeps its images in use (`rmi` refuses), and deleting it
  doesn't affect running forks. Only VMs started with this version of
  fcvm's initramfs can be re-addressed; older ones get a warning.

## Building images

`fcvm build` takes a Dockerfile subset, from `Fcvmfile` or `Dockerfile` in
the build context, or `-f`:

```dockerfile
ARG PYVER=3.13
FROM python:${PYVER}-slim          # an fcvm image, or a registry ref (imported if missing)
ENV APP_HOME=/app PIP_ROOT_USER_ACTION=ignore
WORKDIR $APP_HOME
COPY requirements.txt .
RUN pip install -q -r requirements.txt
COPY src/ ./src/
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 8000
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--greeting", "hello"]
```

```sh
./fcvm build -t myapp --allow @pypi ./myapp   # RUN steps get only PyPI
./fcvm run myapp                               # python -m src.main --greeting hello
./fcvm run myapp -- --greeting hi              # replaces CMD, keeps ENTRYPOINT
```

- **Supported:** `FROM` (a single stage), `RUN` (shell and JSON forms),
  `COPY`/`ADD` (local sources and globs, `--chown`, `.dockerignore`, and
  `ADD` of a local tar extracts it), `ENV`, `ARG`/`--build-arg`, `WORKDIR`,
  `USER`, `CMD`, `ENTRYPOINT`, `EXPOSE`, `LABEL` (ignored). Variables are
  substituted as Docker does.
- **Not supported:** multi-stage builds, `COPY --from`, `ADD <url>`,
  `HEALTHCHECK`, `SHELL`, `ONBUILD`, `FROM scratch`. These fail with a clear
  error instead of being ignored. `COPY` needs `sh` and `tar` in the image.
- **How it runs:** each filesystem step (`RUN`, `COPY`, `ADD`, `WORKDIR`)
  runs in a VM booted from the previous step's result, which is then stopped
  and committed as a hidden cache image, a fresh environment per step as with
  Docker. The cache key chains the previous key, the instruction, the config
  (ENV, USER, ...) and, for `COPY`/`ADD`, the copied files' content. An
  unchanged prefix of the file is reused: editing application code after a
  `pip install` re-runs only the steps from the `COPY` on. Cache chains are
  squashed past six layers. The result is always the FROM image plus one
  squashed layer, with ENV, WORKDIR, USER, ENTRYPOINT and CMD written to its
  `/.fcvm` config. `USER` names are resolved at boot against the image's
  `/etc/passwd`.
- **Speed:** a typical first build is dominated by its `RUN` steps plus about
  1.5 s per filesystem step. A rebuild with nothing changed takes about 2 s.
  `--no-cache` runs everything in one VM and commits once.
- **Build network:** full by default. `--net none` and `--allow` work as for
  VMs. The cache lives under `images/_bc-*` (`fcvm images --all`), and
  `fcvm prune` removes it. Built images don't depend on it.
- **Building FROM the Ubuntu image** works too: `RUN` steps go through the
  systemd VM's agent, and `CMD`/`ENTRYPOINT` are refused because systemd is
  the init.

**Local images.** `fcvm import` also takes `docker save` / `podman save`
tarballs (classic and Docker 25+ OCI-style), OCI layout directories and OCI
archives, so images never have to be pushed to a registry:

```sh
docker save myorg/tool:1.0 -o tool.tar && ./fcvm import tool.tar   # -> image tool-1.0
./fcvm import oci:./layout:v2 mytool                               # a tag from an OCI layout
```

## Web console

```sh
./fcvm serve            # prints http://127.0.0.1:8686/?token=...  (open it in a browser)
```

A cloud-console-like UI for this host:
- **Dashboard:** host CPU, memory and disk; VM memory actually in use vs
  allocated; network slots; running instances with their CPU and memory.
- **Instances:** start, stop, restart, delete, snapshot, commit to image.
  Each instance page has live charts (CPU, memory, disk I/O, network; the
  last 10 minutes, sampled every 2 s) with a table view, details, logs, and
  the egress policy with its allow/deny log.
- **Browser terminals:** a **Shell** tab (an interactive shell through the
  exec agent, optionally as another user) and a **Console** tab (the live
  serial console), using xterm.js.
- **Launch:** name, image, vCPU and memory; network (full, an allowlist
  built from presets and extra hosts, or none); published ports; volumes and
  host directories; for app images, run the image's command, stay idle for
  the shell, or run a custom command.
- **Images:** import from a registry or a local archive (runs as a
  background job with progress), launch from an image, delete.
- **Files** (instance tab): browse the VM's filesystem, upload files (with
  progress), download, edit text files in place, create folders, rename,
  delete. It also manages host directories: add or remove them, live on a
  running VM. File operations are native to the exec agent (list, stat,
  read, write, mkdir, remove, rename), so they work in any image, even
  distroless ones. Uploads and edits are written atomically; edited files
  keep their owner and mode, and new ones take their folder's owner.
- **Builds:** projects under `builds/<name>/` (from a Python, Alpine, Ubuntu
  or blank template), or an existing host directory. Edit the Dockerfile and
  files in the browser, upload files or whole folders, then build with an
  image name, build args, network (full, allowlist or none) and no-cache.
  The log streams live, next to a step list showing which steps ran and
  which came from cache. "Launch it" opens the launch dialog on the result.
- **Snapshots:** fork, delete. **Volumes:** list, delete.

How it's built: `lib/web/server.py` is standard-library Python. It
implements HTTP/1.1 and WebSockets (RFC 6455) on asyncio, and every change
goes through the fcvm CLI, like the MCP server, so the UI and the command
line always agree. Stats come straight from `/proc` (the Firecracker
process's CPU time, resident memory and I/O) and from the tap devices'
counters. The frontend in `lib/web/static/` is plain HTML, CSS and JS with no
build step. xterm.js is vendored (MIT, see `vendor/LICENSE.xterm`), so it
works offline. Light and dark themes follow the OS, with a toggle.

**Security (local only for now):**
- It binds 127.0.0.1 only.
- The URL printed at start carries a random token, exchanged for an
  HttpOnly, SameSite=Strict cookie. A new token is made on every start.
- Requests must carry a localhost `Host` header, which blocks DNS
  rebinding. Changes and WebSockets need a same-origin `Origin`, which
  blocks cross-site requests.
- Static files can't escape `static/`, and a strict Content-Security-Policy
  allows only the app's own scripts.
- Treat the URL like a password: the browser shell is a root shell in your
  VMs. Remote access with real authentication is roadmap item W2.

## Agents (MCP)

`fcvm mcp` is an MCP server on stdio (standard library only). It wraps the
CLI, so everything behaves as it does on the command line. To register it
with Claude Code:

```sh
claude mcp add fcvm -- /path/to/fcvm mcp
```

| tool | does |
|---|---|
| `images`, `pull_image` | list images; import from Docker Hub or any registry |
| `create_sandbox` | create and boot a VM. App images stay idle for `exec` unless given a command |
| `exec` | run a shell command (`command`) or exact `argv`, with `workdir`, `env`, `user`, `stdin` and `timeout` (default 300 s). Returns `exit_code`, `stdout`, `stderr` |
| `write_file`, `read_file` | text files in the VM |
| `copy_to_vm`, `copy_from_vm` | host files and directories in or out (`fcvm cp`) |
| `commit_vm` | save a VM's state as an image; new sandboxes start from it |
| `build_image` | build an image from a Dockerfile in a host directory (cached per step) |
| `snapshot_vm`, `fork`, `snapshots`, `remove_snapshot` | snapshot a prepared sandbox and fork running copies in ~0.1 s, for parallel attempts or rollback |
| `egress_log` | a sandbox's network policy and its allowed/denied requests |
| `logs`, `list_vms`, `start_vm`, `stop_vm`, `remove_vm`, `volumes` | lifecycle and state |

`create_sandbox` takes `network`: `"full"`, `"none"`, or an allowlist such
as `["@pypi", "github.com"]`. To decide the policy for agents yourself, pin
it when registering the server. Agents can then only narrow it to `"none"`,
and no tool widens an allowlist:

```sh
claude mcp add fcvm -e FCVM_MCP_NETWORK=@pypi,@github -- /path/to/fcvm mcp
```

Output is capped at 20,000 characters per stream (head and tail kept) so a
noisy command can't flood the agent's context. Errors from fcvm itself (no
such VM, VM not running) come back as tool errors, not as a command's exit
code. A typical loop takes about 1.5 s to create a sandbox and under 0.5 s
per `exec`. `commit_vm` after installing dependencies makes later sandboxes
start ready.

## Layout

```
fcvm                    CLI entry point
lib/common.sh           settings + helpers
lib/build-kernel.sh     kernel.org → vmlinux
lib/build-base.sh       Ubuntu 26.04 base image
lib/oci_import.py       registry client + layer flattening (stdlib only)
lib/import.sh           import wrapper (sizes and creates the ext4)
lib/vm.sh               VM lifecycle
lib/portfwd.py          rootless TCP port publishing
lib/egress_proxy.py     egress proxy for restricted VMs (allowlists, logging)
lib/share9p.py          9P server for live host directories (-v /host:/path)
lib/jaild.py            fcvm-jaild, the root helper for jailed VMs (installed by jail-setup)
lib/jail-setup.sh       installs/removes fcvm-jaild (sudo)
lib/egress-presets.conf allowlist presets (@pypi, @npm, ...)
lib/exec_client.py      host side of fcvm exec / shell (vsock)
lib/mcp_server.py       MCP server (fcvm mcp)
lib/web/server.py       web console backend (fcvm serve): HTTP, WebSockets, stats
lib/web/static/         web console frontend (plain HTML/CSS/JS, vendored xterm.js)
lib/build.py            fcvm build (Dockerfile subset)
lib/console.py          per-VM serial console relay (attach/detach, logs)
lib/net.sh              host bridges, taps, NAT, isolation and anti-spoofing rules
init/fc-init.c          init for every VM (initramfs): root assembly, container PID 1, exec agent
kernel/microvm-*.config kernel fragment
docs/security.md        security and isolation reference (jailer, network, verification)
bin/ kernels/ images/ vms/ volumes/ snapshots/ cache/ build/   generated
```

## Roadmap

Priorities come from a review of fcvm from two angles: as a sandbox for
agentic development (compared with Multipass), and as something an enterprise
could adopt. Orchestration is out of scope for now. Items marked ✅ are done.

### Agentic development

Where fcvm already fits: many short-lived, disposable, isolated sandboxes.
`create` takes 0.1 s, an app VM runs in about 1 s, and Ubuntu boots in
2.3 s. Docker Hub works as the toolchain catalogue, behind a real kernel
boundary. `exec` has real exit codes and separate stdout/stderr, which maps
directly onto an agent's "run command" tool. Multipass is still better for
long-lived dev machines with mounted source trees, and it runs on macOS and
Windows. fcvm needs KVM and never will.

- **A1. Getting code in and out.** ✅ `fcvm cp` (both directions), ✅ named
  volumes (`-v NAME:/path`, persistent ext4 disks), ✅ live host directories
  (`-v /host/dir:/path`): the guest kernel's 9P client over a vsock
  connection to a host-side 9P server, since Firecracker has no virtio-fs.
  Still open: faster many-small-file workloads (a multi-threaded or native
  server, or opt-in client caching).
- **A2. Snapshots and commit.** ✅ `fcvm commit VM IMAGE` saves a VM's
  writable layer as a new image layer (Docker-style stacking, instant,
  rootless). ✅ `fcvm snapshot` / `fcvm fork`: Firecracker memory snapshots,
  forked into running VMs in ~120 ms, each with its own disk, IP, MAC and
  hostname, and RNG reseeding through VMGenID. Still open: diff snapshots
  (only dirty pages) to cut the ~0.75 s/GB pause, and snapshots of VMs with
  read-write volumes.
- **A3. Machine interface.** ✅ `--json` for `ls`/`images`, `fcvm inspect`,
  and ✅ an MCP server (`fcvm mcp`) that exposes sandbox tools to agents.
- **A4. Egress control.** ✅ `--net none`, ✅ `--allow` allowlists enforced by
  a host-side proxy with an audit log, ✅ DNS from the host's upstream
  resolvers. Still open: non-HTTP protocols in allowlists (e.g. SSH to
  github.com), and TLS inspection, which is deliberately not done.
- **A5. exec for agents.** ✅ `--timeout`, `-e KEY=VAL`, `-w DIR`.
- **A6. Provisioning.** ✅ `fcvm build` (Dockerfile subset, per-step cache,
  single-layer results), ✅ local `docker save`/OCI imports, ✅ `fcvm squash`.
  Still open: multi-stage builds, and a cloud-init style first-boot hook for
  Ubuntu VMs.
- **A7. Concurrency.** ✅ 64 taps per network pool, and `--net none` VMs
  need none. Beyond ~240 VMs per pool, taps would have to be created on
  demand, which needs root.
- Not planned: macOS/Windows, GPUs, nested virtualization, desktop GUIs.

### Web console

A cloud-console-like web UI for one host: browse images, launch and manage
instances, see resource use, with a console and shell in the browser. It
lives in this project (`fcvm serve`) because it is a client of the same
operations as the CLI and MCP server, and it ships with them. A multi-host
fleet view would be a separate control-plane product built on this API.

- **W1. Local web console v1.** ✅ `fcvm serve`, bound to 127.0.0.1 with a
  token cookie. A dashboard of host and VM resource use; images (list,
  import, delete); a launch form (image, vCPU/memory, network mode and
  allowlist, ports, volumes and host directories); instances
  (start/stop/delete, snapshot, fork); an instance page with live stats,
  logs, egress log, and browser terminals (serial console and shell over
  WebSockets); snapshots and volumes. Standard library only, no build step,
  with xterm.js vendored.
- **W2. Remote access.** Serve on a LAN or tailnet: TLS, real
  authentication (local users, or OIDC/SSO), roles (viewer, operator,
  admin). Shares work with enterprise items E1/E4/E5.
- **W3. Builds from the UI.** ✅ Edit a Dockerfile and upload a build context,
  with streamed build logs and the per-step cache made visible.
- **W4. Files.** ✅ A file browser for a running VM (upload, download, edit,
  rename, delete) using the exec agent's native file operations, so it works
  in any image, even without a shell, and management of host-directory
  mounts, live on running VMs.
- **W5. Activity and audit.** A timeline of who did what (launch, exec,
  console sessions, egress denials), feeding E5.
- **W6. Service mode.** Run `fcvm serve` as a systemd user service; VMs
  marked "start on boot" come back after a host reboot (with E4).
- **W7. Metrics.** Guest-level metrics through the agent (CPU, memory, disk
  and processes inside the VM), longer history, and a Prometheus endpoint.
- **W8. Templates.** Saved launch presets, e.g. "Python sandbox, @pypi only,
  2 GB", shared with the MCP server's defaults.
- **W9. Fleet view.** Several fcvm hosts in one console: a separate
  control-plane product using `fcvm serve` as the per-host agent.

### Enterprise

Prior art to position against: Weave Ignite (Docker image → Firecracker VM,
archived 2023), Fly.io (the same idea as a platform), E2B (Firecracker
sandboxes for AI agents, as a service), Kata Containers and
firecracker-containerd (microVMs behind the container runtime interface).
fcvm's niche is self-hosted, simple and auditable: closer to Multipass than
to Kubernetes.

What is solid: a minimal monolithic guest kernel, digest-verified pulls with
`@sha256:` pinning, shared read-only images with per-VM layers, rootless
operation, and a small auditable code base. What blocks adoption, in order:

#### E1. Run VMs under the Firecracker jailer ✅

Done: `fcvm create --jail` / `JAIL=1` and `fcvm jail-setup` (see "Jailed
VMs"): a per-VM uid, chroot, cgroup v2 limits and a per-VM network namespace
(tap inside, veth to the bridge), through a root helper that validates every
path. Snapshots and fork of jailed VMs, cgroup-based I/O stats, and jailed by
default for MCP sandboxes and the web console. `--new-pid-ns` was left out on
purpose (see "Jailed VMs" for why). The original plan follows.

Before this, Firecracker always ran as your user. KVM and Firecracker's own seccomp filters
are the only boundary between a guest and the host, and every VM can reach the
files of every other VM. `bin/jailer` (already downloaded by `fcvm firecracker`)
is Firecracker's production wrapper. Plan:

- **Opt-in mode**: `fcvm start --jail VM` / `JAIL=1`, with the rootless mode
  staying the default for development. The jailer has to start as root (chroot,
  mknod, cgroups, namespaces, then drop privileges). That means either `sudo`
  per start or a small root helper (a systemd service owning `/srv/jailer`)
  that `fcvm` asks to launch VMs. The helper keeps the CLI password-free.
- **Chroot per VM** under `/srv/jailer/firecracker/<vm>/root`. The kernel, the
  shared image (read-only) and the VM's `rw.ext4` are hard-linked or
  bind-mounted in. `fc.json` paths become chroot-relative.
- **Dedicated uid/gid per VM** from a reserved range, owning only that VM's
  `rw.ext4` and sockets, so one compromised VMM can't read or write another
  VM's disk.
- **cgroup v2 limits** (`--cgroup-version 2`, `cpu.max`, `memory.max`, pids)
  derived from `--vcpus`/`--mem`, plus `--resource-limit no-file=...`.
- **Namespaces**: `--new-pid-ns`, and `--netns` with the VM's tap inside a
  per-VM network namespace, joined to `fcbr0` through a veth pair. `net.sh`
  would create those instead of the flat tap pool.
- **Keep the CLI working**: the API socket and vsock socket live in the chroot.
  `stop`, `exec`/`shell` and the port forwarder need group access to them, so
  the helper would create them with a shared `fcvm` group.

#### E2. Network isolation and policy ✅

Done (see "Isolation" under networking):
- VMs are isolated from each other, both bridged and routed through the host.
- VMs are cut off from host services.
- Anti-spoofing pins each port to its MAC and IPv4 address.
- Guests get no IPv6.
- Published ports bind to `127.0.0.1` by default.
- Egress allowlists came with A4.

Still open:
- Full IPv6 for guests (addressing, NAT66 or routed, and the same rules for
  v6).
- An option to keep full-network VMs off private ranges: today NAT reaches
  your LAN and other bridges on the host (LXD, Multipass, Docker).

#### E3. Supply chain

- Firecracker and the kernel are checked against checksums from the same
  place they're downloaded from. kernel.org's signed `sha256sums.asc` is not
  signature-verified yet (GPG).
- Container images: signature verification (cosign/notation), registry
  allowlists, mirrors/proxies (Artifactory), `~/.docker/config.json` and
  credential helpers.
- Reproducibility: "latest stable" is good for patches but not reproducible,
  and Firecracker upstream validates 5.10, 6.1 and 6.18 guests. Enterprise
  default: a pinned LTS kernel (`KERNEL_CHANNEL=longterm`), SBOMs, and
  reproducible image builds.

#### E4. Daemon and API

State lives in JSON files, pid files and file locks. After a host reboot,
nothing is recovered: taps are gone and VMs aren't restarted. There is only
light protection against two `fcvm` commands running at once, and no API.
Needed: a daemon with an API and a state store that reconciles after reboot.
Orchestration would build on it later. The control plane likely moves to Go
or Rust at that point, while `fc-init` stays small and static.

#### E5. Audit and observability

Log `exec`/`shell`/`console` sessions (who ran what, and when). Export
Firecracker metrics (`--metrics-path`), and ship console logs somewhere
central.

#### E6. Resource governance

Host-side cgroup limits (with E1), Firecracker rate limiters for block and
network I/O, quotas on writable layers and volumes (today they are sparse
files that can fill the host disk), and disk encryption at rest.

#### E7. Credentials in images

The Ubuntu base image contains the project SSH key and the builder's public
keys, and `fcvm ssh` skips host-key checking. That is fine on a laptop, not in
shared images. Keys should be injected per VM at boot instead.

#### E8. Engineering maturity

- ✅ `fc-init` boots from an initramfs instead of living in every image, so
  upgrading the init or agent no longer means rebuilding images.
- A test suite and CI, covering failure paths, concurrency and host reboots.
  The bash `set -e` pitfalls hit during development show why.
- Exec agent protocol: ◐ requests carry a version tag (`fcvm2`), and a
  mismatch fails with a clear error. Still open: version negotiation and a
  compatibility policy, so newer hosts can talk to VMs booted with an older
  initramfs.
- aarch64 (Graviton): needs `kernel/microvm-aarch64.config` and testing.

#### Smaller items

- Published ports are TCP only and don't preserve the client address. An
  nftables DNAT mode in `net.sh` (root) would fix both.
- `exec` via the agent has no auth beyond access to the VM's vsock socket.
  That is fine for a single user; the jailer's per-VM uids are the natural
  place to tighten it.
