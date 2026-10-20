# The node contract

**Contract version: 1** (fcvm 0.8.0). `fcvm version` and `GET /api/host`
report it as `contract`.

A program that manages fcvm hosts from outside (a control plane, a fleet
agent, your own scripts) can rely on everything on this page, and on
nothing else about fcvm's files. Changing anything here bumps the contract
version, in a minor fcvm release, with a CHANGELOG entry; additions don't.
Everything is relative to the state directory (`fcvm version` prints it).

## How to drive fcvm

- **Every change goes through the `fcvm` command** (or the [HTTP
  API](api.md), which runs it). Never write the files below; read them.
- **Failures** exit non-zero with an `error:` line and, when stderr isn't
  a terminal, `fcvm-error: CODE` ([Error codes](errors.md)). The codes and
  their meanings are part of the contract; messages aren't.
- **`FCVM_ACTOR`** in the environment names who acts in events and logs
  (`serve`, `supervisor`, or your program's name); events also record
  the command line (`argv`, secrets masked), so the event stream is the
  audit trail ([Audit](events.md#audit)).
- **`FCVM_DAEMON=1`** makes `start`, `fork` and `publish` act locally
  instead of delegating to the running service.
- **Locks.** A command that changes a VM holds `vms/.locks/vm-NAME`
  (flock) while it does; it waits 30 s for it and fails with `vm_busy`
  otherwise. Readers don't lock.

## Flags

| flag | on | meaning |
|---|---|---|
| `--label KEY=VALUE` | `create`, `update`, `fork`, `pool claim` | tags; `ls --label KEY=VALUE` filters; fcvm never reads them, except `tenant` |
| `--tenant NAME` | `create`, `fork`, `pool claim` | the `tenant` label, set once (`update` refuses `--label tenant=`); `ls --tenant NAME` filters. Its quota is checked at admission |
| `--tenant NAME` | `import`, `build`, `volume create`, `snapshot import` | the object's `tenant`: only that tenant's VMs may use it (`policy` otherwise). `commit` and `snapshot` take the VM's; `fork` takes the snapshot's. `images`, `snapshot ls`, `volume ls --tenant NAME` filter |
| `--unset-label KEY` | `update` | |
| `--if-same-spec` | `create` | succeed if the VM exists with the same `spec`; `vm_exists` if it differs |
| `--lease UNTIL` | `create`, `start`, `update`, `fork`, `pool claim` | a Unix time, ISO 8601 time, or duration from now; `none` clears |
| `--op` | `import`, `build`, `export`, `push`, `snapshot`, `commit` | run detached; prints an operation id |
| `--json` | `ls`, `inspect` (always JSON), `images`, `snapshot ls`, `volume ls`, `pool ls`, `template ls`, `events`, `ops`, `tenant ls`, `tenant show` (always JSON), `status`, `config` | machine output on stdout |

## Objects

### A VM: `vms/NAME/vm.json` and `fcvm inspect NAME`

`inspect` returns `vm.json`'s fields plus the live ones. Contract fields:

| field | meaning |
|---|---|
| `spec` | the create request, normalized: the fields of a [template](templates.md#managing-templates) (`image`, `vcpus`, `mem_mib`, `disk`, `copy`, `network`, `allow`, `ports`, `volumes`, `process`, `command`, `entrypoint`, `jail`, `restart`, `ttl`, `idle_stop`, `secrets` (names), `forward`, `cache`) |
| `status` | `{"state": "running\|stopped\|exited", "reason", "since"}`; reasons: `start`, `user`, `ttl`, `idle`, `lease`, `exit`, `shutdown`, `crash`, `created` |
| `labels` | `{KEY: VALUE}` |
| `lease_until` | Unix time, or `null` |
| `state`, `pid`, `ip`, `image`, `type`, `jail`, `restart`, `ports`, `volumes`, `shares`, `net`, `ttl_s`, `idle_stop_s`, `expires_at`, `idle_expires_at`, `expired`, `last_exit`, `stopped_by_user`, `pooled`, `claimed_at`, `restored_from`, `created`, `mem_used_bytes`, `disk_used_bytes` | as [the API describes them](api.md#vms) |

Markers next to `vm.json`, readable for liveness and reasons: `pid` and
`pid.id` (boot id and process start time: the VM runs iff `/proc/PID`
exists and matches), `stopped`, `expired` (its content is the reason),
`last-exit` (JSON `{code, fc_status, stale, at}`), `pooled` (the pool's
name), `claimed` (Unix time), `activity` (its mtime is the idle clock),
`starting` (admitted, no pid yet; counts as allocated while under two
minutes old).

### Tenants: `vms/.tenants/NAME.json` and `fcvm tenant show NAME`

`{name, quota: {vcpus, mem_mib, vms, disk_mib}, created, updated}`, each
quota value a whole number or `null` (no limit), written by `fcvm tenant
set`. `tenant show` adds `allocated: {vcpus, mem_mib, vms, disk_mib}`
(vCPUs, memory and count of the tenant's running and starting VMs; the
nominal disk of all its VMs, images, snapshots and volumes) and `vms`,
`images`, `snapshots`, `volumes` (names). A tenant is the `tenant` label
of its VMs and the `tenant` field of `images/NAME.json`,
`snapshots/NAME/meta.json` and `volumes/NAME.json` (absent: public); one
without a file has no quota.

**Admission** runs under `vms/.locks/admission` when a VM starts (by any
path, including restart policies), is forked, or is claimed from a pool
for a tenant, and before anything takes disk (a VM's layer at `create`,
a fork's copy, `commit`, `snapshot`, `volume create`, `import`, `build`):
`quota_exceeded` when the tenant's quota would be exceeded; `node_full`
when `NODE_ADMIT=1` and the host's capacity would be. A refused fork
leaves nothing; a refused claim leaves the VM pooled. A VM that names
another tenant's object is refused with `policy`.

### Events: `vms/.events.jsonl`, `vms/.events.1.jsonl`, `vms/.events.seq`

One JSON object per line, appended under `vms/.locks/events`; `seq`
strictly increasing; rotated at 4 MiB into `.events.1.jsonl`. Fields and
types: [Events](events.md). Read the older file first; a line may be cut
while it's being written. `fcvm events --since N --json` and
`GET /api/events` (JSON or server-sent events) read them for you.

### Operations: `vms/.ops/ID.json` and `vms/.ops/ID.log`

`{id, kind, title, command, status: running|done|failed, started, ended,
exit, error, code, pid, boot_id}`; the log is the command's output. Ids
match `op-[a-z0-9]{10}`. `fcvm ops ID --json` adds `lines` and `next`;
`GET /api/jobs/ID?wait=S` waits. Kept a week after the end; one whose
process is gone without an end is `failed` with `error: "interrupted"`.

### The host

- `vms/.node/cordoned` exists while the host refuses new VMs
  (`fcvm cordon`); its content is the reason.
- `fcvm status --json`: `{version, contract, hostname, cordoned,
  admission, host: {cpus, mem_mib, mem_available_mib, disk_mib,
  disk_free_mib, load}, capacity, allocated, allocatable (each {vcpus,
  mem_mib, vms, disk_mib}), vms: {running, starting, stopped}, objects:
  {images, snapshots, volumes}, tenants, slots: {taps_full,
  taps_restricted: {total, free}, jail}, kernel, firecracker}`.
  `GET /api/host` carries the same under `node`.
- `fcvm config --json`: `{version, contract, config_file,
  config_file_exists, settings: {NAME: "value"}, paths: {code, state,
  images, vms, volumes, snapshots, kernels, bin, cache, build}}`: the
  settings in effect, as strings, so nothing parses `fcvm.conf`.
- `vms/.serve.json`: `{url, token, port, pid}` of the running service
  (mode 0600).
- `vms/.pools/NAME.json`: `{name, template|snapshot, size, jail}`.
- `images/NAME.json`, `snapshots/NAME/meta.json`, `volumes/NAME.ext4`
  (and `volumes/NAME.json`, its tenant and size): the objects
  `fcvm images|snapshot ls|volume ls --json` list, each with `tenant`.

### Settings

`fcvm.conf` (shell syntax; `fcvm version` says which file) with the
variables in [Configuration](configuration.md). The ones this contract
adds: `LEASE_GRACE` (seconds, default 300), and the
[node settings](configuration.md#node) `NODE_ADMIT`, `NODE_VCPUS`,
`NODE_MEM_MIB`, `NODE_VMS`, `NODE_DISK_MIB`, `NODE_CPU_OVERCOMMIT`,
`NODE_MEM_OVERCOMMIT`, `NODE_RESERVE_MIB`.

## What a release may change

Field *additions* to any object or event, new event types and reasons,
new error codes, new flags. Renames and removals of anything above bump
the contract version.
