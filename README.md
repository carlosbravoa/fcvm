# fcvm: Firecracker microVMs from Ubuntu 26.04 and Docker images

`fcvm` builds everything a Firecracker microVM needs and turns container images
into VMs:

- **Kernel**: fetches the newest kernel from kernel.org (stable by default),
  starts from `allnoconfig` and applies a small fragment
  (`kernel/microvm-x86_64.config`). The result is a ~27 MB monolithic `vmlinux`
  that boots in about 0.5 s.
- **Ubuntu 26.04 base image**: minbase + systemd + ssh, built rootless with
  `mmdebstrap`.
- **Container import**: pulls any image from Docker Hub (or ghcr.io, quay.io,
  ...) and flattens its layers. A small static init (`init/fc-init.c`, booted
  from an initramfs) runs the image's entrypoint with its env, workdir and
  user.
- **Agent-friendly**: `exec` with exit codes, timeouts and separate
  stdout/stderr, `cp`, `commit` (snapshot a prepared VM as an image), named
  volumes, `--json` output, and an MCP server (`fcvm mcp`) that exposes all
  of it as tools.
- **Networking**: one host bridge with NAT and a pool of taps owned by your
  user, so VMs start without root.

Only `host-setup` and `net-up` need sudo. Building images uses user
namespaces and `mkfs.ext4 -d <tarball>`, so file ownership is kept without
root.

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
| `net-up` / `net-down` | bridge `fcbr0` (172.30.0.1/24), taps `fctap0..15`, nftables NAT, ufw rules |
| `firecracker` | downloads the latest Firecracker release into `bin/` (checksum-verified) |
| `kernel [stable\|mainline\|longterm\|X.Y.Z]` | builds `kernels/vmlinux-X.Y.Z`; `kernels/vmlinux` points at the newest |
| `init` | builds `build/fc-init` (static) and `build/initramfs.cpio`, which every VM boots with |
| `base [NAME]` | builds `images/ubuntu-26.04.ext4` |
| `import REF [NAME]` | registry image → `images/NAME.ext4` + `NAME.json` |
| `images [--json]`, `ls [--json]` | lists images (with what uses each) / VMs (state, exit code, disk use, ports, volumes) |
| `inspect VM` | the VM's details as JSON |
| `create VM IMAGE [opts] [-- CMD...]` | VM on the shared image plus its own writable layer. `--vcpus N`, `--mem MiB`, `--disk SIZE` (layer size, default 8G sparse), `-p [BIND:]HOST:GUEST` (repeatable), `-v VOLUME:/PATH[:ro]` (repeatable, created on first use), `--idle` (container: run nothing, stay up for `exec`), `--copy` (private full copy instead), `-- CMD` (replaces the container command) |
| `start [-a] VM` | boots in the background, like `docker start`. `-a` attaches the console |
| `run IMAGE [-d] [opts] [-- CMD...]` | throwaway VM, deleted when it stops. **Container images**: attached like `docker run`: you see the output, Ctrl-C goes to the app, Ctrl-] detaches, and fcvm exits with the container's exit code. **Ubuntu images**: boots and opens `fcvm shell` (or runs CMD); when the shell or CMD ends, the VM is stopped and deleted. `-d`: background |
| `stop VM` | Ctrl-Alt-Del (graceful), killed after 20 s |
| `exec [-i] [-t] [-u USER] [-w DIR] [-e K=V]... [--timeout S] VM CMD...` | runs a command in a running VM, like `docker exec`. Exits with its status, or 124 on timeout |
| `cp [-L] SRC DST` | copies files or directories into or out of a running VM (`VM:PATH` on one side), like `docker cp` |
| `commit VM IMAGE` | saves a stopped VM's changes as a new image, a read-only layer on its image |
| `rmi IMAGE` | deletes an image nothing depends on |
| `volume create NAME [SIZE]`, `volume ls [--json]`, `volume rm NAME` | named volumes: persistent ext4 disks attached with `-v` |
| `mcp` | MCP server on stdio, for agents |
| `shell [-u USER] VM` | interactive shell in a running VM (bash, else sh). Same as `exec -it VM` |
| `console VM` | attaches to the live serial console. Ctrl-] detaches and the VM keeps running |
| `logs [-f] VM` | console output of the current or last boot |
| `ssh VM`, `rm VM` | connects with ssh, deletes |

Settings (kernel channel, Ubuntu suite, packages, subnet, default vCPU/memory,
extra kernel args) live at the top of `lib/common.sh`. Override them in
`fcvm.conf` or the environment, e.g. `KERNEL_CHANNEL=longterm ./fcvm kernel`
or `VM_KERNEL_ARGS=loglevel=7 ./fcvm run alpine-latest`.

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
runs the container config or, for Ubuntu images (`fcvm.exec=/sbin/init`),
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

**Volumes.** Named ext4 disks in `volumes/`, attached with `-v NAME:/PATH`
and created on first use (`VOLUME_SIZE`, default 10G sparse). They outlive
VMs. A new, empty volume takes the owner and mode of the directory it covers,
as Docker volumes do, so non-root images can write to it. A read-write volume
can be attached to only one running VM at a time; `:ro` volumes can be
shared. Live host-directory mounts aren't possible (Firecracker has no
virtio-fs or 9p); use `fcvm cp` instead.

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
`run`, stops the port forwarder, and deletes throwaway VMs. Ubuntu images
have no serial autologin: the console shows boot messages and a `login:`
prompt, and you get in with `fcvm shell` or `fcvm ssh`.

**exec / shell.** Every VM has a vsock device, and `fc-init` runs an exec
agent on vsock port 1024. In container VMs it is forked by PID 1. In Ubuntu
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
(Ubuntu images). The session's PTY is handed to the user, as `login` does,
so `sudo`, `less` and the like can open `/dev/tty`.

```sh
./fcvm shell web                       # root@nginx:/#
./fcvm shell -u root unpriv            # root shell in an image whose USER is non-root
./fcvm exec -u postgres db psql        # as another user
./fcvm exec web nginx -t               # one-off command
./fcvm cp ./site web:/usr/share/nginx/html   # tar over exec; needs sh and tar in the image
```

**Container VMs.** The importer keeps the image config (Entrypoint, Cmd, Env,
WorkingDir, User) in `/.fcvm/`. `fc-init` mounts `/proc`, `/sys`, `/dev`, devpts, cgroup2 and friends, writes
`/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf`, drops to the image
user, and execs the entrypoint. It forwards signals and reaps zombies. When
the main process exits, it reboots the guest, and with `reboot=k` Firecracker
then exits. `fcvm stop` sends Ctrl-Alt-Del, which `fc-init` turns into SIGTERM
for the app.

**Networking.** The kernel configures `eth0` from the `ip=` boot argument:
`172.30.0.(10+i)` for tap `i`, gateway `172.30.0.1`, DNS `$NET_DNS`. No DHCP
or network manager runs in the guest. The Ubuntu image links
`/etc/resolv.conf` to `/proc/net/pnp`. `fcvm start` takes the first free tap
and holds a lock on it for the life of the VM. VMs are reachable from the host
at their IP, and they reach the internet through NAT.

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
| `create_sandbox` | create and boot a VM. Container images stay idle for `exec` unless given a command |
| `exec` | run a shell command (`command`) or exact `argv`, with `workdir`, `env`, `user`, `stdin` and `timeout` (default 300 s). Returns `exit_code`, `stdout`, `stderr` |
| `write_file`, `read_file` | text files in the VM |
| `copy_to_vm`, `copy_from_vm` | host files and directories in or out (`fcvm cp`) |
| `commit_vm` | save a VM's state as an image; new sandboxes start from it |
| `logs`, `list_vms`, `start_vm`, `stop_vm`, `remove_vm`, `volumes` | lifecycle and state |

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
lib/exec_client.py      host side of fcvm exec / shell (vsock)
lib/mcp_server.py       MCP server (fcvm mcp)
lib/console.py          per-VM serial console relay (attach/detach, logs)
lib/net.sh              host bridge/taps/NAT
init/fc-init.c          init for every VM (initramfs): root assembly, container PID 1, exec agent
kernel/microvm-*.config kernel fragment
bin/ kernels/ images/ vms/ volumes/ cache/ build/   generated
```

## Roadmap

Priorities come from a review of fcvm from two angles: as a sandbox for
agentic development (compared with Multipass), and as something an enterprise
could adopt. Orchestration is out of scope for now. Items marked ✅ are done.

### Agentic development

Where fcvm already fits: many short-lived, disposable, isolated sandboxes.
`create` takes 0.1 s, a container VM runs in about 1 s, and Ubuntu boots in
2.3 s. Docker Hub works as the toolchain catalogue, behind a real kernel
boundary. `exec` has real exit codes and separate stdout/stderr, which maps
directly onto an agent's "run command" tool. Multipass is still better for
long-lived dev machines with mounted source trees, and it runs on macOS and
Windows. fcvm needs KVM and never will.

- **A1. Getting code in and out.** Firecracker has no virtio-fs or 9p, so
  there are no live shared folders. ✅ `fcvm cp` (both directions) and ✅
  named volumes (`-v NAME:/path`, persistent ext4 disks). Still open: a
  host-directory sync over vsock that behaves like a bind mount.
- **A2. Snapshots and commit.** ✅ `fcvm commit VM IMAGE` saves a VM's
  writable layer as a new image layer (Docker-style stacking, instant,
  rootless). Next: Firecracker memory snapshots, to prepare a VM once and
  fork it per agent attempt in about 100–200 ms, or roll back after a bad
  attempt. This needs a new tap and IP per fork (network overrides plus
  re-addressing inside the guest) and entropy reseeding (VMGenID).
- **A3. Machine interface.** ✅ `--json` for `ls`/`images`, `fcvm inspect`,
  and ✅ an MCP server (`fcvm mcp`) that exposes sandbox tools to agents.
- **A4. Egress control.** A per-VM network policy (no network, or an
  allowlist such as PyPI/npm/GitHub only) and configurable DNS. DNS is fixed
  to `$NET_DNS` today, which breaks split-DNS corporate networks.
- **A5. exec for agents.** ✅ `--timeout`, `-e KEY=VAL`, `-w DIR`.
- **A6. Provisioning.** A cloud-init equivalent or build recipe. Import from a
  local `docker save` tarball or OCI layout, not only from registries.
- **A7. Concurrency.** Raise the limit of 16 taps on one /24; parallel agent
  attempts need more.
- Not planned: macOS/Windows, GPUs, nested virtualization, desktop GUIs.

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

#### E1. Run VMs under the Firecracker jailer

Today Firecracker runs as your user. KVM and Firecracker's own seccomp filters
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

#### E2. Network isolation and policy

VMs can reach each other on the flat bridge, and every host service on
172.30.0.1. Published ports bind to `0.0.0.0` by default. Needed: VMs
isolated from each other, `127.0.0.1` as the default bind address for
published ports, egress allowlists (shared with A4), and IPv6.

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
- A versioned exec agent protocol.
- aarch64 (Graviton): needs `kernel/microvm-aarch64.config` and testing.

#### Smaller items

- Published ports are TCP only and don't preserve the client address. An
  nftables DNAT mode in `net.sh` (root) would fix both.
- `exec` via the agent has no auth beyond access to the VM's vsock socket.
  That is fine for a single user; the jailer's per-VM uids are the natural
  place to tighten it.
