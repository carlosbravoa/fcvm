# Web console

```sh
./fcvm serve            # prints http://127.0.0.1:8686/?token=...  (open it in a browser)
```

With the [fcvm service](service.md) installed, the console is always
running. `./fcvm service status` prints its URL.

![Dashboard](img/console-dashboard.png)

A cloud-console-like UI for this host:
- **Dashboard:** host CPU, memory and disk; VM memory actually in use vs
  allocated; network slots; running instances with their CPU and memory.
- **Instances:** start, stop, restart, delete, snapshot, commit to image.
  Each instance page has live charts (CPU, memory, disk I/O, network; the
  last 10 minutes, sampled every 2 s) with a table view, details, the
  restart policy, logs, and the egress policy with its allow/deny log.
- **Browser terminals:** a **Shell** tab (an interactive shell through the
  exec agent, optionally as another user) and a **Console** tab (the live
  serial console).
- **Launch:**
  - name, image, vCPUs and memory;
  - network: full, an allowlist built from presets and extra hosts, or
    none;
  - published ports, volumes and host directories;
  - for app images: run the image's command, stay idle for the shell, or
    run a custom command;
  - a restart policy, and the jailer (checked by default when installed).
- **Images:** import from a registry or a local archive (a background job
  with progress), launch from an image, delete.
- **Files** (instance tab):
  - browse the VM's filesystem, upload (with progress), download, edit
    text files in place, create folders, rename, delete;
  - add or remove host directories, live on a running VM;
  - it works in any image, even without a shell;
  - writes are atomic, and edited files keep their owner and mode.
- **Builds:**
  - projects from a template (Python, Alpine, Ubuntu or blank), or an
    existing directory on the host;
  - edit the Dockerfile and files in the browser, or upload files and
    folders;
  - build with a name, build args, a network policy and no-cache, while the
    log streams live beside a step list that shows cached steps;
  - "Launch it" opens the launch dialog on the result.
- **Snapshots:** fork, delete. **Volumes:** list, delete.

![An instance: details, restart policy and live charts](img/console-instance.png)

Light and dark themes follow your OS, with a toggle. Everything works
offline: xterm.js is vendored, and there's no build step or CDN.

## Security

- **Local only.** It binds `127.0.0.1` only.
- **Token.** The printed URL carries a token, which the browser exchanges
  for an HttpOnly, SameSite=Strict cookie. A plain `fcvm serve` makes a new
  token on every start; the service keeps one.
- **Request checks.** Requests must carry a localhost `Host` header, which
  blocks DNS rebinding. Changes and terminals need a same-origin `Origin`,
  which blocks cross-site requests.
- **Content-Security-Policy.** A strict policy allows only the app's own
  scripts.
- **Treat the URL like a password.** The browser shell is a root shell in
  your VMs. Remote access with real accounts is
  [on the roadmap](roadmap.md#web-console).

To reach the console from another machine today, use an SSH tunnel:
`ssh -L 8686:127.0.0.1:8686 yourhost`.

## Scripting

The console is a client of fcvm's [HTTP API](api.md). Scripts can use the
same API with the token as a bearer token.

How the server is built is in
[How it works](internals.md#the-web-console).
