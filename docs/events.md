# Events

Every command that changes a VM's state records an event: one JSON line in
`vms/.events.jsonl`, with a sequence number that only goes up. The web
console redraws on them instead of waiting for its next poll, scripts can
follow what happens to their VMs, and a program managing fcvm (a control
plane) can list the VMs once and then watch from where the list was taken.

```sh
fcvm events                     # everything kept, oldest first
fcvm events --since -20         # the last 20
fcvm events -f web              # follow one VM
fcvm events --since 4012 --json # as JSON lines, after event 4012
```

```
  4013  14:02:11  vm.created     web                      image=nginx-latest       cli:carlos
  4014  14:02:12  vm.started     web                      pid=41023 ip=172.30.0.11 cli:carlos
  4015  14:09:40  vm.exited      web                      shutdown fc_status=0     serve
  4016  14:09:40  vm.stopped     web                      user                     serve
```

## An event

```json
{"seq": 4014, "t": 1760900532.1, "type": "vm.started", "vm": "web", "reason": null,
 "by": "cli:carlos", "labels": {"team": "web"}, "data": {"pid": 41023, "ip": "172.30.0.11"}}
```

| field | meaning |
|---|---|
| `seq` | strictly increasing across every process that writes (your commands, the service, the supervisor) |
| `t` | Unix time |
| `type`, `vm` | what happened, to which VM |
| `reason` | why, when it matters (below) |
| `by` | who did it: `cli:USER` for a command run by hand, `serve` for the console and the API, `supervisor` for restart policies, lifetimes and pools. A program sets its own with `FCVM_ACTOR` |
| `labels` | the VM's [labels](vms.md#listing-and-inspecting) at the time |
| `data` | details per type |

## Types

| type | reason | data |
|---|---|---|
| `vm.created` | `fork` for a fork | `image`, `ephemeral`; `restored_from` for a fork |
| `vm.started` | `fork` for a fork | `pid`, `ip` |
| `vm.stopped` | `user` (`fcvm stop`, the console), `ttl` or `idle` (a [lifetime](vms.md#lifetimes---ttl-and---idle-stop)), `lease` (its lease ended), `system` (host shutdown) | |
| `vm.exited` | how the guest ended: `exit` (an app's process ended), `shutdown` (the guest shut itself down, which a stop also causes), `crash` (Firecracker was killed or died, or the host did) | the exit record: `code`, `fc_status`, `stale`, `at` |
| `vm.restarting` | `policy:NAME` | `in` (seconds), `attempt` |
| `vm.updated` | | `what`: the changes, as `fcvm update` lists them |
| `vm.deleted` | `ephemeral` (a throwaway VM that ended), `pool` (an unclaimed VM of a removed pool), `lease` (its lease ended) | |
| `node.cordoned`, `node.uncordoned` (no `vm`) | | `reason` given to `fcvm cordon` |
| `pool.claimed` | | `pool` |
| `operation.done` (no `vm`) | `done` or `failed` | `op`, `kind`, `title`, `error`: an [operation](cli.md#operations) ended |

A stop produces both `vm.exited` (the guest ended) and `vm.stopped` (who
asked for it, and why). Releases may add types and reasons; they don't
rename them.

## In the API

`GET /api/events?since=N` returns `{"events": [...], "next": N}`: the
events after `N`, oldest first, and the sequence number to pass next
time. `vm=NAME` keeps one VM's, `limit=N` only the last N of them (so
`next` then skips the older ones; the MCP tool's `limit` keeps the oldest
instead, for paging). `GET /api/vms`
carries `X-Fcvm-Seq`, the latest event at the time of the list, so a
client can list, then watch from exactly there.

With `Accept: text/event-stream` the connection stays open and events are
streamed as they happen, in the [server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
format: `id` is the sequence number and `data` the JSON (there is no
`event` field, so a browser's `onmessage` sees them all; the type is in
the data).
A browser's `EventSource` resumes a dropped stream from its last id by
itself; `since=latest` starts from now.

```sh
curl -sN -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" \
    "http://127.0.0.1:8686/api/events?since=latest"
```

The MCP server has the same as an `events` tool.

## Retention

The file is rotated at 4 MiB (the previous one is kept as
`vms/.events.1.jsonl`), so the last several thousand events are there.
Asking for a `since` older than what's kept gets a `410` with code `gone`:
list the VMs again and watch from `X-Fcvm-Seq`.
