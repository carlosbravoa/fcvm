# HTTP API

`fcvm serve` (and the fcvm service, which runs it at boot) exposes the same
operations as the CLI as JSON over HTTP on `127.0.0.1`. The web console is
built on this API, and scripts can use it too. Every change goes through the
fcvm CLI, so the API behaves exactly like the command line: the same
validation, the same errors, the same locks.

- [Connecting](#connecting)
- [Client libraries](#client-libraries)
- [Conventions](#conventions)
- [Host](#host)
- [VMs](#vms)
- [Files and host directories in a VM](#files-and-host-directories-in-a-vm)
- [Images, snapshots, volumes](#images-snapshots-volumes)
- [Background jobs](#background-jobs)
- [Events](#events)
- [Build projects](#build-projects)
- [Terminals (WebSockets)](#terminals-websockets)

## Connecting

The server listens on `127.0.0.1` only (port 8686 by default). Every request
needs the token. With the service installed, `fcvm service status` prints
the URL and the token. They are also in `vms/.serve.json` (mode 0600),
written by any running `fcvm serve`.

```sh
TOKEN=$(jq -r .token vms/.serve.json)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8686/api/vms | jq '.[].name'
```

- **Scripts** send `Authorization: Bearer <token>`. That header is only
  accepted under `/api/`.
- **Browsers** open the printed URL (`/?token=...`) once, and the server
  swaps the token for an HttpOnly, SameSite=Strict cookie. Changes made with
  the cookie must carry a same-origin `Origin` header. Bearer requests don't
  need one, because browsers never add an `Authorization` header by
  themselves.
- **Host header.** It must be `127.0.0.1:PORT` or `localhost:PORT`, as a
  guard against DNS rebinding.
- **Token lifetime.** A plain `fcvm serve` makes a new token each time it
  starts. The service (`fcvm serve --service`) keeps its token in
  `vms/.serve-token` across restarts.

**Anyone with the token has your fcvm**: they can run commands as root in
your VMs, and create VMs with your host directories mounted. Treat it like
an SSH key.

## Client libraries

[`sdk/python/fcvm.py`](../sdk/README.md) (standard library) and
[`sdk/typescript/fcvm.mjs`](../sdk/README.md) (Node 18+, no dependencies)
wrap this API: `Client().create("box", template="python-sandbox")`, then
`exec`, `write_file`, `publish`, `snapshot`, and so on. See
[sdk/README.md](../sdk/README.md).

## Conventions

- Bodies are JSON (`Content-Type` isn't checked). Responses are JSON unless
  noted.
- Names (VMs, images, snapshots, volumes, projects) match
  `[A-Za-z0-9_][A-Za-z0-9_.-]*`.
- Errors come back as `{"error": "message", "code": "CODE"}` with a 4xx or
  5xx status, for example `409 {"error": "VM 'web' is running (fcvm stop web
  first)", "code": "vm_running"}`. The codes and their statuses are listed in
  [Error codes](errors.md): 404 for something that doesn't exist, 409 for a
  conflict with the VM's state, 503 when the host can't take the VM right
  now, 400 for invalid input and for failures without a code. A command that
  takes too long returns 504.
- Paths below are relative to `/api`.

## Host

| method, path | returns |
|---|---|
| `GET /host` | hostname, kernel, CPUs, load, disk and memory, free taps per network, `jail_available`, and recent host samples |
| `GET /presets` | the egress presets (`@pypi`, ...) with their host patterns |

## VMs

| method, path | body / query | does |
|---|---|---|
| `GET /vms` | | every VM (as `fcvm ls --json`), each with a `supervisor` field |
| `GET /vms/{vm}` | | one VM (as `fcvm inspect`), plus `supervisor` |
| `POST /vms` | see below | create a VM and start it |
| `POST /vms/{vm}/start` | | `fcvm start` |
| `POST /vms/{vm}/stop` | | `fcvm stop`; restart policies then leave it alone |
| `POST /vms/{vm}/restart` | | stop, then start |
| `POST /vms/{vm}/update` | `{"restart": "no\|on-failure\|unless-stopped\|always", "ttl": "2h", "idle_stop": null, "secrets": {"NAME": "value"}, "unset_secrets": ["OLD"], "forward": ["ssh-agent", "git"], "labels": {"team": "web"}, "unset_labels": ["old"]}` | `fcvm update`: any of these; `null` or `""` clears a lifetime; secrets and forwarding apply from the next start; labels merge |
| `POST /vms/{vm}/snapshot` | `{"name": "snap"}` | `fcvm snapshot` |
| `POST /vms/{vm}/commit` | `{"image": "name"}` | `fcvm commit` (the VM must be stopped) |
| `DELETE /vms/{vm}` | | stop it if running, then delete it |
| `GET /vms/{vm}/stats` | `?range=10m\|1h\|6h\|24h` | `{"span", "samples": [...], "guest": [...]}`. `samples`, from the host: `cpu_pct`, `rss`, `disk_read_bps`, `disk_write_bps`, `net_rx_bps`, `net_tx_bps`. `guest`, from inside the VM: `g_cpu_pct` (100 = one vCPU), `g_mem_used`, `g_mem_total`, `g_disk_used`, `g_disk_total`, `g_load1`, `g_procs`. `10m` (the default) is every 2 s (guest: 10 s); longer ranges are one-minute averages, with both sets of fields in each point |
| `GET /vms/{vm}/processes` | | the guest's processes: `{"t", "uptime", "load1", "mem_used", "mem_total", "cpus", "processes": [{pid, ppid, uid, user, state, comm, cmd, rss, ticks, cpu_pct, kernel, fcvm}]}`. `cpu_pct` is since the previous call (null on the first); `fcvm` labels fcvm's own processes |
| `POST /vms/{vm}/exec` | `{"command": "sh string"}` or `{"argv": [...]}`, plus `workdir`, `env` (object), `user`, `stdin`, `timeout` (s, default 300), `background` | `fcvm exec`: `{"exit_code", "stdout", "stderr"}` (`timed_out` on a timeout), or with `background` `{"job": id}` |
| `GET /vms/{vm}/jobs` | | background commands: `[{id, command, started, status: running\|exited\|lost, exit_code, ended}]` |
| `GET /vms/{vm}/jobs/{id}` | `?tail=200` | one job, plus `stdout` and `stderr` (the last `tail` lines; `0` for all) |
| `DELETE /vms/{vm}/jobs/{id}` | | kill it |
| `GET /vms/{vm}/balloon` | | `{"balloon_mib", "mem_mib", "guest": {"total_mib", "free_mib", "available_mib"}}` (`fcvm balloon`) |
| `POST /vms/{vm}/balloon` | `{"mib": 512}` | take that much memory back through the balloon; `0` returns it |
| `GET /vms/{vm}/logs` | | `{"log": "..."}`: console output, the last 200 KB |
| `GET /vms/{vm}/egress` | | `{"text": "..."}`: a restricted VM's allowlist and recent decisions |

`POST /vms` body:

```json
{
  "name": "box", "image": "python-3.13-slim", "vcpus": 2, "mem_mib": 1024,
  "network": "full | none | restricted", "allow": ["@pypi", "github.com"],
  "ports": ["8080:80"], "volumes": ["cache:/root/.cache", "/home/me/src:/work:ro"],
  "idle": true, "command": "shell command (app images, when not idle)",
  "jail": true, "restart": "unless-stopped", "start": true,
  "ttl": "2h", "idle_stop": "30m", "secrets": {"API_KEY": "sk-..."}, "forward": ["ssh-agent", "git"],
  "disk": "16G", "copy": false, "entrypoint": null, "ref": "python:3.13-slim",
  "labels": {"team": "web"}, "if_same_spec": false
}
```

Only `name` and `image` are required. `ref` imports the image first when
it's missing. `allow` is used with
`"network": "restricted"`. The response is the new VM, as `GET /vms/{vm}`.
With `if_same_spec`, a VM that already exists with the same spec is
returned as it is (and started if it isn't running), so a request can be
retried after a lost reply; a different spec is a 409 `vm_exists`.

Fields worth knowing in a VM object (as `fcvm inspect`, plus `vm.json`):

| field | meaning |
|---|---|
| `state` | `running`, `stopped`, or `exited` (an app VM whose process ended; see `exit_code`) |
| `status` | `{"state", "reason", "since"}`: why it's in that state and since when, see [Listing and inspecting](vms.md#listing-and-inspecting) |
| `spec` | the create request, normalized (the fields of a template) |
| `labels` | your tags (`{"team": "web"}`), from `labels` at create or update |
| `restart` | the restart policy |
| `stopped_by_user` | stopped with `fcvm stop`, so restart policies leave it alone |
| `last_exit` | how it last ended: `code` (an app's exit code), `fc_status` (Firecracker's: 0 = the guest shut down; negative = killed by that signal), `stale` (it was running when the host crashed or rebooted), `at` (Unix time) |
| `supervisor` | `null`, or `{"restart_at", "attempts", "error"}` while a restart is pending or after a failed one |
| `ttl_s`, `idle_stop_s`, `expires_at`, `idle_expires_at`, `expired` | [lifetimes](vms.md#lifetimes---ttl-and---idle-stop): the settings in seconds, when they would stop a running VM (Unix time), and `"ttl"` or `"idle"` after one did |
| `secrets` | the names of the VM's [secrets](vms.md#secrets) (never their values) |
| `forward` | what the host [forwards](vms.md#forwarding-your-credentials) into it: `"ssh-agent"`, `"git"` |
| `balloon_mib` | memory taken back through the [balloon](vms.md#resources) (running VMs; `null` for one without the device) |
| `pooled`, `claimed_at` | the [pool](templates.md#warm-pools) a VM waits in, and when it was claimed (Unix time) |
| `jail`, `net`, `ports`, `volumes`, `shares`, `ip`, `pid`, `mem_used_bytes`, `disk_used_bytes` | as the names say |

## Files and host directories in a VM

These work on running VMs through the exec agent, so they work in any image,
even one without a shell.

| method, path | body / query | does |
|---|---|---|
| `GET /vms/{vm}/files` | `?path=/etc` | list a directory: `{"path", "entries": [{name, type, size, mode, uid, gid, mtime, ...}]}` |
| `GET /vms/{vm}/file` | `?path=/etc/hosts[&download=1]` | the file's bytes, streamed (`download=1` sets `Content-Disposition: attachment`) |
| `PUT /vms/{vm}/file` | `?path=/work/a.txt[&mode=644]`, raw body | write a file atomically. Existing files keep their owner and mode; new ones take their folder's owner |
| `POST /vms/{vm}/files` | `{"op": "mkdir\|remove\|rename", "path": "...", "to": "..."}` | directory operations; `remove` is recursive |
| `POST /vms/{vm}/mounts` | `{"host": "~/dir", "path": "/guest/path", "ro": false}` | `fcvm mount`: a live host directory. `host` may start with `~`; `path` may be empty (a directory in the home goes to the image user's home, any other to the same path) or start with `~` |
| `DELETE /vms/{vm}/mounts` | `?path=/guest/path` | `fcvm umount` (the guest path, e.g. `~/dir`) |
| `POST /vms/{vm}/ports` | `{"port": "[BIND:][HOSTPORT]:GUESTPORT"}` | `fcvm publish`: live on a running VM, kept for the next start; without a host port a free one is picked (the new spec is the last of `ports` in the reply) |
| `DELETE /vms/{vm}/ports` | `?port=HOSTPORT` (or the full spec) | `fcvm unpublish` |

## Images, snapshots, volumes

| method, path | body | does |
|---|---|---|
| `GET /images` | | `fcvm images --json` |
| `POST /images` | `{"ref": "nginx:latest", "name": "optional"}` | start an import as a [background job](#background-jobs); returns the job |
| `DELETE /images/{image}` | | `fcvm rmi` |
| `GET /snapshots` | | `fcvm snapshot ls --json` |
| `POST /snapshots/{snap}/fork` | `{"name": "optional", "count": 1, "labels": {}}` | `fcvm fork`; returns `{"log": "..."}` |
| `DELETE /snapshots/{snap}` | | `fcvm snapshot rm` |
| `GET /volumes` | | `fcvm volume ls --json` |
| `DELETE /volumes/{volume}` | | `fcvm volume rm` |

## Templates

| method, path | body | does |
|---|---|---|
| `GET /templates` | | every template, built-in and yours, each with `name` and `builtin` |
| `POST /templates` | `{"name": "pydev", "image": "...", ...}` | save one (the fields of a [template](templates.md#managing-templates)); a 400 says what's wrong |
| `DELETE /templates/{name}` | | delete one of yours (built-in ones can't be deleted) |

To launch from a template, send its fields to `POST /vms` (the launch
dialog does that). A template's `ref` in that body imports a missing image
first.

## Warm pools

| method, path | body | does |
|---|---|---|
| `GET /pools` | | the [pools](templates.md#warm-pools), each with `ready` and its unclaimed `vms` |
| `POST /pools` | `{"name": "py", "template": "python-sandbox", "size": 3, "jail": true}` (or `"snapshot"`) | `fcvm pool set` |
| `DELETE /pools/{name}` | | `fcvm pool rm` |
| `POST /pools/{name}/claim` | `{"ttl": "2h", "idle_stop": "30m", "labels": {}}` (optional) | takes a ready VM; returns it as `GET /vms/{vm}`; 409 `pool_empty` when none is ready |

## Prometheus

`GET /metrics` (not under `/api`) takes the same bearer token and returns
the Prometheus text format. See [Metrics](web-console.md#metrics).

## Background jobs

Slow operations (image imports, builds) run as jobs:

| method, path | query | returns |
|---|---|---|
| `GET /jobs` | | the 20 most recent jobs, each with the last 3 lines of output |
| `GET /jobs/{id}` | `?from=N` | `{id, kind, title, status: running\|done\|failed, started, ended, lines: [...], next}`: output lines from offset `N`. Poll with `from=next` |

## Events

| method, path | query | returns |
|---|---|---|
| `GET /events` | `?since=N[&vm=NAME][&limit=N]` | `{"events": [...], "next": N}`: the state changes after event `N` (`vm.created`, `vm.started`, `vm.stopped`, `vm.exited`, `vm.deleted`, ...), oldest first; with `Accept: text/event-stream`, streamed as they happen. `410` when `N` is older than what's kept. See [Events](events.md) |

`GET /vms` carries `X-Fcvm-Seq`, the latest event when the list was made.

## Build projects

A build project is a directory with a Dockerfile (see
[Building images](images.md#building-images)). It lives under `builds/NAME/`, or is an existing directory
registered by path.

| method, path | body / query | does |
|---|---|---|
| `GET /builds` | | projects, with their last build |
| `POST /builds` | `{"name": "app"}` or `{"name": "app", "path": "/abs/dir"}` | a new empty project, or register an existing directory |
| `DELETE /builds/{p}` | | delete the project (a registered directory is only unregistered, never deleted) |
| `GET /builds/{p}/files` | | the project's files |
| `GET /builds/{p}/file` | `?path=Dockerfile` | `{"path", "content"}` for a text file up to 2 MB |
| `PUT /builds/{p}/file` | `?path=src/app.py`, raw body | write a file into the project |
| `DELETE /builds/{p}/file` | `?path=...` | delete a file or directory |
| `POST /builds/{p}/build` | `{"tag", "build_args": {}, "no_cache", "network", "allow"}` | start `fcvm build` as a job |

## Terminals (WebSockets)

`/ws/vms/{vm}/console` (the serial console) and `/ws/vms/{vm}/shell` (an
interactive shell through the exec agent) are WebSockets carrying raw
terminal bytes. They authenticate with the browser cookie and a same-origin
`Origin`, not with a bearer token. For scripted access, use the CLI
(`fcvm exec`, `fcvm console`) instead.
