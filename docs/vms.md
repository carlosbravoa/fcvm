# Running VMs

Everything about VMs once you have an image: creating them, getting into
them, moving files, publishing ports and attaching storage.

- [create, start, run](#create-start-run)
- [App VMs: the command, --idle, exit codes](#app-vms-the-command---idle-exit-codes)
- [Resources](#resources)
- [exec and shell](#exec-and-shell)
- [Copying files](#copying-files)
- [Console and logs](#console-and-logs)
- [Stopping and removing](#stopping-and-removing)
- [Published ports](#published-ports)
- [Volumes](#volumes)
- [Host directories](#host-directories)
- [Restart policies](#restart-policies)
- [Jailed VMs](#jailed-vms)
- [Listing and inspecting](#listing-and-inspecting)

## create, start, run

```sh
fcvm create web nginx-latest -p 8080:80    # define the VM (0.1 s, a few MB)
fcvm start web                             # boot it in the background
fcvm start -a web                          # ... or boot it and attach the console

fcvm run alpine-latest                     # create + start + attach; deleted when it ends
fcvm run nginx-latest -d -p 8080:80        # the same in the background; deleted when stopped
```

- **`create`** defines a VM from an image: its own writable layer on the
  shared image, and its settings in `vms/NAME/vm.json`. It keeps its disk
  until you `rm` it.
- **`run`** is for throwaway VMs, like `docker run --rm`.
  - App images: your terminal is attached to the app. Ctrl-C goes to the
    app, Ctrl-] detaches, and fcvm exits with the app's exit code.
  - System images: `run` opens a shell, or runs `-- CMD`, then stops and
    deletes the VM.

`run` takes the same options as `create`.

## App VMs: the command, --idle, exit codes

An app VM runs its image's command as its only process, like a container:

```sh
fcvm run alpine-latest -- echo hi               # -- CMD replaces the image's CMD, keeps its ENTRYPOINT
fcvm run nginx-latest --entrypoint sh -- -c 'nginx -v'   # --entrypoint replaces it ("" clears it)
fcvm create box python-3.13-slim --idle         # run nothing; stay up for exec
```

- **Lifetime.** When the command exits, the VM stops. `fcvm ls` shows
  `exited(N)`, and `run`/`start -a` exit with that code. Signals give
  128+signal, as Docker does.
- **`--idle`.** The VM stays up with no command, waiting for `exec`: the
  usual shape for sandboxes and dev boxes.
- **Configuration.** The image's env, workdir and user apply, as in Docker.

System VMs boot systemd instead, and run until stopped.

## Resources

| option | default | |
|---|---|---|
| `--vcpus N` | 2 (`VM_VCPUS`) | virtual CPUs |
| `--mem MiB` | 1024 (`VM_MEM_MIB`) | guest memory. The host only backs what the guest touches |
| `--disk SIZE` | 8G (`VM_DISK`) | the writable layer's size, sparse, so it takes only what's written |
| `--copy` | | a private full copy of the image instead of a layer (for images with committed layers, squash first) |

`fcvm ls` shows memory as used/allocated: what the host actually backs, out
of what the guest was given.

## exec and shell

Every VM runs an exec agent over vsock. It needs no network or sshd, so it
works in any image, even distroless ones:

```sh
fcvm exec web nginx -t                       # one command; exits with its status
fcvm exec -it web bash                       # interactive, on a TTY
fcvm exec -u postgres db psql                # as another user (name, uid, name:group, uid:gid)
fcvm exec -w /work -e DEBUG=1 box make test  # working directory and extra environment
fcvm exec --timeout 60 box ./long-task       # killed after 60 s; exit code 124
fcvm shell web                               # = exec -it web bash (or sh)
fcvm shell -u root unpriv                    # root shell in an image whose USER isn't root
```

- **Output.** Without `-t`, stdout and stderr stay separate, which suits
  scripts and agents. With `-t`, you get a real PTY, with window-size
  updates.
- **User and directory.** Commands run as the image's user, with its env
  and workdir, like `docker exec`. `-u` names are resolved inside the
  guest, so users created after import work too. The PTY belongs to that
  user, so `sudo` and `less` work.
- **System VMs** get the user's home as the working directory.
- **`fcvm ssh VM`** also works for system images, with the key in `ssh/`.

## Copying files

```sh
fcvm cp ./site web:/usr/share/nginx/html     # host -> VM (files or directories)
fcvm cp web:/var/log/nginx ./logs            # VM -> host
fcvm cp -L ./link web:/tmp                   # follow symlinks on the host side
```

`cp` works like `docker cp`: tar over exec. It needs `sh` and `tar` in the
image. The web console's Files tab browses, uploads, downloads and edits
files in any image, without needing either.

## Console and logs

```sh
fcvm console web      # the live serial console; Ctrl-] detaches, the VM keeps running
fcvm logs web         # console output of the current or last boot
fcvm logs -f web      # follow
```

For app VMs, the console is the app's output. For system VMs, it shows boot
messages and a `login:` prompt (no autologin), so use `fcvm shell` instead.

## Stopping and removing

```sh
fcvm stop web         # Ctrl-Alt-Del: the app gets SIGTERM, systemd shuts down; killed after 20 s
fcvm rm web           # delete the VM and its writable layer (volumes are kept)
```

## Published ports

```sh
fcvm create web nginx-latest -p 8080:80                  # 127.0.0.1:8080 -> VM port 80
fcvm create web nginx-latest -p 0.0.0.0:8080:80          # every interface: your LAN too
fcvm create web nginx-latest -p 192.168.1.5:8080:80      # one interface
```

- **Local by default.** Ports listen on `127.0.0.1`, reachable only from
  your machine. Publishing on another address is explicit.
- **Firewall.** With ufw active, LAN clients also need
  `sudo ufw allow 8080/tcp`.
- **TCP only**, relayed by a small user-space forwarder that exits with the
  VM.
- **Client address.** The guest sees connections coming from the bridge
  (`172.30.0.1`), not from the real client.
- **Direct access.** From the host, you can always reach a VM directly at
  its IP (`fcvm ls`), on any port.
- **Conflicts.** A port that's already taken stops the VM from starting.

## Volumes

Named volumes are ext4 disks in `volumes/`, attached as block devices. They
outlive VMs:

```sh
fcvm create db postgres-17 -v pgdata:/var/lib/postgresql/data   # created on first use (10G sparse)
fcvm create box alpine-latest -v tools:/opt/tools:ro             # read-only
fcvm volume create cache 20G
fcvm volume ls
fcvm volume rm cache
```

- **Ownership.** A new, empty volume takes the owner and mode of the
  directory it covers, as Docker volumes do, so non-root images can write to
  it.
- **Sharing.** A read-write volume can be attached to only one running VM
  at a time. `:ro` volumes can be shared.
- **Snapshots.** VMs with read-write volumes can't be snapshotted.

## Host directories

A value starting with `/`, `./`, `../` or `~` mounts a host directory live,
both ways, like a bind mount: `HOST/DIR[:GUEST/PATH][:ro]`.

```sh
fcvm create dev ubuntu-26.04 -v ~/src               # your ~/src, at ~/src in the VM (/root/src)
fcvm create box alpine-latest -v /srv/data          # a directory outside your home: the same path
fcvm run python-3.13-slim -v ./myproject:/work -- python /work/main.py   # or say where
fcvm mount dev ~/notes:~/notes:ro                   # add one to a VM, running or not
fcvm umount dev ~/notes                             # by its path in the VM, or its host directory
```

**Where it goes in the VM.** Name only the host directory, and fcvm picks
the guest path:
- **A directory inside your home** goes to the same place inside the home of
  the image's user: `~/src` is `/root/src` for an image that runs as root,
  and `/home/app/src` for one whose user is `app`. That home comes from the
  image's own `/etc/passwd`.
- **Any other directory** goes to the same path: `/srv/data` is
  `/srv/data`.
- **Saying where:** a guest path after a `:` is either absolute (`/work`) or
  starts with `~`, the image user's home (`~/data`).

- **Live.** Changes on either side are visible on the other at once, with
  no cache to go stale. git, editors and servers work.
- **Ownership.** Files appear owned by the image's user (root for system
  images), so non-root images can write. On the host they belong to you,
  and `chown` in the guest is accepted and ignored.
- **Confinement.** Nothing outside the directory is reachable. A symlink
  to `/etc` points at the guest's `/etc`, not the host's.
- **Speed.** About 300 MB/s for large files and about 1 ms per small-file
  operation. That's fine for source trees. Keep `node_modules` or
  virtualenvs on the VM's own disk or a volume.
- **Changing them.** `mount`/`umount` on a running VM take effect at once
  and are saved for the next start. The web console's Files tab does the
  same.
- **Snapshots.** VMs with host directories can't be snapshotted.
- **Networking.** None needed: it works with `--net none` (9P over vsock;
  see [How it works](internals.md#host-directories)).

## Restart policies

```sh
fcvm create web nginx-latest -p 8080:80 --restart unless-stopped
fcvm update web --restart on-failure
```

`no` (default), `on-failure`, `unless-stopped` or `always`, as in Docker.
They're applied by the supervisor in `fcvm serve`, normally the fcvm
service. See [The fcvm service](service.md).

## Jailed VMs

```sh
fcvm create box alpine-latest --idle --jail      # or JAIL=1 for every new VM
```

`--jail` runs the VM's Firecracker under the jailer: its own uid, a chroot,
cgroup limits and a network namespace. It needs `fcvm jail-setup` once.
Everything on this page works the same for jailed VMs. See
[Security](security.md#jailed-vms-in-detail).

## Listing and inspecting

```sh
fcvm ls                 # NAME, STATE, IP, IMAGE, MEM (used/allocated), DISK, network/ports/volumes
fcvm ls --json
fcvm inspect web        # everything about one VM, as JSON (including its last exit)
```
