# Changelog

fcvm follows [semantic versioning](https://semver.org/). Before 1.0, minor
releases (0.x.0) may change commands and on-disk formats; the notes say how
to move across.

## 0.11.0 (2026-10-27)

A host can join a control plane. `fcvm node join URL --token TOKEN`
enrolls it once; the fcvm service then keeps one outbound session with
the control plane, which drives the host through the HTTP API as if it
were local: for each request it asks for a work connection, and the
service pipes a fresh outbound WebSocket to its own `127.0.0.1` API.
Nothing listens on the node, no agent is installed, and a cloud image
runs the command from cloud-init. `fcvm node status` and `fcvm node
leave`; `fcvm status` shows the session, `fcvm status --json` carries
`control_plane`; `NODE_ORPHAN_CMD` lets a node its control plane forgot
end itself. The protocol (version 1) is in [the node
contract](docs/node-contract.md#joining-a-control-plane); the contract
version stays 1 (additions only). New error codes `node_joined`,
`node_not_joined`, `join_refused`. After `fcvm upgrade`, `fcvm service
install` so the service runs the session.

Also:
- **A new kernel build has to boot before new VMs get it.** `fcvm kernel`
  pointed `kernels/vmlinux` at every build that compiled, even one whose
  `.config` had lost options of the fragment (a renamed symbol or a new
  dependency in a newer kernel), so one bad build broke every VM started
  after it. Now the switch needs every fragment option and a throwaway VM
  that boots on the build; otherwise `kernels/vmlinux` stays where it was
  and `fcvm kernel` fails, saying why. `--no-check` switches anyway.
  Switching to a version already built runs the same checks. See [The
  guest kernel](docs/configuration.md#the-guest-kernel).
- `fcvm kernel --help` went looking for a kernel called `--help` on
  kernel.org. It prints the usage now, and an argument that's neither a
  channel nor a version is refused.
- The docs said `kernels/vmlinux` points at the newest build; it points at
  the one `fcvm kernel` last switched to. They say so now, and why a new
  release such as 7.3 is `mainline` on kernel.org until its first point
  release.

## 0.10.0 (2026-10-20)

The second half of a host that serves several tenants: what a tenant
makes is its own, disk counts against its quota, its package cache is
its own, and `NODE=1` turns a host into a node that refuses what would
put the host itself in a tenant's hands. As before, a host with no
tenants and no `NODE` setting sees no change. After `fcvm upgrade`,
`fcvm service install` so the API serves the new endpoints.

- **Tenants own their images, snapshots and volumes.** `commit` and
  `snapshot` give the result the VM's tenant; `import`, `build`, `volume
  create` and `snapshot import` take `--tenant`; a volume `-v` creates on
  first use is the VM's tenant's. A VM of another tenant, or of none,
  that names one of them is refused with the new code `policy` (HTTP
  403), and a tenant's snapshot forks for that tenant only (the forks
  take its label). `images`, `snapshot ls` and `volume ls` show the
  tenant and take `--tenant`, as do `GET /api/images|snapshots|volumes`;
  `POST /api/volumes` and the SDKs' `create_volume` are new; `tenant
  show` lists what a tenant owns. See [Tenants and
  quotas](docs/vms.md#tenants-and-quotas).
- **Disk in quotas.** `tenant set --disk MiB` limits the nominal size of
  everything a tenant has: VM layers (stopped ones too), images,
  snapshots and volumes, checked before any of them is created
  (`quota_exceeded`). `NODE_DISK_MIB` is the host's capacity for it
  (default: the state's filesystem), enforced with `NODE_ADMIT=1`.
- **A package mirror per tenant.** A tenant's VMs fill and read their own
  cache (`cache/egress/tenants/NAME`), so one tenant's installs neither
  reveal anything to another nor serve it files it didn't fetch. The
  egress proxy is now replaced at the next VM start after an upgrade.
- **Node mode.** `NODE=1` in `fcvm.conf` pins admission, jailing and
  isolation on, refuses host paths outside `NODE_PATHS` (`-v HOST/DIR`,
  `mount`, `cp`, `--secret @FILE`, build contexts, local imports,
  exports), `--forward`, `--no-jail` and unjailed VMs or snapshots, and
  gives new VMs a restricted network unless told otherwise;
  `NODE_NETWORKS` limits the modes a host accepts. Enforced in the CLI,
  so the API, the MCP server and scripts obey. `--net restricted` (an
  empty allowlist, filled later with `fcvm egress --allow`) is new for
  everyone, and templates and the API accept the same. See [Node
  mode](docs/configuration.md#node-mode).
- `fcvm status --json` adds `node_mode`, `objects` and the host's disk;
  `fcvm status` says when node mode or admission is on.

## 0.9.0 (2026-10-20)

A host that several teams, customers or agents share: VMs say whose they
are, each tenant has a quota, the host knows its own capacity, and the
event stream says who ran what. All of it is opt-in: nothing changes for
a host with no tenants and `NODE_ADMIT` unset. After `fcvm upgrade`,
`fcvm service install` so the API serves tenants and the node view.

- **Tenants and quotas.** `--tenant NAME` on `create`, `fork` and
  `pool claim` sets the `tenant` label, which `update` can't change;
  `fcvm tenant set NAME --vcpus N --mem MiB --vms N` gives the tenant a
  quota, checked whenever one of its VMs starts, is forked or is claimed
  (`quota_exceeded`, HTTP 429). `fcvm tenant ls|show|rm`, `ls --tenant`,
  `GET/PUT/DELETE /api/tenants`, `?tenant=` and `?label=` on `GET /api/vms`,
  and `tenant` on the SDKs' `create`, `fork` and `claim`. See
  [Tenants and quotas](docs/vms.md#tenants-and-quotas).
- **The host's capacity.** `NODE_VCPUS`, `NODE_MEM_MIB` and `NODE_VMS`
  (computed from the host by default) are reported by `fcvm status --json`
  and `GET /api/host` (`node`), and enforced with `NODE_ADMIT=1`
  (`node_full`, 503). Off by default.
- **`fcvm status --json`** is the node view (capacity, allocated,
  allocatable, tenants, free slots, versions), and **`fcvm config
  [--json]`** prints the settings in effect and the paths, so programs
  never parse `fcvm.conf`.
- **Events** for pools (`pool.set`, `pool.removed`, a `tenant` on
  `pool.claimed`) and tenants (`tenant.set`, `tenant.removed`).
- **Audit.** Every event a command records carries `argv`, the command
  line as fcvm received it with `--secret` values masked, next to `by`
  (who). `fcvm events --by ACTOR --type TYPE`, and the same filters on
  `GET /api/events` and the MCP `events` tool. See
  [Audit](docs/events.md#audit).
- **Fixed:** a failure relayed from the service (`start`, `fork`,
  `publish` through `fcvm serve`) now ends with its `fcvm-error: CODE`
  line like a local one; `POST /api/vms` answers with the start's own
  code and status when the VM was created but refused at its start.
- `tests/fresh-ec2.sh` runs on a `c8i.2xlarge` with nested virtualization
  instead of a bare-metal instance.

## 0.8.0 (2026-10-19)

Groundwork for driving fcvm from a program, and better answers for
people too: every failure has a code, every state change leaves an event,
slow commands can run detached, a VM says why it's in its state, and VMs
can carry labels and leases. Nothing changes for existing VMs or
commands; after `fcvm upgrade`, `fcvm service install` so the supervisor
applies leases and the console serves events. [The node
contract](docs/node-contract.md) lists what outside programs may rely on
from here (contract version 1).

- **Error codes.** Every failure now has a code (`vm_not_found`,
  `vm_running`, `image_exists`, ... see [docs/errors.md](docs/errors.md)).
  The CLI prints it on a `fcvm-error: CODE` line after the `error:` line
  when stderr isn't a terminal; the API replies `{"error", "code"}` with a
  status that follows the code (404 for things that don't exist, 409 for
  state conflicts, 503 when the host can't take the VM) instead of 400 for
  everything; `FcvmError` in both client libraries has `.code`. Scripts that
  matched the API's 400 for "no such VM" or "already running" see 404 and
  409 now.
- **Spec, status and labels.** `fcvm inspect` (and the API's VM object)
  has `spec`, the create request as given; `status` with the `reason` for
  the VM's state (`user`, `ttl`, `idle`, `exit`, `shutdown`, `crash`,
  `created`) and `since` when; and `labels`, tags of your own
  (`create --label team=web`, `update --label` / `--unset-label`,
  `ls --label team=web`, `labels` in templates, project files and the API,
  also on `fork` and `pool claim`). `create --if-same-spec` (API:
  `if_same_spec`) makes a repeated create succeed when the VM exists with
  the same spec, so scripts and control planes can retry safely.
- **Events.** Every state change is recorded in `vms/.events.jsonl`
  with a sequence number: `vm.created`, `vm.started`, `vm.stopped` (with
  why: `user`, `ttl`, `idle`, `system`), `vm.exited` (`exit`, `shutdown`,
  `crash`), `vm.restarting`, `vm.updated`, `vm.deleted`, `pool.claimed`,
  each saying who acted. `fcvm events [--since N] [-f] [VM]` shows them,
  `GET /api/events` returns or streams them (server-sent events), the
  console redraws on them at once, and the MCP server has an `events`
  tool. See [docs/events.md](docs/events.md).
- **Operations.** `import`, `build`, `export`, `push`, `snapshot` and
  `commit` take `--op`: the command runs detached, prints an id, and
  `fcvm ops [ID] [-f]` shows its state and output, kept in `vms/.ops/` for
  a week. The console's jobs (`GET /api/jobs`) are these operations now:
  their ids are strings, they survive a restart of the service (one cut
  short is `failed` with `interrupted`), and `GET /api/jobs/{id}?wait=S`
  blocks until the job ends. An `operation.done` event closes each one.
- **Leases and cordon.** `--lease UNTIL` on `create`, `start`, `fork` and
  `pool claim` (and `update --lease`, `lease` in the API, `POST
  /leases/renew` for every VM with a label) says until when a VM is
  wanted; once that passes by `LEASE_GRACE` (300 s), the supervisor stops
  and removes it. For programs that manage VMs and might forget them.
  `fcvm cordon [REASON]` / `uncordon` make a host refuse new VMs (code
  `cordoned`) while running ones and restart policies carry on, for
  draining it; the console shows the state.
- **Packages.** The clients are on PyPI and npm as `fcvm` (`pip install
  fcvm`, `npm install fcvm`). `fcvm-mcp` on PyPI launches the installed
  fcvm's MCP server (`uvx fcvm-mcp`), and lists fcvm in the MCP Registry.
  `tests/run lint` checks that every package carries `VERSION`.

## 0.7.0 (2026-10-11)

A release for agents and for moving things around: sandboxes that end on
their own, background commands, live ports, secrets and forwarded
credentials, a package mirror, warm pools, export and push of images and
snapshots, project files, and client libraries. After `fcvm upgrade`:
- `fcvm service install`, so the supervisor applies lifetimes and fills
  pools; `fcvm jail-setup`, so jailed VMs get a balloon device;
- running VMs keep working; secrets, forwarding and the mirror take effect
  at their next start (the initramfs is rebuilt then).

New:

- **Lifetimes.** `create`/`run --ttl 2h` stops a VM that long after it
  boots; `--idle-stop 30m` stops it after that long without `exec`,
  `shell`, `cp` or file operations. `fcvm update` changes them; `ls`,
  `inspect` and the console show them, and `inspect` says `expired: "ttl"`
  or `"idle"` after one fired. Templates take `ttl` and `idle_stop`, the API
  takes them in `POST /vms` and `/update`, and the MCP server's
  `create_sandbox` takes them too, with `FCVM_MCP_TTL` and
  `FCVM_MCP_IDLE_STOP` as defaults for sandboxes the agent forgets about.
  Applied by the supervisor in `fcvm serve`, like restart policies.
- **Live ports.** `fcvm publish VM [BIND:][HOST]:GUEST` and `fcvm unpublish
  VM HOSTPORT` add and remove published ports on a running VM, and keep the
  change for the next start; without a host port a free one is picked and
  printed. The console's instance page, the API (`POST`/`DELETE
  /vms/{vm}/ports`) and the MCP server (`publish_port`, which returns a
  URL, and `unpublish_port`) have the same. Each published port now has
  its own forwarder.
- **Background commands.** `fcvm exec -d VM CMD` starts a command as a job
  and prints its id; `fcvm jobs VM` lists them, `fcvm jobs VM ID [-f]`
  shows or follows the output, `--kill` ends one. The API gets
  `POST /vms/{vm}/exec` (also for ordinary commands, with `exit_code`,
  `stdout` and `stderr`) and `/vms/{vm}/jobs`; the MCP server's `exec`
  takes `background`, with `jobs`, `job` and `kill_job` tools. An agent
  can now start a server, publish its port and talk to it.
- **Secrets.** `create --secret NAME=VALUE | NAME=@FILE | NAME` (from your
  environment) hands a secret to the VM at boot over vsock, into a tmpfs
  at `/run/secrets/NAME` and the environment of the app and of `exec`
  sessions, never onto its disk, so `commit` can't leak it. `update
  --secret` / `--unset-secret` change them. Templates name them, the API
  takes `secrets` and `unset_secrets`, and the MCP server passes the
  secrets named in `FCVM_MCP_SECRETS` from its own environment, with no
  tool for the agent to see or set them. Needs the initramfs of this
  release (rebuilt at the next VM start).
- **Credential forwarding.** `create --forward ssh-agent,git` relays the
  host's ssh-agent into the VM (`SSH_AUTH_SOCK` for the app and `exec`
  sessions) and answers the VM's git credential lookups with the host's
  `git credential fill`, so cloning and pushing work with no key or token
  inside the VM. Logged in `vms/VM/forward.log`. Templates, the API
  (`forward`) and the MCP server (`FCVM_MCP_FORWARD`) take it too.
- **Memory balloon.** Every VM gets a balloon device; `fcvm balloon VM
  MiB` takes memory back from a running VM (the host frees the pages),
  `0` returns it, and without a size it shows the balloon and the guest's
  memory view. `inspect` has `balloon_mib`; the API `GET`/`POST
  /vms/{vm}/balloon`; the console a row on the instance page. Jailed VMs
  need `fcvm jail-setup` re-run for the helper to configure the device.
- **Export and push.** `fcvm export IMAGE FILE.tar` writes an image (its
  whole chain flattened into one layer, with owners, links, devices and
  capabilities) as an OCI image archive for `docker load`, `podman load`
  or `fcvm import` on another host; `fcvm push IMAGE REGISTRY/REPO:TAG`
  uploads it to a registry. The tree is read inside a helper VM (`fc-init`
  export mode), no root needed.
- **Snapshot export and import.** `fcvm snapshot export NAME FILE.tar`
  packs a snapshot (memory, kept sparse; device state; disk; settings) and
  `fcvm snapshot import FILE.tar [NAME]` brings it to another host that has
  the same images and a compatible Firecracker, ready to fork: a prepared,
  already-running state can be shipped.
- **Client libraries.** `sdk/python/fcvm.py` (standard library, `pip
  install` from the repository or copy the file) and
  `sdk/typescript/fcvm.mjs` (Node 18+, no dependencies, typed) wrap the
  HTTP API: `Client().create("box", template="python-sandbox")`, then
  `exec`, files, ports, jobs, snapshots, pools.
- **Warm pools.** `fcvm pool set NAME --template T --size N` (or
  `--snapshot S`) keeps N VMs booted ahead of time; `fcvm pool claim NAME`
  hands one out in milliseconds, name and all, and the supervisor refills
  the pool. The MCP server claims from a template's pool on its own, so
  `create_sandbox` is instant; the API has `/pools`.
- **The package mirror.** `--cache` on a restricted VM (`--allow`) points
  its pip and npm at a mirror on the egress proxy
  (`http://172.30.1.1:3128/cache/HOST/PATH`), which fetches over TLS with
  the allowlist applied, keeps wheels, tarballs and other immutable files
  in `cache/egress/`, and rewrites index links so everything goes through
  it. `fcvm egress` shows `CACHE-HIT`/`CACHE-MISS`; `fcvm prune` empties
  it. Templates (`cache`), the API and the MCP server (`FCVM_MCP_CACHE=1`)
  take it. A running proxy from an older fcvm is replaced at the next
  restricted start.
- **Project files.** `fcvm.toml` in a repository describes its VMs
  (`[vms.NAME]` tables with the template fields, a `template` to start
  from, host paths relative to the file); `fcvm up` imports, creates and
  starts them, `fcvm down` stops and removes them, `fcvm project show`
  explains the file. See [Project files](docs/projects.md).
- **`fcvm ssh-config`.** An ssh client entry per system VM (`Host
  fcvm-NAME`) with a `ProxyCommand` that finds the VM's current address,
  so `ssh fcvm-dev`, `scp`, `rsync` and VS Code Remote-SSH work with strict
  host-key checking and survive restarts. `--install` puts it in
  `~/.ssh/config`.

Fixes from a code review:

- Live host directories stopped working after about 50 minutes: the 9P
  server quit while the VM was still running.
- On hosts where NetworkManager runs, it adopted fcvm's bridges and could
  drop their addresses, cutting every VM off from the host and the internet.
  `net-up` now marks fcvm's interfaces unmanaged
  (`/etc/NetworkManager/conf.d/fcvm.conf`), always puts the bridge
  addresses back, and `fcvm status` reports a bridge without its address.
- Hosts booted with `ipv6.disable=1` failed `net-up` and every jailed launch.
- `fcvm import` of an untrusted image could write outside the work directory
  through a symlink in a layer (the fallback unpack path on e2fsprogs older
  than 1.47.1).
- A restricted VM could start without its egress proxy when the proxy's
  recorded pid belonged to something else after a reboot.
- A web console job whose output had a very long line (progress bars) stayed
  "running" forever.
- `fcvm base` deleted the existing Ubuntu image before building its
  replacement; it now builds under a temporary name like `import`. Its
  AppArmor hint on failure never showed.
- The installer's `--source DIR` copied `.git` and state directories from a
  checkout; `--help` printed two lines of code.
- `host-setup`, `setup` and `status` broke under `su`, cron or `sudo -u`
  (`$USER` unset).

And from a second review of this release's own changes, before it shipped:

- The mirror could be made to fetch from a host outside the VM's allowlist
  (a host string a URL reads differently, such as `evil.com?x.pypi.org`,
  or an allowed host redirecting elsewhere): the host must now be a plain
  name, and redirects are followed only to allowed hosts.
- `--forward ssh-agent` on a system image: systemd's own `/run` hid the
  relay socket unless a secret was set too.
- `fcvm import` could still create directories outside the work directory
  through a symlinked parent (the check ran after `makedirs`).
- npm's package metadata was never rewritten to the mirror, so `--cache`
  cached nothing of npm's.
- `fcvm push` asked for a pull-only token when the registry's challenge
  named no scope, and failed with "authentication failed".
- A job whose command a signal killed inside the VM (exit 137 from an
  OOM, say) was listed `lost`; only a dropped connection or `--kill` is.
- The mirror left a temporary file behind for every failed upstream fetch,
  and a truncated upstream body crashed the request.
- The client libraries now find the server of an fcvm run from a checkout
  (`fcvm _home` tells them where its state is).
- `fcvm update --ttl` printed the value twice ("7200s7200"); `fcvm fork`
  through the service warned about published ports twice.

## 0.6.2 (2026-10-02)

Provenance for the vendored web console files. Nothing to run after
`fcvm upgrade`; nothing about how fcvm behaves changes.

- The minified xterm.js files in `lib/web/static/vendor` now come with a
  README naming the npm package and version each comes from (`@xterm/xterm`
  6.0.0, `@xterm/addon-fit` 0.11.0), links to the readable upstream source,
  and commands to check they are identical to upstream. Their hashes are in
  `SHA256SUMS`, checked by the unit tests, and the directory is marked
  `linguist-vendored` so code scanners treat it as third-party.

## 0.6.1 (2026-09-30)

Per-VM SSH identity. Upgrade with `fcvm upgrade`; nothing else to run.

- SSH keys are no longer baked into the Ubuntu base image. Each system VM
  gets its `authorized_keys` (fcvm's key and your `~/.ssh/id_*.pub`) and its
  own host key at `create`, and `fcvm ssh` checks that host key strictly
  instead of skipping the check. `commit` also scrubs `authorized_keys`.
  VMs created earlier keep working; `fcvm ssh` warns that it can't check
  their host key. An existing base image still holds the old
  `authorized_keys`, but each new VM's own file replaces it; a base image
  built from now on has none.

## 0.6.0 (2026-09-30)

Metrics from inside your VMs, and launch templates. Upgrade with
`fcvm upgrade`, then run `fcvm service install` so the console serves the
new version, and restart running VMs to get guest metrics (the new init is
rebuilt automatically). Nothing on disk changes format.

- Metrics from inside each VM, through the exec agent: CPU, memory, root
  disk, load and processes, in any image. The instance page charts them
  next to the host-side charts, with 10 minute, 1 hour, 6 hour and 24 hour
  ranges; one-minute history is kept for 24 hours and survives restarts.
- Web console: a Processes tab per instance, like `top`.
- A Prometheus endpoint, `GET /metrics`, with the API's bearer token.
- Templates: saved launch recipes. `fcvm create VM --template NAME`,
  `fcvm run --template NAME`, and `fcvm template ls|show|save|rm` (save
  from an image and options, or `--from` an existing VM). Built in:
  `python-sandbox`, `node-sandbox` and `offline-shell`. A template's image
  is imported on first use.
- Web console: a Templates page, a template picker in the launch dialog,
  and Save as template. API: `GET/POST /templates`, `DELETE /templates/NAME`.
- MCP: a `templates` tool, `create_sandbox` takes `template`, and
  `FCVM_MCP_TEMPLATE` sets the default.
- `create --no-agent` (command line only): a VM without fcvm's exec agent,
  for the rare case where nothing inside may accept commands. It lists what
  you lose (exec, cp, snapshots, guest metrics...) and asks first. Commands
  that need the agent refuse clearly, and the console hides its Shell,
  Files and Processes tabs for such VMs.

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
