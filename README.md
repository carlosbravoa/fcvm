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
  ...), flattens its layers, and injects a small static PID 1 (`init/fc-init.c`)
  that runs the image's entrypoint with its env, workdir and user.
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
./fcvm start dev           # serial console, auto-login as root; `poweroff` to exit
./fcvm start dev -d && ./fcvm ssh dev

./fcvm import nginx:latest
./fcvm run nginx-latest -d -p 8080:80    # throwaway VM, deleted when stopped
curl http://localhost:8080/              # or the VM's own IP: http://172.30.0.10/
./fcvm run alpine-latest -- sh -c 'exit 3'; echo $?   # prints 3
```

## Commands

| command | what it does |
|---|---|
| `host-setup` | installs build/runtime packages, checks `/dev/kvm` access |
| `net-up` / `net-down` | bridge `fcbr0` (172.30.0.1/24), taps `fctap0..15`, nftables NAT, ufw rules |
| `firecracker` | downloads the latest Firecracker release into `bin/` (checksum-verified) |
| `kernel [stable\|mainline\|longterm\|X.Y.Z]` | builds `kernels/vmlinux-X.Y.Z`; `kernels/vmlinux` points at the newest |
| `init` | builds `build/fc-init` (static) |
| `base [NAME]` | builds `images/ubuntu-26.04.ext4` |
| `import REF [NAME]` | registry image → `images/NAME.ext4` + `NAME.json` |
| `images`, `ls` | lists images (with how many VMs use each) / VMs (state, exit code, disk use, ports) |
| `create VM IMAGE [opts] [-- CMD...]` | VM on the shared image plus its own writable layer. `--vcpus N`, `--mem MiB`, `--disk SIZE` (layer size, default 8G sparse), `-p [BIND:]HOST:GUEST` (repeatable), `--copy` (private full copy instead), `-- CMD` (replaces the container command) |
| `start VM [-d]` | boots in the foreground (serial console) or in the background. In the foreground, a container VM's exit code becomes fcvm's |
| `run IMAGE [-d] [opts] [-- CMD...]` | `create` + `start` for a throwaway VM, deleted when it stops |
| `stop VM` | Ctrl-Alt-Del (graceful), killed after 20 s |
| `exec [-i] [-t] [-u USER] VM CMD...` | runs a command in a running VM, like `docker exec`. Exits with its status |
| `shell [-u USER] VM` | interactive shell in a running VM (bash, else sh). Same as `exec -it VM` |
| `console VM`, `ssh VM`, `rm VM` | follows the console log, connects with ssh, deletes |

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

**Shared images, writable layers.** Image disks are read-only (`chmod a-w`)
and shared by every VM created from them. Each VM gets `vms/<vm>/rw.ext4`, a
sparse ext4 holding overlayfs `upper/` and `work/`. `create` takes about 0.1 s
and 6 MB. The image is `/dev/vda` (attached read-only), the layer is
`/dev/vdb`, and the kernel boots `init=/.fcvm/init fcvm.overlay=/dev/vdb`.
`fc-init` mounts the overlay, moves `/dev`, `/proc` and `/sys` into it and
switches root, the same moves as `switch_root`. Then it either runs the
container config or, for Ubuntu images (`fcvm.exec=/sbin/init`), execs
systemd as PID 1. `import` and `base` refuse to overwrite an image that VMs
still use. `--copy` gives a VM a private full copy of the image instead.

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

**exec / shell.** Every VM has a vsock device, and `fc-init` runs an exec
agent on vsock port 1024. In container VMs it is forked by PID 1. In Ubuntu
images it runs as `fcvm-agent.service` (`/.fcvm/init --agent`). The host side
(`lib/exec_client.py`) connects through Firecracker's vsock Unix socket
(`vms/<vm>/vsock.sock`, `CONNECT 1024`). Each connection runs one command, as
the image's user with its env and workdir, as `docker exec` does. It runs on
a PTY with raw mode and window-size updates (`-t`), or on pipes with separate
stdout/stderr (no `-t`), and returns the exit code. No network or sshd is
needed, so it works for any image, including distroless ones (as long as the
command exists). Images built before this feature need a re-import or
rebuild, because `fc-init` is baked into each image.

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
tar c ./site | ./fcvm exec -i web tar x -C /usr/share/nginx/html
```

**Container VMs.** The importer keeps the image config (Entrypoint, Cmd, Env,
WorkingDir, User) in `/.fcvm/`. The kernel boots with `init=/.fcvm/init`.
`fc-init` mounts `/proc`, `/sys`, `/dev`, devpts, cgroup2 and friends, writes
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
lib/net.sh              host bridge/taps/NAT
init/fc-init.c          PID 1 for container VMs
kernel/microvm-*.config kernel fragment
bin/ kernels/ images/ vms/ cache/ build/   generated
```

## Limits and next steps

- x86_64 only for now. aarch64 needs a `kernel/microvm-aarch64.config`.
- Published ports are TCP only, and the client address is not preserved.
  If you need either, add an nftables DNAT rule in `net.sh` (root).
- Private registries: set `REGISTRY_USER` / `REGISTRY_PASSWORD`.
- For production isolation, run Firecracker under `bin/jailer`.
