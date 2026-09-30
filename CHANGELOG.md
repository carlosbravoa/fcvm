# Changelog

fcvm follows [semantic versioning](https://semver.org/). Before 1.0, minor
releases (0.x.0) may change commands and on-disk formats; the notes say how
to move across.

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
