# Snapshots and fork

`fcvm snapshot` captures a running VM whole: its memory (processes, page
cache, anything in RAM), device state and writable disk, all taken while it
is paused, so they are consistent. `fcvm fork` starts new VMs from that
exact moment. Processes keep running from where they were, and warm caches
and loaded dependencies stay warm.

- [Example](#example)
- [What each fork gets](#what-each-fork-gets)
- [Snapshot or commit?](#snapshot-or-commit)
- [Performance](#performance)
- [Managing snapshots](#managing-snapshots)
- [Limits](#limits)

## Example

```sh
fcvm create box python-3.13-slim --idle && fcvm start box
fcvm exec box sh -c 'pip install -q numpy pandas && python -c "import pandas"'   # prepare once
fcvm snapshot box ready          # box keeps running
fcvm fork ready try -n 3         # try-1, try-2, try-3: running in ~0.6 s total
fcvm exec try-2 python -c 'import pandas; print(pandas.__version__)'
fcvm rm try-2 && fcvm fork ready try-2   # roll back: a fresh copy of the prepared state
```

## What each fork gets

- **Its own disk:** a copy of the snapshot's writable layer. Images stay
  shared and read-only.
- **Network:** its own tap, IP and MAC in the snapshot's network mode (full,
  restricted with the same allowlist, or none). Published ports aren't
  carried over, since they would conflict with the source.
- **Hostname:** its own, set after restore by the exec agent over vsock.
- **Clock:** set from the host's clock right after the restore (by the exec
  agent), so it's current, whatever time has passed since the snapshot.
- **Randomness:** the guest kernel reseeds its RNG on restore (VMGenID), so
  forks don't share random state. User-space programs that keep their own
  random state in memory (a PRNG seeded before the snapshot) will still
  repeat it, which is inherent to cloning a running process.
- **Memory:** loaded on demand from the snapshot's memory file and shared
  copy-on-write between forks. A fork starts at about 20 MB of host RAM.
- **Isolation:** a snapshot of a jailed VM forks jailed, each fork in its
  own fresh jail.

## Snapshot or commit?

| | `snapshot` + `fork` | `commit` + `create`/`run` |
|---|---|---|
| captures | memory, running processes and disk | disk only |
| source VM | keeps running (paused briefly) | must be stopped |
| new VMs start | already running, where the source was, in ~150 ms | with a normal boot, ~0.5 s for apps |
| result | a snapshot, tied to this Firecracker version and CPU | an image, portable across fcvm versions |
| good for | parallel attempts, rollback, skipping a slow warm-up | reusable environments, building on further |

## Performance

Measured on a 1 GiB VM:
- **Snapshot.** The source is paused for about 0.75 s, mostly writing the
  memory file.
- **Size.** The memory file is then made sparse, so an idle VM's 1 GiB
  takes about 50 MB on disk.
- **Fork.** About 150 ms, including giving the fork its new address. A
  jailed fork takes about 1 s, most of it building the jail and its network
  namespace.

## Managing snapshots

```sh
fcvm snapshot ls            # or --json
fcvm snapshot rm ready      # running forks are unaffected
```

The web console lists snapshots, and forks or deletes them. The MCP server
has `snapshot_vm`, `fork`, `snapshots` and `remove_snapshot`.

## Limits

- **Volumes and host directories.** VMs with read-write volumes can't be
  snapshotted, because two forks can't share a writable volume; use `:ro`
  volumes, or copy data in. Neither can VMs with host directories, because
  their live connections can't be cloned.
- **Versions.** A snapshot is tied to the Firecracker version and CPU model
  it was taken on. After `fcvm firecracker` upgrades, old snapshots may not
  load; `fork` says so.
- **Stopped forks.** A stopped fork is an ordinary VM: `fcvm start`
  cold-boots it from its own disk. Its hostname was set at fork time as
  runtime state, so an app VM comes back with its image's hostname.
- **Images in use.** A snapshot keeps its images in use (`rmi` refuses).
- **Old VMs.** Only VMs started with a current fcvm initramfs can be
  re-addressed after a fork; older ones get a warning.
- **Pause time.** The source's pause grows with its memory (~0.75 s per
  GB). Diff snapshots, which would shorten it, are on the
  [roadmap](roadmap.md#agentic-development).
