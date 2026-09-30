# Use cases

Walkthroughs of what fcvm is typically used for. Each one links to the
pages that go deeper. They assume you've done the
[quick start](../README.md#quick-start).

- [A sandbox for your coding agent](#a-sandbox-for-your-coding-agent)
- [Run code you don't trust](#run-code-you-dont-trust)
- [Prepare once, fork many](#prepare-once-fork-many)
- [A development machine](#a-development-machine)
- [A small service that stays up](#a-small-service-that-stays-up)
- [Build an image from a Dockerfile](#build-an-image-from-a-dockerfile)

## A sandbox for your coding agent

Give an agent its own machines to work in, instead of your laptop. Register
fcvm's MCP server. This shows Claude Code, but any MCP client works:

```sh
./fcvm jail-setup    # recommended: sandboxes are then jailed automatically
claude mcp add fcvm -e FCVM_MCP_NETWORK=@pypi,@github -- "$PWD/fcvm" mcp
```

The agent gets tools to:
- pull images and create sandboxes;
- run commands, with exit codes and separate stdout and stderr;
- read and write files, and copy them in or out;
- commit a prepared sandbox as an image, and snapshot and fork it.

`FCVM_MCP_NETWORK` pins what sandboxes can reach. Here that's PyPI and
GitHub over HTTP(S), and nothing else, whatever the agent asks for. Try a
prompt such as *"clone my repo into an fcvm sandbox and run its tests"*.

Worth knowing:
- **Every sandbox is a VM.** A malicious package the agent installs is
  stuck in that VM, which can't reach your host's services or other VMs.
- **Prepared environments start fast.** After the agent installs
  dependencies, `commit_vm` saves an image, so new sandboxes start ready.
  `snapshot_vm` plus `fork` goes further: running copies in about 150 ms,
  for parallel attempts or rollback.
- **Every request is logged.** `egress_log`, or `fcvm egress VM`, shows
  what a sandbox tried to reach.

More: [Agents (MCP)](agents.md), [Security](security.md).

## Run code you don't trust

A tool from the internet, a repository's build script, a package whose
install hooks you'd rather not run on your machine:

```sh
./fcvm import node:22-slim
# the project read-only, network limited to the npm registry, and the VM gone afterwards
./fcvm run node-22-slim --jail --allow @npm -v ./suspicious-pkg:/src:ro -- \
    sh -c 'cp -r /src /work && cd /work && npm install && npm test'
```

- **`--jail`** runs Firecracker itself as an unprivileged, chrooted,
  cgroup-limited user, as a second wall behind the VM boundary (needs
  `jail-setup`).
- **`--allow @npm`** gives the VM a route to nothing but an egress proxy,
  which lets through only the npm registry. `--net none` removes the
  network entirely.
- **`-v ./dir:/src:ro`** shows the VM your files without letting it change
  them. Leave it out and use `fcvm cp` to copy things in instead.
- **`run`** deletes the VM when the command ends. `fcvm egress` isn't
  available after that, so use `create` and `start` if you want to look at
  what it tried to reach.

More: [Networking and egress](networking.md), [Security](security.md).

## Prepare once, fork many

Installing dependencies is slow. Forking a VM that already has them isn't:

```sh
./fcvm create base python-3.13-slim --idle && ./fcvm start base
./fcvm cp ./myproject base:/work
./fcvm exec -w /work base pip install -r requirements.txt
./fcvm snapshot base ready                          # memory, disk and running processes

./fcvm fork ready t -n 4                            # t-1 .. t-4, each running in ~150 ms
for i in 1 2 3 4; do ./fcvm exec -w /work t-$i pytest -q tests/part$i & done; wait
./fcvm rm t-2 && ./fcvm fork ready t-2              # roll back one to the prepared state
```

Each fork has its own disk, IP, MAC and hostname, and starts from the exact
moment of the snapshot, with warm caches and imported modules. Forks share
the snapshot's memory copy-on-write, so four forks cost little more than
one.

For a starting point that doesn't need running processes, `commit` is
simpler. It saves the disk as an image, and new VMs boot from it:

```sh
./fcvm stop base && ./fcvm commit base myproject-env
./fcvm run myproject-env -- python /work/main.py
```

More: [Snapshots and fork](snapshots.md), [Images](images.md#commit-a-vm-as-an-image).

## A development machine

A full Ubuntu VM with systemd, your source tree mounted live, and ssh:

```sh
./fcvm base                                          # once: the ubuntu-26.04 system image
./fcvm create dev ubuntu-26.04 --vcpus 4 --mem 4096 -v ~/src:/src --restart unless-stopped
./fcvm start dev
./fcvm shell dev          # or: ./fcvm ssh dev
```

- **Your source tree.** Edit in `~/src` on the host and build or run in
  `/src` in the VM. Changes are visible on both sides at once.
- **Packages and services.** `apt install` works as on any Ubuntu server,
  and services are enabled and started.
- **It stays up.** With the fcvm service installed, `--restart
  unless-stopped` brings the VM back after a crash or a host reboot.

More: [Images](images.md#app-and-system-images),
[Running VMs](vms.md#host-directories), [The fcvm service](service.md).

## A small service that stays up

Run a container image as a long-lived service, with container convenience
and VM isolation:

```sh
./fcvm service install                               # once
./fcvm import nginx:latest
./fcvm create web nginx-latest -p 8080:80 -v site:/usr/share/nginx/html --restart unless-stopped
./fcvm start web
curl http://localhost:8080/
```

- **Restarts.** If nginx crashes or the host reboots, the supervisor
  starts it again, with backoff. `fcvm stop web` is respected until you
  start it again.
- **Local only by default.** Ports listen on `127.0.0.1`. To serve your
  LAN, publish with `-p 0.0.0.0:8080:80` (and open the port in ufw if it's
  active).
- **Your content survives.** The site lives in the named volume `site`, so
  it outlives the VM.

More: [The fcvm service](service.md), [Running VMs](vms.md#published-ports).

## Build an image from a Dockerfile

```sh
./fcvm build -t myapp --allow @pypi ./myapp     # RUN steps can reach only PyPI
./fcvm run myapp
```

fcvm builds from a common Dockerfile subset: `FROM`, `RUN`, `COPY`, `ENV`,
`WORKDIR`, `USER`, `CMD`, `ENTRYPOINT`, and more. Each step runs in a VM and
is cached, so editing your code re-runs only the steps after the `COPY`
that changed. You can also build from the web console, which has a file
editor and a live log.

More: [Images](images.md#building-images).
