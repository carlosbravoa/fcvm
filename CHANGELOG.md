# Changelog

fcvm follows [semantic versioning](https://semver.org/). Before 1.0, minor
releases (0.x.0) may change commands and on-disk formats; the notes say how
to move across.

## 0.5.2 (2026-09-30)

Web console improvements. Upgrade with `fcvm upgrade`, then run
`fcvm service install` so the console serves the new version.

- Web console: a "jailed" tag next to jailed VMs in the Instances list and
  the dashboard, and on the instance page. Instance rows stay on one line.
- Web console: titled "fcvm Web Manager". The bottom of the sidebar shows
  the user and host it runs as, and the fcvm version (linking to the
  changelog). The Instances page no longer repeats the Launch button that's
  already in the top bar.

## 0.5.1 (2026-09-30)

Upgrade with `fcvm upgrade` (or re-run the installer). Then:
- run `fcvm service install` and `fcvm jail-setup` if you use them, to
  refresh their root-owned copies (`fcvm status` lists what's needed);
- restart running VMs to pick up the new init, which is rebuilt
  automatically.

**Tests and CI**
- `tests/run`: lint (syntax of every script, shellcheck, docs links), unit
  tests (no KVM needed), and integration tests with real VMs. The
  integration tests only touch `fcvmtest-*` objects.
- `tests/fresh-machine.sh`: the whole thing on a fresh Ubuntu in Multipass,
  including a reboot.
- GitHub Actions runs lint, unit and integration tests on every push.
  See CONTRIBUTING.md.

**Setup**
- `fcvm setup` offers a choice of first images: Alpine, Ubuntu, Debian,
  Python and Node container images, and the Ubuntu 26.04 system image. Any
  number can be picked, and the test VM boots from the first one. Alpine is
  the default.
- The installer says clearly when `~/.local/bin` isn't on your PATH. It
  checks whether your profile adds it at the next login, and otherwise
  offers to add it to your shell's rc file. `fcvm setup` also warns at the
  end if `fcvm` isn't on your PATH.

**Web console and host directories**
- Launch: the "Process" options are hidden for system images, which boot
  systemd. They used to show, and "Stay idle" then failed the launch. The
  server ignores them for system images too.
- Host directories take `~` on both sides, and the guest path is optional:
  `-v ~/project` mounts your folder at the same place in the image user's
  home (`/root/project`, `/home/app/project`), and a directory outside your
  home at the same path. `umount` takes the guest path or the host
  directory. This works the same in the CLI (`-v`, `mount`) and in the web
  console's launch form and Files tab.
- A new icon: a flame inside small walls.

**Fixes**
- Ubuntu 24.04 works. Building disks from tarballs (every import, every new
  VM's layer, the base image, builds) needs e2fsprogs 1.47.1, and 24.04 has
  1.47.0. There, `lib/tar2ext4.py` unpacks the tarball as you, builds from
  the directory, and writes the real owners, modes, device nodes and file
  capabilities with debugfs. No privileges or user namespaces are needed,
  which 24.04 restricts. `fcvm base` (mmdebstrap) still needs unprivileged
  user namespaces there, and says how to allow them for the build.
- VMs unmount their disks cleanly at shutdown. A volume used read-write
  used to be left needing journal recovery, so the next VM that mounted it
  read-only failed to boot. Read-only mounts of such a disk now also fall
  back to skipping recovery instead of halting.
- Snapshots and forks work on nested hosts (cloud VMs, Multipass, CI
  runners). Restores relied on Firecracker's `clock_realtime`, which needs a
  TSC-clocked host, so every fork failed there. Forks now get the host's
  time from the exec agent instead.
- `fcvm status` no longer stops halfway when no version information is
  cached (offline, on a new machine).
- Errors from commands run by the fcvm service keep their detail, e.g.
  Firecracker's own message, instead of just the last line.
- Hosts with Docker: Docker's FORWARD policy (DROP) no longer cuts
  full-network VMs off. `net-up` adds exceptions to Docker's `DOCKER-USER`
  chain, and the boot-time network unit starts after Docker.

## 0.5.0 (2026-09-30)

The first versioned release. Earlier work, unversioned, is summarized here.

**Installing and running**
- An installer (`install.sh`): per user in `~/.local` (no root), or
  `--system` in `/opt/fcvm`. Each release gets its own directory.
  `fcvm upgrade` moves to a newer one (or back).
- Code and state are separate. State (images, VMs, kernels, ...) lives in
  `~/.local/share/fcvm` (`FCVM_HOME`), settings in
  `~/.config/fcvm/fcvm.conf`. A git checkout that already holds state keeps
  using it.
- `fcvm version` / `-V`.
- `fcvm setup`: a guided, interactive setup.
- `fcvm status`: a health check with component versions, available updates
  and fixes.
- The initramfs is rebuilt automatically when its source changes, so VMs
  never boot an init older than the host side.

**VMs and images**
- OCI images from any registry, digest-verified (private registries via
  `REGISTRY_USER`/`REGISTRY_PASSWORD`), local `docker save` and OCI
  archives. Imports are atomic.
- App images (a container's command as PID 1) and system images (systemd),
  including a rootless-built Ubuntu 26.04 base.
- Shared read-only images with a writable layer per VM, `commit`, `squash`,
  Dockerfile builds cached per step.
- `run`, `create`/`start`/`stop`, `exec`, `shell`, `cp`, consoles, logs,
  published ports, named volumes, and live host directories (9P over vsock).
- Snapshots of running VMs, and forks in ~150 ms.
- Restart policies (`no`, `on-failure`, `unless-stopped`, `always`), and a
  boot-time service that brings VMs back after crashes and reboots. With the
  service running, long-lived VMs belong to it, so they're stopped cleanly
  at shutdown.

**Isolation**
- An optional Firecracker jailer: per-VM uid, chroot, cgroup v2 limits,
  network namespace, through a validating root helper.
- VMs are isolated from each other and from host services, with
  anti-spoofing and no guest IPv6. Published ports bind to localhost.
- Egress allowlists with presets through a logging proxy, or no network at
  all.

**Interfaces**
- A web console with dashboard, charts, terminals, file browser and image
  builds.
- An HTTP API with bearer tokens.
- An MCP server for agents, jailed by default, with a pinned network policy.

**Found by testing on fresh machines, and fixed**
- `host-setup` installs libarchive, without which `mkfs.ext4 -d` (every
  import and build) failed on a fresh host.
- `setup` works with passwordless sudo.
- Ubuntu system VMs resolve their own hostname (no more sudo warnings).
- Socket paths are checked up front, with a clear message, instead of
  Firecracker's "path must be shorter than SUN_LEN".
- The guest kernel includes `NFT_REJECT` and `NF_TABLES_BRIDGE`, so fcvm's
  own network setup runs inside a VM (for testing).

**Notes for existing checkouts**
- A checkout with state inside keeps working unchanged. To use `fcvm`
  without `./`, link it onto your PATH:
  `ln -s "$PWD/fcvm" ~/.local/bin/fcvm`. Don't also run the installer: it
  would create a second, empty state directory, while the service, jailer
  and network serve one.
- To move a checkout's state to `~/.local/share/fcvm`:
  1. stop all VMs;
  2. move `bin kernels build cache images vms volumes snapshots ssh builds`
     there, and `fcvm.conf` to `~/.config/fcvm/`;
  3. re-run `fcvm jail-setup` and `fcvm service install` if you use them.
