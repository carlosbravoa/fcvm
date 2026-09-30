# Commands

Every `fcvm` command, grouped by purpose. `./fcvm help` prints the same
list in short form. Pages under "Using fcvm" in the
[documentation index](README.md) explain each area in depth.

- [Host setup](#host-setup)
- [Building blocks](#building-blocks)
- [Images](#images)
- [VMs](#vms)
- [Inside a VM](#inside-a-vm)
- [Storage](#storage)
- [Snapshots](#snapshots)
- [Network](#network)
- [Interfaces](#interfaces)
- [Conventions](#conventions)

## Host setup

These use sudo.

| command | what it does |
|---|---|
| `host-setup` | installs build and runtime packages, checks `/dev/kvm` access, sets up a subuid range |
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
| `rmi IMAGE` | deletes an image nothing depends on |
| `prune` | deletes the build cache and leftover build VMs |

## VMs

| command | what it does |
|---|---|
| `create VM IMAGE [opts] [-- CMD...]` | defines a VM on the shared image plus its own writable layer. Options below |
| `start [-a] VM` | boots it in the background, like `docker start`; `-a` attaches the console |
| `run IMAGE [-d] [opts] [-- CMD...]` | throwaway VM, deleted when it stops. App images: attached like `docker run` (output, Ctrl-C to the app, Ctrl-] detaches, the app's exit code). System images: a shell, or CMD, then stop and delete. `-d`: in the background |
| `stop VM` | Ctrl-Alt-Del (graceful), killed after 20 s. Restart policies then leave it alone until you start it |
| `update VM --restart POLICY` | changes a VM's restart policy |
| `rm VM` | deletes a stopped VM and its writable layer (volumes are kept) |
| `ls [--all] [--json]` | lists VMs: state, exit code, IP, memory used/allocated, disk, network, ports, volumes, restart policy. `--all` includes build VMs |
| `inspect VM` | one VM's details as JSON, including its last exit |

`create` and `run` options:

| option | |
|---|---|
| `--vcpus N`, `--mem MiB` | CPUs and memory (defaults 2 and 1024) |
| `--disk SIZE` | the writable layer's size (default 8G, sparse) |
| `--copy` | a private full copy of the image instead of a layer |
| `-p [BIND:]HOST:GUEST` | publish a TCP port, on `127.0.0.1` unless BIND is given. Repeatable |
| `-v NAME:/PATH[:ro]` | a named volume, created on first use. Repeatable |
| `-v /HOST/DIR:/PATH[:ro]` | a live host directory (any value starting with `/`, `./`, `../` or `~`). Repeatable |
| `--net none` | no network card |
| `--allow HOST,*.DOMAIN,HOST:PORT,@PRESET` | restricted network: only these, through the egress proxy. Repeatable |
| `--idle` | app images: run nothing, stay up for `exec` |
| `-- CMD...` | app images: replaces the image's CMD, keeps its ENTRYPOINT |
| `--entrypoint CMD` | app images: replaces the ENTRYPOINT (`""` clears it) |
| `--jail`, `--no-jail` | run under the Firecracker jailer (default: `JAIL`, 0) |
| `--restart POLICY` | `no`, `on-failure`, `unless-stopped`, `always` (`create` only) |

## Inside a VM

| command | what it does |
|---|---|
| `exec [-i] [-t] [-u USER] [-w DIR] [-e K=V]... [--timeout S] VM CMD...` | runs a command in a running VM, like `docker exec`. Exits with its status, or 124 on timeout |
| `shell [-u USER] VM` | an interactive shell (bash, else sh). Same as `exec -it VM bash` |
| `cp [-L] SRC DST` | copies files or directories in or out of a running VM (`VM:PATH` on one side), like `docker cp` |
| `console VM` | attaches to the live serial console. Ctrl-] detaches; the VM keeps running |
| `logs [-f] VM` | console output of the current or last boot |
| `ssh VM [args]` | ssh as root (system images, with the key in `ssh/`) |

## Storage

| command | what it does |
|---|---|
| `volume create NAME [SIZE]`, `volume ls [--json]`, `volume rm NAME` | named volumes: persistent ext4 disks attached with `-v` |
| `mount VM /HOST/DIR:/PATH[:ro]`, `umount VM /PATH` | adds or removes a live host directory: at once on a running VM, and from the next start |

## Snapshots

| command | what it does |
|---|---|
| `snapshot VM NAME` | saves a running VM's memory, device state and disk. The VM keeps running (paused ~0.75 s per GB of RAM) |
| `snapshot ls [--json]`, `snapshot rm NAME` | lists and deletes snapshots |
| `fork SNAPSHOT [NAME] [-n N]` | starts running VMs from a snapshot, ~150 ms each (~1 s jailed), each with its own disk, IP, MAC and hostname |

## Network

| command | what it does |
|---|---|
| `egress VM [--allow H,...] [--deny H,...] [-n N \| -f]` | a restricted VM's allowlist and its recent allowed and denied requests. Changes apply live |

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
- **Exit codes.** Failures exit non-zero with an `error:` line. `run`,
  `start -a` and `exec` pass through the guest's exit code.
- **Settings.** They come from `fcvm.conf` and the environment. See
  [Configuration](configuration.md).
