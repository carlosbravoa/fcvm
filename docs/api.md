# HTTP API

`fcvm serve` (and the fcvm service, which runs it at boot) exposes the same
operations as the CLI as JSON over HTTP on `127.0.0.1`. The web console is
built on this API, and scripts can use it too. Every change goes through the
fcvm CLI, so the API behaves exactly like the command line: the same
validation, the same errors, the same locks.

- [Connecting](#connecting)
- [Conventions](#conventions)
- [Host](#host)
- [VMs](#vms)
- [Files and host directories in a VM](#files-and-host-directories-in-a-vm)
- [Images, snapshots, volumes](#images-snapshots-volumes)
- [Background jobs](#background-jobs)
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

## Conventions

- Bodies are JSON (`Content-Type` isn't checked). Responses are JSON unless
  noted.
- Names (VMs, images, snapshots, volumes, projects) match
  `[A-Za-z0-9_][A-Za-z0-9_.-]*`.
- Errors come back as `{"error": "message"}` with a 4xx or 5xx status. A CLI
  error is a 400 carrying the CLI's message, for example
  `{"error": "VM 'web' is running (fcvm stop web first)"}`. A command that
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
| `POST /vms/{vm}/update` | `{"restart": "no\|on-failure\|unless-stopped\|always"}` | `fcvm update --restart` |
| `POST /vms/{vm}/snapshot` | `{"name": "snap"}` | `fcvm snapshot` |
| `POST /vms/{vm}/commit` | `{"image": "name"}` | `fcvm commit` (the VM must be stopped) |
| `DELETE /vms/{vm}` | | stop it if running, then delete it |
| `GET /vms/{vm}/stats` | | `{"samples": [...]}`: the last 10 minutes, every 2 s. Fields: `cpu_pct`, `rss`, `disk_read_bps`, `disk_write_bps`, `net_rx_bps`, `net_tx_bps` |
| `GET /vms/{vm}/logs` | | `{"log": "..."}`: console output, the last 200 KB |
| `GET /vms/{vm}/egress` | | `{"text": "..."}`: a restricted VM's allowlist and recent decisions |

`POST /vms` body:

```json
{
  "name": "box", "image": "python-3.13-slim", "vcpus": 2, "mem_mib": 1024,
  "network": "full | none | restricted", "allow": ["@pypi", "github.com"],
  "ports": ["8080:80"], "volumes": ["cache:/root/.cache", "/home/me/src:/work:ro"],
  "idle": true, "command": "shell command (app images, when not idle)",
  "jail": true, "restart": "unless-stopped", "start": true
}
```

Only `name` and `image` are required. `allow` is used with
`"network": "restricted"`. The response is the new VM, as `GET /vms/{vm}`.

Fields worth knowing in a VM object (as `fcvm inspect`, plus `vm.json`):

| field | meaning |
|---|---|
| `state` | `running`, `stopped`, or `exited` (an app VM whose process ended; see `exit_code`) |
| `restart` | the restart policy |
| `stopped_by_user` | stopped with `fcvm stop`, so restart policies leave it alone |
| `last_exit` | how it last ended: `code` (an app's exit code), `fc_status` (Firecracker's: 0 = the guest shut down; negative = killed by that signal), `stale` (it was running when the host crashed or rebooted), `at` (Unix time) |
| `supervisor` | `null`, or `{"restart_at", "attempts", "error"}` while a restart is pending or after a failed one |
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
| `POST /vms/{vm}/mounts` | `{"host": "/abs/dir", "path": "/guest/path", "ro": false}` | `fcvm mount`: a live host directory |
| `DELETE /vms/{vm}/mounts` | `?path=/guest/path` | `fcvm umount` |

## Images, snapshots, volumes

| method, path | body | does |
|---|---|---|
| `GET /images` | | `fcvm images --json` |
| `POST /images` | `{"ref": "nginx:latest", "name": "optional"}` | start an import as a [background job](#background-jobs); returns the job |
| `DELETE /images/{image}` | | `fcvm rmi` |
| `GET /snapshots` | | `fcvm snapshot ls --json` |
| `POST /snapshots/{snap}/fork` | `{"name": "optional", "count": 1}` | `fcvm fork`; returns `{"log": "..."}` |
| `DELETE /snapshots/{snap}` | | `fcvm snapshot rm` |
| `GET /volumes` | | `fcvm volume ls --json` |
| `DELETE /volumes/{volume}` | | `fcvm volume rm` |

## Background jobs

Slow operations (image imports, builds) run as jobs:

| method, path | query | returns |
|---|---|---|
| `GET /jobs` | | the 20 most recent jobs, each with the last 3 lines of output |
| `GET /jobs/{id}` | `?from=N` | `{id, kind, title, status: running\|done\|failed, started, ended, lines: [...], next}`: output lines from offset `N`. Poll with `from=next` |

## Build projects

A build project is a directory with a Dockerfile (see "Building images" in
the README). It lives under `builds/NAME/`, or is an existing directory
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
