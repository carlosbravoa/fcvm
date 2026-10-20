# Commands

Every `fcvm` command, grouped by purpose. `fcvm help` prints the same
list in short form. Pages under "Using fcvm" in the
[documentation index](README.md) explain each area in depth.

- [Host setup](#host-setup)
- [Building blocks](#building-blocks)
- [Images](#images)
- [VMs](#vms)
- [Projects](#projects)
- [Templates](#templates)
- [Warm pools](#warm-pools)
- [Inside a VM](#inside-a-vm)
- [Storage](#storage)
- [Snapshots](#snapshots)
- [Network](#network)
- [Operations](#operations)
- [Interfaces](#interfaces)
- [Conventions](#conventions)

## Host setup

These use sudo.

| command | what it does |
|---|---|
| `setup [-y]` | the guided setup: checks every step below (plus `firecracker`, `kernel`, and optionally `base`), asks before the optional ones, runs what's missing, and boots a test VM. Safe to re-run. `-y` takes the default answers |
| `version`, `-V`, `--version` | fcvm's version (`0.5.0`; from a git checkout also the commit, e.g. `0.5.0+12.gabc1234`). `version` also shows where the code, state and settings are |
| `upgrade [VERSION]` | installs the newest release (or VERSION, also an older one) next to the running one and switches over, then lists what needs refreshing (jailer copies, boot-time network, service). Refuses in a git checkout: use `git pull` |
| `status [--offline]` | a health check (no sudo): host, components and their versions against the newest releases (kernel with build and release dates, Firecracker), whether the kernel config or initramfs changed since the build, the network, the service and the jailer (including whether their installed copies match this tree), VMs still running older components, then VMs and images. Ends with a to-do list of fixes; exits 1 if something is broken. `--offline` skips the version checks (cached 6 h otherwise) |
| `host-setup [--check]` | installs build and runtime packages, checks `/dev/kvm` access, sets up a subuid range. `--check` only reports what's missing |
| `net-up`, `net-down` | creates (or removes) the bridges `fcbr0` (NAT) and `fcbr1` (restricted), 64 taps each, and the firewall rules. Needed after every reboot, unless the service is installed |
| `jail-setup [--remove]` | installs (or removes) `fcvm-jaild`, the root helper that runs VMs under the Firecracker jailer. Re-run after updating fcvm or Firecracker |
| `service install [--port N]`, `service remove`, `service status` | runs fcvm at boot: the network, then `fcvm serve` as you (console, API, restart policies). `status` needs no sudo and prints the console URL and API token |

## Building blocks

| command | what it does |
|---|---|
| `firecracker` | downloads the latest Firecracker release (or `FC_VERSION`) into `bin/`, checksum-verified |
| `kernel [stable\|mainline\|longterm\|X.Y.Z]` | builds `kernels/vmlinux-X.Y.Z` from kernel.org sources; `kernels/vmlinux` points at the newest. `FORCE=1` rebuilds |
| `init` | builds `build/fc-init` (static) and `build/initramfs.cpio`, which every VM boots with. Runs automatically when needed |
| `base [NAME]` | builds the Ubuntu 26.04 system image (default name `ubuntu-26.04`) |
| `all` | `firecracker` + `kernel` + `init` + `base` |

## Images

| command | what it does |
|---|---|
| `import REF [NAME]` | a registry image (`nginx:latest`, `ghcr.io/org/app:tag`, `name@sha256:...`) or a local one (`docker-archive:F.tar`, `oci:DIR[:TAG]`, `oci-archive:F.tar`, or just a path) → an app image |
| `build [-t NAME] [-f FILE] [--build-arg K=V]... [--no-cache] [--net none \| --allow H,...] [CONTEXT]` | builds an image from a Dockerfile subset, cached per step. The result is the FROM image plus one layer |
| `images [--all] [--json]` | lists images, with type, size, users and source. `--all` includes the build cache |
| `commit VM IMAGE` | saves a stopped VM's changes as a new image: a read-only layer on its image |
| `squash IMAGE NEW` | merges IMAGE's layers into one, on the same base image |
| `export IMAGE FILE.tar` | the image, flattened, as an OCI image archive for `docker load`, `podman load` or `fcvm import` elsewhere |
| `push IMAGE REGISTRY/REPO[:TAG]` | the same, uploaded to a registry (`REGISTRY_USER`/`REGISTRY_PASSWORD` to log in) |
| `rmi IMAGE` | deletes an image nothing depends on |
| `prune` | deletes the build cache, leftover build VMs and the package mirror's files |

## VMs

| command | what it does |
|---|---|
| `create VM IMAGE [opts] [-- CMD...]` | defines a VM on the shared image plus its own writable layer. Options below |
| `create VM --template NAME [opts] [-- CMD...]` | the same from a [template](templates.md); your options win over its own. `run --template NAME` works too |
| `start [-a] VM` | boots it in the background, like `docker start`; `-a` attaches the console |
| `run IMAGE [-d] [opts] [-- CMD...]` | throwaway VM, deleted when it stops. App images: attached like `docker run` (output, Ctrl-C to the app, Ctrl-] detaches, the app's exit code). System images: a shell, or CMD, then stop and delete. `-d`: in the background |
| `stop VM` | Ctrl-Alt-Del (graceful), killed after 20 s. Restart policies then leave it alone until you start it |
| `update VM [--restart POLICY] [--ttl D\|none] [--idle-stop D\|none] [--secret SPEC]... [--unset-secret NAME]... [--forward LIST\|none] [--label K=V]... [--unset-label K]...` | changes a VM's restart policy, lifetime, secrets or forwarding (secrets and forwarding apply from the next start) |
| `rm VM` | deletes a stopped VM and its writable layer (volumes are kept) |
| `ls [--all] [--json] [--label K=V]` | lists VMs: state, exit code, IP, memory used/allocated, disk, network, ports, volumes, restart policy. `--all` includes build VMs; `--label` only those carrying that label |
| `events [--since N] [-f] [--json] [VM]` | the state changes fcvm recorded (`vm.created`, `vm.started`, `vm.stopped`, `vm.exited`, `vm.deleted`, ...), oldest first: after event `N`, or the last `N` with `--since -N`; `-f` follows. See [Events](events.md) |
| `inspect VM` | one VM's details as JSON: its `spec` (the create request, as a template holds it), `status` (`state`, `reason`, `since`), `labels`, its last exit, resource use... see [Listing and inspecting](vms.md#listing-and-inspecting) |

`create` and `run` options:

| option | |
|---|---|
| `--vcpus N`, `--mem MiB` | CPUs and memory (defaults 2 and 1024) |
| `--disk SIZE` | the writable layer's size (default 8G, sparse) |
| `--copy` | a private full copy of the image instead of a layer |
| `-p [BIND:]HOST:GUEST` | publish a TCP port, on `127.0.0.1` unless BIND is given. Repeatable |
| `-v NAME:/PATH[:ro]` | a named volume, created on first use. Repeatable |
| `-v HOST/DIR[:GUEST/PATH][:ro]` | a live host directory (any value starting with `/`, `./`, `../` or `~`). Without a guest path, a directory in your home goes to the same place in the image user's home, any other to the same path. A guest path is `/PATH` or `~/PATH`. Repeatable |
| `--net none` | no network card |
| `--allow HOST,*.DOMAIN,HOST:PORT,@PRESET` | restricted network: only these, through the egress proxy. Repeatable |
| `--cache` | with `--allow`: pip and npm fetch through the proxy's [package mirror](networking.md#the-package-mirror), which keeps the files for the next VM |
| `--idle` | app images: run nothing, stay up for `exec` |
| `-- CMD...` | app images: replaces the image's CMD, keeps its ENTRYPOINT |
| `--entrypoint CMD` | app images: replaces the ENTRYPOINT (`""` clears it) |
| `--jail`, `--no-jail` | run under the Firecracker jailer (default: `JAIL`, 0) |
| `--no-agent` | no exec agent in the VM: no `exec`, `cp`, snapshots or guest metrics. Says what you lose and asks first ([details](vms.md#without-the-exec-agent)) |
| `--restart POLICY` | `no`, `on-failure`, `unless-stopped`, `always` (`create` only) |
| `--ttl DURATION` | stop the VM that long after each boot (`90s`, `30m`, `2h`, `1d`); applied by `fcvm serve` ([lifetimes](vms.md#lifetimes---ttl-and---idle-stop)) |
| `--idle-stop DURATION` | stop it after that long without `exec`, `shell`, `cp` or file operations |
| `--secret NAME=VALUE`, `NAME=@FILE`, `NAME` | a [secret](vms.md#secrets): a file in `/run/secrets` (a tmpfs, never on the VM's disk) and an environment variable. `NAME` alone takes it from your environment. Repeatable |
| `--forward ssh-agent,git` | [forward your credentials](vms.md#forwarding-your-credentials): the host's ssh-agent, and `git credential fill` for the VM's git |
| `--label KEY=VALUE` | tags the VM (repeatable); `ls --label KEY=VALUE` filters by them, `inspect` shows them. Keys: letters, digits, `.` `_` `-` `/`, not starting with `fcvm.` |
| `--if-same-spec` | if a VM of that name exists with the same spec, succeed without doing anything (a safe retry); a different spec fails with `vm_exists` |

## Projects

| command | what it does |
|---|---|
| `up [-f FILE] [VM...]` | the VMs of a [project file](projects.md) (`fcvm.toml` here or above): imports missing images with a `ref`, creates what's missing, starts what's stopped |
| `down [-f FILE] [VM...]` | stops and removes them (volumes are kept) |
| `project show [-f FILE]` | what the file means, as JSON, with each VM's `create` command |

## Templates

| command | what it does |
|---|---|
| `template ls [--json]`, `template show NAME` | lists templates (`*` marks built-in ones), or shows one as JSON |
| `template save NAME IMAGE [create opts] [-- CMD...] [-d DESC]` | saves a recipe, checked as `create` would check it; nothing is created. `-d` goes before IMAGE |
| `template save NAME --from VM [-d DESC]` | saves an existing VM's configuration |
| `template rm NAME` | deletes one of yours |

## Warm pools

| command | what it does |
|---|---|
| `pool set NAME (--template T \| --snapshot S) --size N [--jail]` | a [warm pool](templates.md#warm-pools): N VMs booted (or forked) ahead of time, kept full by `fcvm serve` |
| `pool ls [--json]`, `pool rm NAME` | lists pools (with how many are ready); removes one and its unclaimed VMs |
| `pool claim NAME [--ttl D] [--idle-stop D] [--label K=V]...` | takes a ready VM out of the pool and prints its name |

## Inside a VM

| command | what it does |
|---|---|
| `exec [-i] [-t] [-d] [-u USER] [-w DIR] [-e K=V]... [--timeout S] VM CMD...` | runs a command in a running VM, like `docker exec`. Exits with its status, or 124 on timeout. `-d`: in the background, as a job; prints its id |
| `jobs VM [--json]`, `jobs VM ID [-f]`, `jobs VM ID --kill`, `jobs VM --clear` | background commands: list them, show (or follow) one's output, kill one, forget finished ones |
| `balloon VM [MiB\|0]` | takes MiB of memory back from a running VM through its balloon (0 gives it all back); without a size, shows the balloon and the guest's memory view ([details](vms.md#resources)) |
| `shell [-u USER] VM` | an interactive shell (bash, else sh). Same as `exec -it VM bash` |
| `cp [-L] SRC DST` | copies files or directories in or out of a running VM (`VM:PATH` on one side), like `docker cp` |
| `console VM` | attaches to the live serial console. Ctrl-] detaches; the VM keeps running |
| `logs [-f] VM` | console output of the current or last boot |
| `ssh VM [args]` | ssh as root (system images), checking the VM's own host key |
| `ssh-config [VM...] [--install]` | ssh client configuration for system VMs (`Host fcvm-NAME`, a `ProxyCommand` that finds the current address); `--install` writes `~/.ssh/fcvm_config` and includes it from `~/.ssh/config`. For `ssh`, `scp`, `rsync`, VS Code Remote-SSH |

## Storage

| command | what it does |
|---|---|
| `volume create NAME [SIZE]`, `volume ls [--json]`, `volume rm NAME` | named volumes: persistent ext4 disks attached with `-v` |
| `mount VM HOST/DIR[:GUEST/PATH][:ro]`, `umount VM PATH` | adds or removes a live host directory (same forms as `-v`): at once on a running VM, and from the next start. `umount` takes the guest path or the host directory |
| `publish VM [BIND:][HOSTPORT]:GUESTPORT`, `unpublish VM HOSTPORT` | publishes or unpublishes a TCP port: at once on a running VM, and from the next start. Without `HOSTPORT` a free one is picked; the spec is printed |

## Snapshots

| command | what it does |
|---|---|
| `snapshot VM NAME` | saves a running VM's memory, device state and disk. The VM keeps running (paused ~0.75 s per GB of RAM) |
| `snapshot ls [--json]`, `snapshot rm NAME` | lists and deletes snapshots |
| `snapshot export NAME FILE.tar`, `snapshot import FILE.tar [NAME]` | moves a snapshot to another fcvm host, which needs the same images (by name) and a compatible Firecracker ([details](snapshots.md#moving-snapshots-between-hosts)) |
| `fork SNAPSHOT [NAME] [-n N] [--label K=V]...` | starts running VMs from a snapshot, ~150 ms each (~1 s jailed), each with its own disk, IP, MAC and hostname |

## Network

| command | what it does |
|---|---|
| `egress VM [--allow H,...] [--deny H,...] [-n N \| -f]` | a restricted VM's allowlist and its recent allowed and denied requests. Changes apply live |

## Operations

Slow commands (`import`, `build`, `export`, `push`, `snapshot`, `commit`)
take `--op`: the command runs detached, its id is printed at once, and its
state and output are kept in `vms/.ops/` (for a week after it ends), so a
script, the console or another host can ask how it went. The [API](api.md#background-jobs)
exposes the same as jobs.

```sh
fcvm import python:3.13 --op       # prints op-k2j9x7d4qa
fcvm ops                           # recent operations: id, when, status, how long, what
fcvm ops op-k2j9x7d4qa -f          # its output, followed until it ends; exits 1 if it failed
fcvm ops op-k2j9x7d4qa --json      # the record: status, exit, error, command, lines
```

| command | what it does |
|---|---|
| `COMMAND ... --op` | runs the command as an operation and prints its id |
| `ops [--json]` | the recent operations |
| `ops ID [-f] [--json]` | one operation's record and output; `-f` follows a running one |

An operation whose process is gone without having ended (the host rebooted,
the process was killed) shows as `failed` with the error `interrupted`.

## Interfaces

| command | what it does |
|---|---|
| `serve [--port 8686] [--service]` | the web console and [HTTP API](api.md) on `127.0.0.1`, plus the supervisor for restart policies. Prints a login URL. `--service` keeps the token across restarts |
| `mcp` | the MCP server on stdio, for agents ([Agents](agents.md)) |

## Conventions

- **Names.** Names of VMs, images, snapshots and volumes match
  `[A-Za-z0-9_][A-Za-z0-9_.-]*`. Names starting with `_` are internal (build
  VMs and cache), hidden unless you pass `--all`.
- **Output.** Commands print progress on stderr and results on stdout, so
  `--json` output can be piped.
- **Exit codes.** Failures exit non-zero with an `error:` line, followed,
  when stderr isn't a terminal, by `fcvm-error: CODE` naming the failure
  for scripts ([Error codes](errors.md)). `run`, `start -a` and `exec` pass
  through the guest's exit code.
- **Settings.** They come from `fcvm.conf` and the environment. See
  [Configuration](configuration.md).
