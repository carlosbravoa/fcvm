# Roadmap

What fcvm could do next, and what has been done from earlier plans. The
[documentation index](README.md) covers what exists today.

Priorities come from a review of fcvm from two angles: as a sandbox for
agentic development (compared with Multipass), and as something an enterprise
could adopt. Orchestration is out of scope for now. Items marked ✅ are done.

## Agentic development

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
  forked into running VMs in ~150 ms (about 1 s for jailed VMs, which get a
  fresh jail), each with its own disk, IP, MAC and hostname, and RNG
  reseeding through VMGenID. Still open: diff snapshots
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

## Web console

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
- **W6. Service mode.** ✅ `fcvm service install`: the network and
  `fcvm serve` at boot (a system unit running as you). VMs with a restart
  policy come back after a reboot or crash (with E4).
- **W7. Metrics.** ✅ Guest-level metrics through the exec agent (CPU,
  memory, disk, load and processes inside the VM, in any image), a
  Processes tab, 24 hours of one-minute history that survives restarts, and
  a Prometheus endpoint (`/metrics`).
- **W8. Templates.** ✅ Saved launch recipes (built-in Python, Node and
  offline sandboxes, and your own) shared by the CLI (`--template`,
  `fcvm template`), the web console (a picker, Save as template, a
  Templates page) and the MCP server (`template`, `FCVM_MCP_TEMPLATE`).
- **W9. Fleet view.** Several fcvm hosts in one console: a separate
  control-plane product using `fcvm serve` as the per-host agent.

## Enterprise

Prior art to position against: Weave Ignite (Docker image → Firecracker VM,
archived 2023), Fly.io (the same idea as a platform), E2B (Firecracker
sandboxes for AI agents, as a service), Kata Containers and
firecracker-containerd (microVMs behind the container runtime interface).
fcvm's niche is self-hosted, simple and auditable: closer to Multipass than
to Kubernetes.

What is solid: a minimal monolithic guest kernel, digest-verified pulls with
`@sha256:` pinning, shared read-only images with per-VM layers, rootless
operation, and a small auditable code base. What blocks adoption, in order:

### E1. Run VMs under the Firecracker jailer ✅

Before this, Firecracker always ran as your user, with KVM and its seccomp
filters as the only boundary. Now `fcvm create --jail` / `JAIL=1` runs a VM
under Firecracker's jailer, through `fcvm-jaild`, a root helper installed by
`fcvm jail-setup`. Details are in [docs/security.md](security.md#jailed-vms-in-detail).

**What's done:**
- **Validated requests.** The helper checks every path a launch names
  against the owner's fcvm tree, and runs root-owned copies of the jailer
  and Firecracker.
- **Per VM:**
  - its own uid/gid;
  - a chroot with its disks bind-mounted in, images read-only;
  - cgroup v2 limits (CPU, memory, pids) and an open-files limit;
  - its own network namespace, with the tap inside and a veth to the
    bridge;
  - access to your files by ACL, so every CLI command keeps working.
- **Snapshots and fork** of jailed VMs.
- **Stats.** Disk I/O comes from the cgroup and traffic from the veth.
- **Cleanup** on exit, and a sweep of leftovers at helper startup.
- **Jailed by default** for MCP sandboxes and in the web console. The CLI
  stays opt-in.

**Decided against:** `--new-pid-ns`. fcvm needs the VMM's real pid, and the
per-VM uid already prevents signalling or ptracing other processes.

### E2. Network isolation and policy ✅

Done (details in [docs/security.md](security.md#network-isolation)):
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

### E3. Supply chain

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

### E4. Daemon and API ◐

**Done:**
- **The daemon.** `fcvm serve` is it: web console, JSON API (bearer
  tokens for scripts, [docs/api.md](api.md)), and a supervisor.
- **Recovery.** The supervisor recovers after a crash or reboot: stale VMs
  are recognized by boot id and process start time, and reaped without
  touching reused pids.
- **Restart policies**, Docker-style: `no`, `on-failure`, `unless-stopped`,
  `always`, with backoff.
- **Clean shutdown.** VMs stop cleanly at host shutdown and resume at boot.
- **Per-VM locks** keep your commands and the supervisor from racing.
- **At boot.** `fcvm service install` runs the network and the daemon at
  boot (W6).

**Still open:**
- **A state store.** State is still JSON files, pid files and markers per
  VM, which is simple and inspectable, but gives no transactions or history.
- **Remote and multi-user access** (with W2): TLS, real accounts, roles.
- **Events.** An event stream (VM started, exited, restarted) for the
  console and for audit (E5).
- **The long-term move.** The control plane may move to Go or Rust once
  orchestration is on the table, while `fc-init` stays small and static.

### E5. Audit and observability ◐

Done: metrics (W7). `GET /metrics` exports host, per-VM and in-guest
numbers in the Prometheus format, and the console keeps 24 hours of
history.

Still open:
- a log of `exec`/`shell`/`console` sessions (who ran what, and when), with
  W5;
- shipping console logs somewhere central;
- Firecracker's own metrics (`--metrics-path`), if something beyond the
  process and guest views turns out to be needed.

### E6. Resource governance

Jailed VMs have cgroup v2 limits on CPU, memory and pids (E1). Still open:
- limits for rootless VMs (a user-level cgroup through systemd);
- Firecracker rate limiters for block and network I/O;
- quotas on writable layers and volumes (today they are sparse files that
  can fill the host disk);
- disk encryption at rest.

### E7. Credentials in images

The Ubuntu base image contains the project SSH key and the builder's public
keys, and `fcvm ssh` skips host-key checking. That is fine on a laptop, not in
shared images. Keys should be injected per VM at boot instead.

### E8. Engineering maturity

- ✅ `fc-init` boots from an initramfs instead of living in every image, so
  upgrading the init or agent no longer means rebuilding images.
- ✅ A test suite and CI (see [CONTRIBUTING](../CONTRIBUTING.md)):
  - `tests/run`: lint (with shellcheck and a docs link checker), 67 unit
    tests, and 52 integration tests on real VMs, in about 3 minutes;
  - `tests/fresh-machine.sh`: install, setup, the suite and a real reboot on
    a fresh Ubuntu in Multipass;
  - GitHub Actions: lint and unit tests, and integration tests on a fresh
    runner with KVM.

  Its first runs found real bugs:
  - volumes were left needing journal recovery after a VM stopped, so a
    read-only mount of them failed;
  - snapshots couldn't be restored on nested hosts (cloud VMs, CI);
  - `status` stopped halfway with nothing cached;
  - Docker's FORWARD policy cut VMs off.

  Groundwork from 0.5.0:
  - `fcvm setup -y` and the installer's `--source` make a fresh machine
    scriptable end to end;
  - manual runs in fresh Multipass VMs (QEMU, nested KVM), including real
    reboots, found six bugs that a developer machine never showed;
  - fcvm's own `net-up` runs inside an fcvm VM, since the guest kernel has
    the nftables features it needs.

  Still open: failure-path and concurrency tests (killing things
  mid-operation), and running the reboot test in CI.
- Exec agent protocol: ◐ requests carry a version tag (`fcvm2`), and a
  mismatch fails with a clear error. Still open: version negotiation and a
  compatibility policy, so newer hosts can talk to VMs booted with an older
  initramfs.
- aarch64 (Graviton): needs `kernel/microvm-aarch64.config` and testing.
- Packaging: ✅ versioned releases, an installer (`~/.local` or
  `/opt/fcvm`), `fcvm upgrade`, separate code and state (0.5.0). Still open:
  a `.deb` built by CI for each tag (its dependencies replacing
  `host-setup`), and later an apt repository or PPA; checksums or
  signatures for release downloads (with E3).

### Smaller items

- Published ports are TCP only and don't preserve the client address. An
  nftables DNAT mode in `net.sh` (root) would fix both.
- ✅ `create --no-agent` (0.6.0), CLI only: a VM without the exec agent,
  for the rare case where nothing inside may accept commands. It says what
  you lose and asks first.
- `exec` via the agent has no auth beyond access to the VM's vsock socket.
  For jailed VMs, that socket lives in the chroot, where only you and the
  VM's uid can reach it; rootless VMs keep it in `vms/<vm>/`, protected by
  your file permissions. Enough for a single user. A multi-user setup (W2,
  E4) would need per-request authentication.
