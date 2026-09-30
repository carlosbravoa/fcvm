# Images

An fcvm image is a read-only ext4 disk (`images/NAME.ext4`) plus its
configuration (`images/NAME.json`). Every VM created from an image shares
that disk read-only and writes to its own small layer, so creating a VM
takes about 0.1 s and a few MB.

- [Where images come from](#where-images-come-from)
- [App and system images](#app-and-system-images)
- [Importing from a registry](#importing-from-a-registry)
- [Importing local images](#importing-local-images)
- [The Ubuntu base image](#the-ubuntu-base-image)
- [Building images](#building-images)
- [Commit a VM as an image](#commit-a-vm-as-an-image)
- [Layers and squash](#layers-and-squash)
- [Managing images](#managing-images)
- [Images and the kernel](#images-and-the-kernel)

## Where images come from

| source | command | type |
|---|---|---|
| a registry (Docker Hub, ghcr.io, quay.io, ...) | `fcvm import nginx:latest` | app |
| a local `docker save` / `podman save`, an OCI layout or archive | `fcvm import ./tool.tar` | app |
| a Dockerfile | `fcvm build -t NAME ./dir` | app, or system when `FROM` a system image |
| a stopped VM's changes | `fcvm commit VM NAME` | same as the VM's image |
| built from scratch with `mmdebstrap` | `fcvm base` | system (Ubuntu 26.04) |

`fcvm images` lists them, with their type, size, what uses each and where
it came from.

## App and system images

Every fcvm image, of either type, boots as a full Firecracker microVM under
KVM: its own kernel, virtual CPUs, memory, disks and network card. No Docker,
containerd or runc is involved. The type only says what runs as PID 1:

| | **app** | **system** |
|---|---|---|
| comes from | container images (Docker Hub, registries, `docker save`), and builds and commits on them | the Ubuntu base (`fcvm base`), and builds and commits on it |
| PID 1 | `fc-init` runs the image's ENTRYPOINT + CMD as its user, with its env and workdir, like `docker run` | `fc-init` sets up the root and hands PID 1 to systemd: journald, udev, dbus, sshd, a login prompt |
| lifetime | as long as that process; its exit code is fcvm's | until stopped or shut down from inside |
| boot / memory | ~0.5 s, tens of MB | ~2.3 s, more |
| `run IMAGE` | attached to the app's output | opens a shell; `exit` deletes the VM |
| `-- CMD`, `--idle`, `--entrypoint` | yes | no |
| good for | apps, one-off commands, agent sandboxes | long-lived dev machines, services, cron |

**Example: Ubuntu both ways.** The same distribution can be either type:

```sh
./fcvm import ubuntu:latest   # app image "ubuntu-latest", from Docker Hub
./fcvm run ubuntu-latest      # a bash prompt; `exit` and the VM is gone

./fcvm base                   # system image "ubuntu-26.04", built with mmdebstrap
./fcvm run ubuntu-26.04       # boots systemd, then a root shell
```

| | `./fcvm import ubuntu:latest` | `./fcvm base` |
|---|---|---|
| type | **app** (like any Docker image) | **system** |
| PID 1 | fcvm's small init, which runs `bash` | systemd |
| services, ssh, journald, timers | no | yes, it boots like a server |
| feels like | `docker run -it ubuntu` | a Multipass or cloud VM |
| size | 128M | 266M |

**What that means in practice:**
- **Services.** In the app image you can `apt install` anything, but
  nothing starts in the background, because a container image has no init
  system. Install nginx and you start it yourself, as in Docker. In the
  system image, `apt install nginx` leaves a running, enabled service, as
  on any Ubuntu server.
- **Compared with Docker.** An app VM has the same filesystem, command, env
  and user as Docker running the same image. The difference is isolation:
  Docker shares the host kernel (namespaces, cgroups), while an app VM has
  its own guest kernel behind hardware virtualization.
- **The cost** of that isolation is about 0.5 s of boot and a few tens of
  MB of memory, which is why fcvm suits running untrusted code.

## Importing from a registry

```sh
./fcvm import nginx:latest                 # -> nginx-latest
./fcvm import ghcr.io/org/app:v1.2         # -> app-v1.2
./fcvm import python:3.13-slim mypython    # choose the name
./fcvm import redis@sha256:<digest>        # pinned to an exact image
```

**Naming.** An image is named after the last component of the reference
plus its tag (`nginx:latest` → `nginx-latest`, bare `alpine` → `alpine`),
unless you give a name.

**What the importer does** (`lib/oci_import.py`, standard-library Python):
- it resolves multi-arch indexes to your architecture;
- it downloads the layers, verifies each digest, and caches them in
  `cache/blobs`;
- it flattens the layers, honouring whiteouts, into one ext4 disk, which
  `mkfs.ext4 -d` builds without root;
- it keeps the image's configuration (Entrypoint, Cmd, Env, WorkingDir,
  User, exposed ports) in the image's `/.fcvm/` and in `images/NAME.json`.

**Private registries.** Set `REGISTRY_USER` and `REGISTRY_PASSWORD` (a
token works as the password) for the import:

```sh
REGISTRY_USER=me REGISTRY_PASSWORD="$(cat ~/.ghcr-token)" ./fcvm import ghcr.io/me/private:1.0
```

Docker's `config.json`, credential helpers and signature verification
aren't supported yet ([roadmap](roadmap.md#e3-supply-chain)).

**Re-importing.** Importing over an existing name replaces it, unless VMs
still use it as their base. Then the import refuses, and you import under
another name.

## Importing local images

Images never have to be pushed to a registry:

```sh
docker save myorg/tool:1.0 -o tool.tar && ./fcvm import tool.tar   # -> tool-1.0
./fcvm import oci:./layout:v2 mytool                               # a tag from an OCI layout directory
./fcvm import oci-archive:./image.tar                              # an OCI archive
```

`docker save` and `podman save` tarballs work in both the classic format and
Docker 25+'s OCI-style one.

## The Ubuntu base image

```sh
./fcvm base                    # images/ubuntu-26.04.ext4
./fcvm base myubuntu           # under another name
```

`fcvm base` builds a minimal Ubuntu 26.04 system image, rootless:
- **How.** `mmdebstrap` runs in a user namespace, and `mkfs.ext4 -d`
  turns its tarball into the disk.
- **What's in it.** systemd, udev, dbus, networking tools, OpenSSH, curl,
  sudo and an editor. That's `BASE_PACKAGES`; add your own in `fcvm.conf`.
- **Other settings.** Suite, mirror and disk size come from
  `UBUNTU_SUITE`, `UBUNTU_MIRROR` and `BASE_SIZE`. See
  [Configuration](configuration.md#images).
- **In the VM.** It boots systemd, and `fcvm ssh VM` logs in with the
  project key in `ssh/`. `fcvm shell` needs no network or ssh at all.

## Building images

`fcvm build` takes a Dockerfile subset, from `Fcvmfile` or `Dockerfile` in
the build context, or `-f`:

```dockerfile
ARG PYVER=3.13
FROM python:${PYVER}-slim          # an fcvm image, or a registry ref (imported if missing)
ENV APP_HOME=/app PIP_ROOT_USER_ACTION=ignore
WORKDIR $APP_HOME
COPY requirements.txt .
RUN pip install -q -r requirements.txt
COPY src/ ./src/
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 8000
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--greeting", "hello"]
```

```sh
./fcvm build -t myapp --allow @pypi ./myapp   # RUN steps get only PyPI
./fcvm run myapp                               # python -m src.main --greeting hello
./fcvm run myapp -- --greeting hi              # replaces CMD, keeps ENTRYPOINT
```

- **Supported:** `FROM` (a single stage), `RUN` (shell and JSON forms),
  `COPY`/`ADD` (local sources and globs, `--chown`, `.dockerignore`, and
  `ADD` of a local tar extracts it), `ENV`, `ARG`/`--build-arg`, `WORKDIR`,
  `USER`, `CMD`, `ENTRYPOINT`, `EXPOSE`, `LABEL` (ignored). Variables are
  substituted as Docker does.
- **Not supported:** multi-stage builds, `COPY --from`, `ADD <url>`,
  `HEALTHCHECK`, `SHELL`, `ONBUILD`, `FROM scratch`. These fail with a clear
  error instead of being ignored. `COPY` needs `sh` and `tar` in the image.
- **Caching.** Each filesystem step (`RUN`, `COPY`, `ADD`, `WORKDIR`) runs
  in a fresh VM booted from the previous step's result, and is cached.
  Editing application code after a `pip install` re-runs only the steps
  from the changed `COPY` on. `--no-cache` runs everything in one VM.
- **The result** is always the FROM image plus one layer.
- **Speed:** a first build takes its `RUN` steps' time plus about 1.5 s
  per filesystem step. A rebuild with nothing changed takes about 2 s.
- **Network:** full by default. `--net none` and `--allow` restrict the
  `RUN` steps, as for VMs.
- **FROM a system image** works too: `RUN` steps go through the systemd
  VM's agent, and `CMD`/`ENTRYPOINT` are refused because systemd is the
  init.
- **Build cache.** It lives in hidden images (`fcvm images --all`), and
  `fcvm prune` removes it. Built images don't depend on it.

The web console has a build page with templates, a file editor and a live
log ([Web console](web-console.md)). How caching works is in
[How it works](internals.md#builds).

## Commit a VM as an image

```sh
./fcvm stop box
./fcvm commit box box-ready          # box's changes, as a new image on top of box's image
./fcvm run box-ready                 # starts from that state
```

- **Speed.** `commit` saves the stopped VM's writable layer as a read-only
  layer on its image. It's instant and needs no root, because nothing is
  merged.
- **Settings.** Per-VM settings aren't saved: a command given with
  `create -- CMD` or `--idle` stays with the VM, and the new image keeps its
  parent's command.
- **`--copy` VMs** have a private full disk, which becomes a standalone
  image instead.
- **Identity scrubbing.** Committing a VM that has booted systemd removes
  its machine-id, SSH host keys, random seed and journal from the layer.
  Every VM from the new image generates its own at first boot.

## Layers and squash

Each committed layer is a separate virtual disk, and Firecracker has about
19 device slots in total (shared with volumes, network and vsock). So keep
layer chains short:

```sh
./fcvm squash box-ready box-flat      # merge all of box-ready's layers into one
```

Squash merges layers in a throwaway VM, keeping deletions, owners, modes,
setuid bits, xattrs (such as file capabilities), hardlinks and timestamps.
A three-layer chain merges in about a second.

## Managing images

```sh
./fcvm images                  # NAME, TYPE, SIZE, USED-BY, SOURCE
./fcvm images --json
./fcvm rmi old-image           # refuses while VMs, snapshots or other images use it
./fcvm prune                   # the build cache and leftover build VMs
```

## Images and the kernel

Images contain no kernel, not even the Ubuntu base. Every VM boots the one
kernel built by `fcvm kernel`, each VM its own instance of it, as with
Docker, where containers share the host's kernel:
- `uname -r` shows the fcvm kernel in every VM.
- Kernel upgrades need no image rebuilds.
- `modprobe` does nothing, because the kernel is monolithic.

Features come from the kernel configuration, which you can extend. See
[Configuration](configuration.md#the-guest-kernel).
