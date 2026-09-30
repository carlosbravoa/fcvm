# Getting started

This page takes you from nothing to a working setup, and explains what
each step does.

**The short way.** Install fcvm, then let `fcvm setup` walk through the
steps below:

```sh
curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh
fcvm setup
```

`fcvm setup` works interactively:
- it checks what's already done, and skips it;
- it asks before each optional step;
- it does the sudo steps first, so the long kernel build runs unattended;
- it ends by booting a test VM.

It's safe to run again at any time (for example after an update), and
`fcvm setup -y` takes the default answer everywhere. Afterwards,
`fcvm status` shows the state of everything at a glance. The rest of this
page covers the same steps by hand.

- [Requirements](#requirements)
- [Installing fcvm](#installing-fcvm)
- [1. Host setup](#1-host-setup)
- [2. The network](#2-the-network)
- [3. Firecracker and the guest kernel](#3-firecracker-and-the-guest-kernel)
- [4. A first image and VM](#4-a-first-image-and-vm)
- [5. Optional: a full-OS image](#5-optional-a-full-os-image)
- [6. Optional: the jailer](#6-optional-the-jailer)
- [7. Optional: run fcvm at boot](#7-optional-run-fcvm-at-boot)
- [Where things live](#where-things-live)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Next steps](#next-steps)

## Requirements

- **x86_64 Linux with KVM.** Check with `ls -l /dev/kvm`. On a cloud VM,
  you need nested virtualization or a bare-metal instance. aarch64 isn't
  supported yet.
- **A Debian-family host** for the automatic setup (`host-setup` uses apt).
  fcvm is developed on Ubuntu 26.04, and CI runs on Ubuntu 24.04 LTS. On other distributions, install the
  equivalent packages by hand (listed in `lib/host-setup.sh`).
- **sudo** for the one-time steps: `host-setup`, `net-up`, and optionally
  `jail-setup` and `service install`. Everything else, including starting
  VMs, runs as your user.
- **Disk:**
  - about 2.5 GB for the kernel source and build tree (`cache/` and
    `build/`; `build/linux-*` can be deleted after a build);
  - a few hundred MB per image;
  - a few MB per VM, because VMs share their image and write to a sparse
    layer.

## Installing fcvm

**The installer** (`install.sh` in the repository) downloads the newest
tagged release and installs it:

| | code | command | needs |
|---|---|---|---|
| for you (default) | `~/.local/lib/fcvm/VERSION` | `~/.local/bin/fcvm` | nothing |
| `--system` | `/opt/fcvm/VERSION` | `/usr/local/bin/fcvm` | sudo |

```sh
curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh -s -- --system
curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh -s -- --version 0.5.0
```

- **Layout.** Each release gets its own directory, and a `current` link
  points at the active one. `fcvm upgrade` installs a newer release and
  switches over; the previous one is kept for going back
  (`fcvm upgrade 0.5.0`).
- **Offline.** `--source PATH` installs from a release tarball or directory
  you already have.
- **Your data.** The install holds only code. Your state (images, VMs,
  volumes, snapshots, kernels, Firecracker) lives in `~/.local/share/fcvm`
  (`FCVM_HOME`). Settings live in `~/.config/fcvm/fcvm.conf`. So
  upgrading or reinstalling never touches your data.
- **With `--system`**, every user runs the same code, each with their own
  state in their home. The network, jailer helper and service are set up
  per user, and serve one user at a time (see
  [Where things live](#where-things-live)).

**From a git checkout.** For development, run `./fcvm` from the clone, or
link it onto your PATH. A checkout that already holds state (from before
0.5) keeps using its own directory, as before. A fresh clone uses
`~/.local/share/fcvm` like an install.

```sh
git clone https://github.com/carlosbravoa/fcvm ~/src/fcvm
ln -s ~/src/fcvm/fcvm ~/.local/bin/fcvm
```

Use one or the other, not both: an installed copy and a checkout with its
own state would be two fcvms with separate state, while the network, the
jailer helper and the service serve one.

`fcvm version` shows the version, and where the code, state and settings
are.

## 1. Host setup

```sh
fcvm host-setup
```

This installs the build and runtime packages:
- the kernel toolchain;
- `mmdebstrap` and `uidmap`, for building images rootless;
- `e2fsprogs` and `libarchive`, which `mkfs.ext4 -d` loads to build disks
  from tarballs (every import and build);
- `jq`, `nftables`, `acl` and `python3`.

`fcvm host-setup --check` lists what's missing without installing anything.

It also adds you to the `kvm` group if `/dev/kvm` isn't accessible (log out
and back in afterwards), and gives you a subuid/subgid range for user
namespaces.

## 2. The network

```sh
fcvm net-up
```

This creates two bridges and the firewall rules around them:

| bridge | subnet | for |
|---|---|---|
| `fcbr0` | `172.30.0.0/24` | VMs with full network access, through NAT |
| `fcbr1` | `172.30.1.0/24` | restricted VMs (`--allow`), which can reach only the egress proxy |

Each bridge gets 64 persistent tap devices owned by you, which is why VMs
start without root. The rules (nftables) keep VMs from reaching each other
or services on your host, pin each VM to its own MAC and IP address, and
drop guest IPv6. [Networking](networking.md) explains the modes, and
[Security](security.md#network-isolation) explains the rules.

The bridges don't survive a reboot. Either run `net-up` again after each
boot, or install the service ([step 7](#7-optional-run-fcvm-at-boot)),
which does it for you.

## 3. Firecracker and the guest kernel

```sh
fcvm firecracker     # the latest release into bin/, checksum-verified
fcvm kernel          # builds kernels/vmlinux-X.Y.Z from kernel.org sources
```

`fcvm kernel` downloads the newest stable kernel, configures it from
`allnoconfig` plus a small fragment (`kernel/microvm-x86_64.config`), and
compiles a ~27 MB monolithic `vmlinux`. That takes a few minutes, once.
Every VM boots this kernel, whatever its image. To pick another kernel
series, set `KERNEL_CHANNEL=longterm` or `mainline`, or name a version
(`fcvm kernel 6.18.54`). [How it works](internals.md#the-guest-kernel)
has the details.

## 4. A first image and VM

```sh
fcvm import alpine:latest       # pull from Docker Hub; the image is named "alpine-latest"
fcvm run alpine-latest          # a shell in a throwaway VM
```

`run` creates a VM, boots it and attaches your terminal. When the image's
command (here `sh`) exits, the VM stops and is deleted. A few more to try:

```sh
fcvm run alpine-latest -- sh -c 'uname -r; exit 3'; echo $?   # the fcvm kernel, then exit code 3
fcvm create box alpine-latest --idle && fcvm start box       # a VM that stays up
fcvm exec box cat /etc/os-release
fcvm ls
fcvm stop box && fcvm rm box
```

The initramfs every VM boots with (`fc-init`) is built automatically the
first time you start a VM, and again whenever its source changes, for
example after an upgrade.

## 5. Optional: a full-OS image

```sh
fcvm base                       # builds the "ubuntu-26.04" system image (a few minutes)
fcvm create dev ubuntu-26.04 -v ~/src:/src
fcvm start dev && fcvm shell dev
```

Container images run a single command. A **system** image boots systemd,
with services, ssh, journald and timers, like a server or a Multipass VM.
[Images](images.md#app-and-system-images) explains the difference.

## 6. Optional: the jailer

```sh
fcvm jail-setup                                  # installs the fcvm-jaild helper (sudo)
fcvm create box alpine-latest --idle --jail
```

By default Firecracker runs as you. With `--jail`, each VM's Firecracker
runs as its own unprivileged uid, in a chroot, with cgroup limits and its own
network namespace. That's the setup Firecracker uses in production. Once the
helper is installed, the MCP server and the web console jail new VMs by
default. Re-run `jail-setup` after updating fcvm or Firecracker.
[Security](security.md#rootless-and-jailed-vmms) compares the two modes.

## 7. Optional: run fcvm at boot

```sh
fcvm service install            # sudo
fcvm service status             # units, console URL, API token
```

This installs two systemd units:
- **`fcvm-net`** sets up the network at every boot, so `net-up` is no
  longer needed after a reboot.
- **`fcvm`** runs `fcvm serve` as you: the web console, the HTTP API, and
  the supervisor that applies restart policies and brings VMs back after
  a reboot.

See [The fcvm service](service.md).

## Where things live

Your state is in `FCVM_HOME`: `~/.local/share/fcvm` by default, or the
checkout itself for a pre-0.5 checkout. `fcvm version` shows which. Inside
it:

```
bin/           firecracker, jailer
kernels/       vmlinux-X.Y.Z builds; vmlinux -> the one VMs boot
build/         initramfs, kernel build tree
ssh/           the key `fcvm ssh` uses for system images
cache/         downloads (kernel sources, image layers)
images/        NAME.ext4 + NAME.json per image
vms/           one directory per VM: writable layer, config, sockets, logs
volumes/       named volumes (NAME.ext4)
snapshots/     one directory per snapshot
builds/        build projects from the web console
```

**Settings** are in `~/.config/fcvm/fcvm.conf` (for a pre-0.5 checkout,
`fcvm.conf` in the checkout); see [Configuration](configuration.md).

**Socket paths.** VMs keep Unix sockets under `vms/`, and Linux limits
socket paths to 107 bytes. A very long `FCVM_HOME` path therefore leaves
little room for VM names; fcvm tells you if it's too long.

**One state directory per user.** The tap devices, the egress proxy's port,
the jailer helper and the service all serve one state directory at a time.
If you point `FCVM_HOME` somewhere else, re-run `jail-setup` and
`service install` from there.

Outside it:
- the network devices and firewall rules (`net-up`);
- if installed, `/srv/jailer` and `/usr/local/lib/fcvm` (the jailer
  helper);
- the systemd units in `/etc/systemd/system/fcvm*.service`;
- the service and helper settings in `/etc/fcvm`.

## Updating

`fcvm status` tells you what's out of date and the command to fix it:
- a newer kernel or Firecracker release;
- a kernel config or initramfs that changed since the build;
- a jailer helper or boot-time network script that no longer matches your
  checkout;
- a service running older code;
- VMs still running an older kernel or initramfs.

To update fcvm itself:

```sh
fcvm upgrade              # an installed release: the newest one (or: fcvm upgrade 0.5.1)
git pull                  # a git checkout
```

`upgrade` finishes by listing what the new release needs refreshed. The
initramfs is rebuilt automatically at the next VM start whenever its source
changed. Components, by hand:

```sh
fcvm firecracker          # optional: a newer Firecracker
fcvm kernel               # optional: a newer kernel; VMs pick it up at their next boot
fcvm jail-setup           # if installed: the helper runs root-owned copies, refresh them
fcvm service install      # if installed: refreshes the boot-time network script and units
```

The init and exec agent live in the initramfs, not in images, so updates
never require rebuilding images. Running VMs keep the version they booted
with until they restart. Snapshots are tied to the Firecracker version that
took them.

## Uninstalling

```sh
fcvm service remove       # if installed
fcvm jail-setup --remove  # if installed
fcvm net-down             # bridges, taps, firewall rules
```

Then remove the code and, if you want, your data:

```sh
rm -rf ~/.local/lib/fcvm ~/.local/bin/fcvm          # the install (--system: /opt/fcvm, /usr/local/bin/fcvm)
rm -rf ~/.local/share/fcvm ~/.config/fcvm           # your images, VMs, volumes, snapshots and settings
```

`/srv/jailer` is left in place by `jail-setup --remove`; delete it by hand
if you want.

## Next steps

- [Use cases](use-cases.md): walkthroughs of what people typically do
  with fcvm.
- [Running VMs](vms.md): the everyday commands in depth.
- [Commands](cli.md): every command and option.
