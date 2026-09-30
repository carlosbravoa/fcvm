# How it works

How fcvm is put together, and why it's built this way. You don't need any
of this to use fcvm. It's for when you want to understand, debug or
change it.

- [The big picture](#the-big-picture)
- [Design decisions](#design-decisions)
- [The guest kernel](#the-guest-kernel)
- [Boot and the root filesystem](#boot-and-the-root-filesystem)
- [Layers, commit and squash](#layers-commit-and-squash)
- [App VMs and exit codes](#app-vms-and-exit-codes)
- [Consoles and reaping](#consoles-and-reaping)
- [The exec agent](#the-exec-agent)
- [Host directories](#host-directories)
- [Networking](#networking)
- [Snapshots and fork](#snapshots-and-fork)
- [Builds](#builds)
- [The web console](#the-web-console)
- [The supervisor and liveness](#the-supervisor-and-liveness)
- [Releases, installs and upgrades](#releases-installs-and-upgrades)
- [Code layout](#code-layout)

## The big picture

```
kernel.org ──► build-kernel.sh ──► kernels/vmlinux ────────────────────────┐
fc-init.c ──► build-init.sh ──► build/initramfs.cpio ──────────────────────┤
Ubuntu archive ─► mmdebstrap ─► tar ─► mkfs.ext4 -d ─► images/ubuntu-26.04.ext4 ─┤
registry ─► oci_import.py ─► flattened tar + /.fcvm ─► mkfs.ext4 -d ─► images/*.ext4 ─┤
                                                                   vm.sh create/start
                              vms/<vm>/{rw.ext4, vm.json, fc.json} ─► firecracker
```

Each running VM is one Firecracker process, plus a few small helpers that
run as you, all started by `fcvm start`:

| process | role |
|---|---|
| `firecracker` | the VMM: KVM, virtio devices, the API socket (`fc.sock`), vsock (`vsock.sock`) |
| `console.py serve` | owns the serial console's PTY, logs it, lets clients attach and detach, and reaps the VM when Firecracker exits |
| `portfwd.py` | published ports (only if `-p`) |
| `share9p.py` | host directories (only if `-v /dir:...`) |
| `egress_proxy.py` | one per user, shared by all restricted VMs |

Inside the guest, `fc-init` from the initramfs is PID 1 for app VMs, or
hands over to systemd for system VMs. Either way, it runs the exec agent.

## Design decisions

**Firecracker, not QEMU.** A minimal VMM with a small device model: virtio
block, net and vsock, a serial port, and no BIOS, USB or PCI passthrough.
It boots a kernel directly in a fraction of a second, and it's what AWS
Lambda and Fargate run on. The small surface is the point.

**No Docker, containerd or runc.** Images are unpacked into ext4 disks, and
a small init reproduces the container semantics (entrypoint, env, user,
workdir, exit code) inside the VM. There's no daemon to trust, and nothing
shares the host's kernel.

**Build your own kernel.** The guest kernel comes from `allnoconfig` plus a
short fragment. It's monolithic, with only what Firecracker's devices and
containers need. Building it locally from kernel.org keeps it small,
current and auditable, instead of trusting a prebuilt binary.

**The init lives in the initramfs, not in images.** Images stay pristine,
and upgrading the init or the exec agent never means rebuilding images.
Every VM boots the same `build/initramfs.cpio`.

**ext4 disks and overlayfs, not image files per VM.** Images are shared
read-only disks, each VM gets a sparse writable layer, and the guest's
overlayfs stacks them. `create` is instant, and a VM costs megabytes.
`mkfs.ext4 -d` builds disks from a tarball without root.

**exec over vsock, not SSH.** It works in every image (distroless ones
too), with no network, keys or sshd. It carries exit codes and separate
stdout and stderr, which agents need.

**Host directories over 9P on vsock.** Firecracker has no virtio-fs or 9P
device. The guest kernel's own 9P client can speak over any file
descriptor, and vsock provides one. That means no FUSE and no extra binary
in the image.

**Persistent taps owned by you.** Creating a tap needs root; using one you
own doesn't. `net-up` creates a pool once, so starting VMs never needs
root.

**An egress proxy, not IP filtering.** Allowlists are about host names, and
services live behind CDNs whose IPs change. A proxy that checks the
requested name, resolved on the host, is precise and logs every decision.

**Bash and standard-library Python.** The whole host side is readable in an
afternoon and has no dependencies to install or audit. The guest side is
one static C file.

**Root only where unavoidable, and never from your tree.** `net-up`, the
jailer helper and the boot network unit need root. The helper and the boot
unit run root-owned copies installed under `/usr/local/lib/fcvm`, never
files you can edit. Everything else runs as you.

## The guest kernel

**Where it comes from.** You build it, on your machine, with `fcvm kernel`
(`lib/build-kernel.sh`). It isn't Ubuntu's kernel or one of Firecracker's
prebuilt CI kernels:

1. Picks the newest release of `KERNEL_CHANNEL` (`stable` by default, or
   `longterm`, `mainline`, or an exact version) from
   `https://www.kernel.org/releases.json`.
2. Downloads the official source tarball from `cdn.kernel.org` into `cache/`.
3. Checks its SHA-256 against kernel.org's `sha256sums.asc`. That catches
   corruption, but it isn't a proof of origin, because the PGP signature
   isn't verified yet ([roadmap E3](roadmap.md#e3-supply-chain)).
4. Extracts it to `build/linux-X.Y.Z/`, runs `make allnoconfig`, applies
   `kernel/microvm-x86_64.config`, and reports any requested option that
   didn't make it into the final `.config`.
5. Compiles `vmlinux` with the host's gcc, in about 2.5 minutes on 12
   cores.
6. Installs it:

```
kernels/
  vmlinux -> vmlinux-7.2.8     # what every VM boots (the newest build)
  vmlinux-7.2.8                # ~27 MB uncompressed ELF
  config-7.2.8                 # the exact .config it was built with
```

The repository holds the recipe (script and fragment), not the binary, so
each machine builds its own.

### Kernel configuration notes

Learned the hard way on 7.2 with Firecracker 1.17:
- **`CONFIG_PCI=y` is required, even for virtio-mmio guests.**
  Firecracker's ACPI DSDT describes a PCI root bridge. Without PCI support
  the DSDT fails to load, device IRQs aren't set up, and virtio probes fail
  with -22. One boot message is expected and harmless: `PCI: Fatal: No
  config space access function found`, a side effect of `pci=off`.
- **`CONFIG_VIRTIO_MMIO_CMDLINE_DEVICES` is off.** Devices are found
  through ACPI, and the `virtio_mmio.device=` arguments Firecracker still
  passes would register every device twice.
- **`SERIAL_8250_NR_UARTS=1`.** Firecracker has a single UART.
- **VMGenID** is on, so forks reseed their RNG.
- **9P** (`NET_9P`, `NET_9P_FD`, `9P_FS`) is on, for host directories.
- **nftables rejects and the bridge family** (`NFT_REJECT`,
  `NF_TABLES_BRIDGE`) are on, so fcvm's own `net-up` works inside a VM (for
  testing fcvm in fcvm). Firecracker doesn't expose nested virtualization,
  so VMs can't boot VMs of their own; test the whole setup in a QEMU-based
  VM (such as Multipass) instead.
- **After moving to a new kernel series**, check the build's report of
  options that didn't make it into the final `.config`.

## Boot and the root filesystem

Every VM boots the same kernel with `build/initramfs.cpio`, which holds
only `fc-init` as `/init`. `fc-init` assembles the root from drives named
on the kernel command line, then switches into it (the same moves as
`switch_root`). Drives are attached in order, `vda`, `vdb`, ...:

| drive | what | argument |
|---|---|---|
| base image | read-only, shared by all VMs | `fcvm.root=` |
| writable layer | `vms/<vm>/rw.ext4`: sparse ext4 holding overlayfs `upper/` and `work/` | `fcvm.rw=` |
| committed layers | read-only, topmost first | `fcvm.layers=` |
| volumes | `volumes/<name>.ext4` | `fcvm.vols=DEV:PATH[:ro]` |

The root is an overlayfs of the layers under the VM's writable layer. After
switching, `fc-init` either runs the app image's command or, for system
images (`fcvm.exec=/sbin/init`), execs systemd as PID 1. A `--copy` VM has
a private full disk instead, with no overlay.

The guest configures `eth0` from the kernel's `ip=` argument, so no DHCP
client or network manager runs. The Ubuntu image links
`/etc/resolv.conf` to `/proc/net/pnp`.

## Layers, commit and squash

**Commit.** `fcvm commit VM IMAGE` copies the stopped VM's writable layer
(sparse, typically a few MB) and registers it as a new image whose parent
is the VM's image. A VM from that image stacks `lowerdir=layer:...:base`
under its own writable layer, which is the Docker model: deletions are
overlayfs whiteouts and keep working across layers. Commit is rootless and
instant, because nothing is merged.

**Squash.** Every layer is a separate virtual disk, and Firecracker on x86
has about 19 device slots in total, shared by disks, network, vsock and
entropy. `fcvm squash` merges layers in a throwaway VM that boots only the
initramfs, in `fc-init`'s merge mode:
- the layer disks are attached read-only, with an empty output disk;
- each layer's `upper/` is applied in order, keeping whiteouts and opaque
  directories, so deletions of files in the base image survive;
- owners, modes, setuid bits, xattrs (such as file capabilities),
  hardlinks, symlinks, device nodes and timestamps are preserved.

**Identity scrubbing.** Committing a VM that has booted systemd removes its
machine-id, SSH host keys, random seed and journal from the layer. The base
image's blank versions show through again.

## App VMs and exit codes

**Image configuration.** The importer keeps the image config (Entrypoint,
Cmd, Env, WorkingDir, User) in `/.fcvm/`.

**What `fc-init` does as PID 1 of an app VM:**
- mounts `/proc`, `/sys`, `/dev`, devpts, cgroup2 and friends;
- writes `/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf`;
- drops to the image user and execs the entrypoint;
- forwards signals and reaps zombies.

**When the app exits**, `fc-init` writes its status (exit code, or
128+signal) to `/.fcvm/exit-status` on the VM's writable layer, then reboots
the guest. With `reboot=k`, Firecracker then exits. The host reads the
status back with `debugfs`, without mounting anything, for `run`, `ls` and
`inspect`.

**Stopping.** `fcvm stop` sends Ctrl-Alt-Del through the API, which
`fc-init` turns into SIGTERM for the app, and systemd into a shutdown.

**Egress settings.** For restricted VMs, `fc-init` exports `http(s)_proxy`
to the app and to every exec session. System images get them through
`systemd.setenv=`.

## Consoles and reaping

Firecracker's serial port is its stdin/stdout. `fcvm start` runs it under
`lib/console.py`, a small relay that owns the PTY:
- it writes everything to `vms/<vm>/console.log` and keeps a scrollback;
- it serves clients on `vms/<vm>/console.sock`, so consoles attach and
  detach without affecting the VM.

**Reaping.** When Firecracker exits, the relay runs `fcvm _reap <vm>`:
- it records how the VM ended (`last-exit`: the app's exit code and
  Firecracker's status);
- it stops the port forwarder and host-directory servers;
- it removes the runtime files;
- it deletes throwaway VMs.

## The exec agent

Every VM has a vsock device, and `fc-init` runs an exec agent on vsock port
1024:
- **App VMs:** it's forked by PID 1.
- **System images:** it runs as `fcvm-agent.service`, from a copy of
  `fc-init` placed on a tmpfs at boot.

**The connection.** The host side (`lib/exec_client.py`) connects through
Firecracker's vsock Unix socket (`vms/<vm>/vsock.sock`) with
`CONNECT 1024`.

**The protocol.** Each frame is a type byte, a 4-byte length and a payload.
A session starts with a request frame:
- a protocol version tag (`fcvm2`), so a host/guest mismatch fails clearly;
- whether a TTY is wanted, and the window size;
- the user, working directory, environment and argv.

Then data, window-size and close frames flow both ways, until an exit
frame carries the status.

**Running the command.**
- It runs on a PTY, or on pipes with separate stdout and stderr.
- `-u USER` is resolved in the guest from its own `/etc/passwd` and
  `/etc/group`, with supplementary groups. The PTY is handed to that user,
  as `login` does.
- `--timeout` drops the connection, and the agent then kills the
  command's process group.

**Other frame types:**
- **network reconfiguration**, used after a fork: address, MAC and
  hostname, set with plain ioctls so it works in any image;
- **file operations** (list, stat, read, write, mkdir, remove, rename),
  used by the web console's file browser, so it needs no shell or `tar` in
  the image;
- **mount/umount of host directories**, used by live `fcvm mount`.

## Host directories

**How the mount is built.** Firecracker has no virtio-fs or 9P device, so
fcvm builds the mount from parts the kernel already has:
1. At boot (or on a live `fcvm mount`), `fc-init` opens a vsock
   connection to the host, on a port from 10000 up.
2. Firecracker hands that connection to `vms/<vm>/vsock.sock_<port>`,
   where `lib/share9p.py` serves the directory. It's a small 9P2000.L
   server: standard library only, one per VM, running as you.
3. `fc-init` gives the connection to the guest kernel's own 9P client
   (`mount -t 9p -o trans=fd`).

**How the server behaves.**
- It confines every operation to the shared directory, and resolves
  symlinked parents on the host so they can't escape.
- It reports files as owned by the image's user, and accepts but ignores
  `chown`.
- It doesn't cache, so changes on either side are visible at once.
- It exits with the VM.

## Networking

**Taps.**
- `net-up` creates the bridges and persistent tap pools owned by you.
- `fcvm start` claims the first free tap of the VM's pool with a file
  lock, held by Firecracker's relay for the life of the VM.
- The tap index gives the VM its address (`.10 + i`) and MAC (`06:00:` or
  `06:01:` + the address). The anti-spoofing rules pin exactly that pair
  per port.

**Jailed VMs** use a veth pair instead: the host end joins the bridge, and
the other end sits in the VM's own network namespace, bridged to its tap.
See [Security](security.md#jailed-vms-in-detail).

**Published ports.** `lib/portfwd.py` is an asyncio TCP relay running as
you:
- it listens on the host port and forwards to the VM's IP;
- it watches the Firecracker pid and exits with the VM.

**The egress proxy.** `lib/egress_proxy.py` runs one per user, started on
demand:
- it accepts `CONNECT` for HTTPS, and absolute-URL requests for HTTP;
- it identifies the VM by source address, which anti-spoofing makes
  trustworthy;
- it checks the requested host against `vms/.egress/<ip>.json`, re-read on
  every request, so `fcvm egress --allow` applies at once;
- it logs each decision to `vms/<vm>/egress.log`.

## Snapshots and fork

**Taking a snapshot.** `fcvm snapshot` pauses the VM, asks Firecracker for
a full snapshot (device state and memory file), and copies the writable
layer, still paused. Then it resumes the VM. The memory file is made sparse
afterwards with `fallocate -d`.

**Forking.** `fcvm fork` starts a bare Firecracker and loads the snapshot
with overrides for the new VM:
- a new tap (`network_overrides`);
- its own vsock socket (`vsock_override`);
- (not `clock_realtime`: it needs a TSC-clocked host, which nested hosts,
  such as cloud VMs, aren't; the exec agent sets the guest clock instead).

Then it re-points the writable drive at the fork's own copy (`PATCH
/drives`), resumes the VM, and has the exec agent set the new address, MAC,
hostname and the clock (from the host's; the guest's stopped at the
snapshot).

**If the source VM is gone.** Loading reopens the source VM's disk path
before the drive is re-pointed. If that VM has been deleted, a symlink to
the snapshot's disk stands in for it while loading.

**Jailed forks** need no overrides at all. Paths inside a jail
(`/drive0.ext4`, `/vsock.sock`, `tap0`) are the same for every VM.

## Builds

`lib/build.py` parses the Dockerfile. Each filesystem step (`RUN`, `COPY`,
`ADD`, `WORKDIR`) runs in a VM booted from the previous step's result. That
VM is then stopped and committed as a hidden cache image (`_bc-<key>`), a
fresh environment per step as with Docker.

**The cache key** chains:
- the previous step's key;
- the instruction;
- the config so far (ENV, USER, ...);
- for `COPY`/`ADD`, the copied files' content.

**The result.**
- Cache chains are squashed past six layers.
- The result is always the FROM image plus one squashed layer, with ENV,
  WORKDIR, USER, ENTRYPOINT and CMD written to its `/.fcvm` config.
- `USER` names are resolved at boot against the image's `/etc/passwd`.

## The web console

`lib/web/server.py` is standard-library Python. It implements HTTP/1.1 and
WebSockets (RFC 6455) on asyncio.
- **Changes** go through the fcvm CLI, like the MCP server, so the UI, the
  API and the command line always agree.
- **Stats** come straight from `/proc`: the Firecracker process's CPU time,
  resident memory and I/O. For jailed VMs, I/O comes from the cgroup's
  `io.stat`. Traffic comes from the tap or veth counters.
- **The frontend** in `lib/web/static/` is plain HTML, CSS and JS with no
  build step. xterm.js is vendored (MIT, see `vendor/LICENSE.xterm`).

## The supervisor and liveness

**Liveness.** A VM is running if its pid file names a live process that is
that VM's Firecracker. Each start records the boot id and the process's
start time in `pid.id`. So a pid file left by a crash or a reboot never
matches whatever process later reuses the pid. Cleanup only kills helper
processes whose command line is still fcvm's.

**The supervisor** (`lib/web/supervisor.py`) runs inside `fcvm serve`:
- **At startup**, it reaps stale VMs and restarts VMs that were running
  when the host went down: stale ones, or ones marked `resume` by the
  shutdown hook.
- **Then, every 2 s**, it looks for new exits and applies each VM's
  restart policy, with backoff.
- **It uses the CLI** for every action, taking the same per-VM lock as
  your commands.

**Starting VMs through the daemon.** With the service running, `start`,
`run -d` and `fork` post to the daemon's API instead of launching the VM
themselves. The VM then belongs to `fcvm.service`'s cgroup rather than the
caller's login-session scope. At shutdown, systemd kills session scopes in
parallel with stopping services, so only VMs in the service's cgroup can be
stopped cleanly by `fcvm _shutdown`. The daemon's own fcvm commands carry
`FCVM_DAEMON=1`, so they launch locally instead of calling back.

## Releases, installs and upgrades

**Code and state.**
- `FCVM_ROOT` is the code: the directory the `fcvm` script resolves to
  (following symlinks).
- `FCVM_HOME` is the state:
  - if the code directory already holds `vms/`, `images/` or `kernels/`,
    it's the code directory itself (a pre-0.5 checkout);
  - otherwise it's `$XDG_DATA_HOME/fcvm`, that is `~/.local/share/fcvm`.
- `lib/common.sh` exports both, so every script and the Python side
  agree.

**Installed layout.** `install.sh` unpacks a tag's GitHub archive into
`LIB/VERSION` (`LIB` is `~/.local/lib/fcvm`, or `/opt/fcvm` with
`--system`), then:
- it points `LIB/current` at it with an atomic rename;
- it links `fcvm` in the bin directory to `LIB/current/fcvm`;
- it keeps the active release and the one before, and removes older ones.

**Upgrades.** `fcvm upgrade` runs the running release's installer for the
new version, which flips `current`, then lists what `status` says needs
refreshing.
- **Why things point at `current`.** Anything that must outlive a release
  (the systemd unit's `ExecStart`, a suggested MCP registration) uses
  `LIB/current/fcvm` (`fcvm_entry`) rather than a versioned path, so it
  follows upgrades.
- **The root-owned copies** of the jailer helper and the boot-time
  network script are refreshed by re-running `jail-setup` and
  `service install`. `status` compares them with the tree.

**Guest compatibility.** `build/initramfs.src` records the SHA-256 of the
`fc-init.c` the initramfs was built from. `start` rebuilds the initramfs
whenever that differs, so after an upgrade VMs never boot an init older
than the host code that talks to it. Builds write temporary files and
rename them, so concurrent starts are safe.

**Versions.**
- `VERSION` holds the release. `fcvm_version` appends `+N.gHASH` (and
  `.dirty`) in a git checkout that isn't exactly at `vVERSION`.
- `status` compares the version with the highest `vX.Y.Z` tag on GitHub
  (cached for 6 hours).

## Code layout

```
fcvm                    CLI entry point
VERSION, CHANGELOG.md   the release and what changed
install.sh              the installer (releases in ~/.local/lib/fcvm or /opt/fcvm)
lib/common.sh           settings, the code/state locations (FCVM_ROOT/FCVM_HOME), helpers
lib/setup.sh            fcvm setup (guided first-time setup)
lib/status.sh           fcvm status (health check)
lib/upgrade.sh          fcvm upgrade
lib/host-setup.sh       host packages, KVM access
lib/net.sh              host bridges, taps, NAT, isolation and anti-spoofing rules
lib/fetch-firecracker.sh
lib/build-kernel.sh     kernel.org → vmlinux
lib/build-init.sh       fc-init and the initramfs
lib/build-base.sh       Ubuntu 26.04 base image
lib/oci_import.py       registry client + layer flattening (stdlib only)
lib/import.sh           import wrapper (sizes and creates the ext4)
lib/build.py            fcvm build (Dockerfile subset)
lib/vm.sh               VM lifecycle
lib/console.py          per-VM serial console relay (attach/detach, logs, reaping)
lib/exec_client.py      host side of exec / shell / file operations (vsock)
lib/portfwd.py          rootless TCP port publishing
lib/share9p.py          9P server for live host directories
lib/egress_proxy.py     egress proxy for restricted VMs (allowlists, logging)
lib/egress-presets.conf allowlist presets (@pypi, @npm, ...)
lib/jaild.py            fcvm-jaild, the root helper for jailed VMs
lib/jail-setup.sh       installs/removes fcvm-jaild
lib/service.sh          fcvm service install/remove/status
lib/mcp_server.py       MCP server (fcvm mcp)
lib/web/server.py       web console backend and API (fcvm serve)
lib/web/supervisor.py   restart policies and recovery after a crash or reboot
lib/web/static/         web console frontend (plain HTML/CSS/JS, vendored xterm.js)
init/fc-init.c          init for every VM (initramfs): root assembly, PID 1, exec agent
kernel/microvm-*.config kernel fragment
docs/                   this documentation
tests/                  tests/run (lint, unit, integration), fresh-machine.sh; see CONTRIBUTING.md
.github/workflows/      CI
```
