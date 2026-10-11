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
- [Lifetimes: --ttl and --idle-stop](#lifetimes---ttl-and---idle-stop)
- [Secrets](#secrets)
- [Forwarding your credentials](#forwarding-your-credentials)
- [Jailed VMs](#jailed-vms)
- [Without the exec agent](#without-the-exec-agent)
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

Both take a [template](templates.md) instead of an image, for recipes you
launch often: `fcvm create box --template python-sandbox`.

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

**Taking memory back.** A running VM keeps the host memory it has touched,
even when it no longer needs it. The balloon takes it back without a
restart:

```sh
fcvm balloon box 512      # the guest gives up 512 MiB to the balloon; the host frees those pages
fcvm balloon box          # the balloon's size and the guest's view (total, free, available)
fcvm balloon box 0        # give it all back
```

The guest decides which pages to hand over (free memory first; with
`deflate_on_oom` it takes memory back from the balloon rather than kill
processes). Useful for idle sandboxes that stay up for `exec`, or a warm
pool. `inspect` shows `balloon_mib`; the API has `GET`/`POST
/vms/{vm}/balloon`. Every VM started by this fcvm has the device; one
started earlier needs a restart to get it.

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
- **In the background.** `exec -d` starts the command as a *job* and
  prints its id instead of waiting, for servers and long builds:

  ```sh
  job=$(fcvm exec -d -w /app box python -m http.server 8000)
  fcvm jobs box                 # ID, STATUS (running, exited(N), lost), STARTED, COMMAND
  fcvm jobs box $job            # its output so far (stdout, then stderr); exits with its status once it ended
  fcvm jobs box $job -f         # follow
  fcvm jobs box $job --kill     # end it (the agent kills its process group in the VM)
  fcvm jobs box --clear         # forget finished jobs
  ```

  Output goes to `vms/VM/jobs/ID.out` and `ID.err`. A job is `lost` when
  its connection dropped: the VM stopped, or it was killed. `--timeout`
  applies to jobs too. The API (`POST /vms/{vm}/exec` with `background`)
  and the MCP server (`exec` with `background`, then `job`, `jobs`,
  `kill_job`) have the same.
- **`fcvm ssh VM`** also works for system images. Each VM gets its keys
  at `create` (fcvm's key in `ssh/` and your `~/.ssh/id_*.pub`), and its
  own host key, which `fcvm ssh` checks. So `ssh root@IP` with your own key
  works too.

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

**While it runs.** Ports can be added and removed without a restart, and
the change is kept for the next start:

```sh
fcvm publish web 8081:80              # at once if the VM is running
fcvm publish box :8000                # pick a free host port; prints the spec, e.g. 43127:8000
fcvm publish box 0.0.0.0:9000:9000    # on every interface
fcvm unpublish web 8081               # by host port, or the full spec
```

The web console's instance page has the same (× and **publish…** next to
the ports), the API has `POST`/`DELETE /vms/{vm}/ports`, and agents get
`publish_port`, which returns the URL to reach a server in the sandbox.

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

## Lifetimes: --ttl and --idle-stop

```sh
fcvm run python-3.13-slim -d --idle --ttl 2h          # gone two hours after it boots
fcvm create box alpine-latest --idle --idle-stop 30m  # stopped after 30 minutes without use
fcvm update box --ttl 1d --idle-stop none             # change or clear them
```

Sandboxes tend to be forgotten, by people and by agents. A lifetime ends
them on their own:
- **`--ttl DURATION`** stops the VM that long after each boot.
- **`--idle-stop DURATION`** stops it after that long without use: an
  `exec`, `shell` or `cp`, a file operation from the console or the API, or
  the console's Shell. Reading stats doesn't count. Before the first use,
  the time counts from the boot.

Durations are `90s`, `30m`, `2h` or `1d` (a bare number is seconds). The
stop is a normal `fcvm stop`: restart policies then leave the VM alone, a
throwaway VM (`run`) is deleted, and `fcvm inspect` shows `expired: "ttl"`
or `"idle"` until the next start. `fcvm ls` shows `ttl:2h` and
`idle-stop:30m`, and `inspect` gives `expires_at` and `idle_expires_at` for
a running VM. Like restart policies, lifetimes are applied by the supervisor
in `fcvm serve`, normally the fcvm service; without it, nothing stops the VM.

## Secrets

```sh
fcvm create box python-3.13-slim --idle --secret API_KEY=sk-abc           # a value
fcvm create box python-3.13-slim --idle --secret GITHUB_TOKEN              # from your environment
fcvm create box python-3.13-slim --idle --secret TLS_KEY=@~/certs/key.pem  # a file's content
fcvm update box --secret API_KEY=sk-new --unset-secret TLS_KEY             # from its next start
```

A secret reaches the VM at boot over vsock and lives in a tmpfs at
`/run/secrets/NAME` (mode 0400, owned by the image's user), and as an
environment variable `NAME` of the app and of every `exec`/`shell`
session. It's never written to the VM's disk, so `commit` can't carry it
into an image, and it isn't in `vm.json` either: it's in
`vms/VM/secrets.json` (mode 0600) on the host.

- **System VMs** get the files in `/run/secrets` and the variables in
  `exec` sessions; services started by systemd read the files.
- **Snapshots** contain the VM's memory, secrets included, and forks get
  the same secrets.
- **Templates** can name secrets (`"secrets": ["GITHUB_TOKEN"]`): the
  values come from the environment of whoever creates the VM, so a template
  never holds one.
- **The API** takes `secrets: {"NAME": "value"}` in `POST /vms` and
  `/update` (and `unset_secrets`); the **MCP server** passes the ones named
  in `FCVM_MCP_SECRETS` from its own environment, and gives the agent no
  tool to see or set them ([Agents](agents.md#secrets)).
- `fcvm inspect` lists the names, never the values.

## Forwarding your credentials

```sh
fcvm create dev ubuntu-26.04 --forward ssh-agent,git   # or either one
fcvm update dev --forward none                           # from its next start
```

Instead of copying keys or tokens into a VM, let it use yours through the
host, the way `ssh -A` does:

- **`ssh-agent`.** The host relays its agent (`SSH_AUTH_SOCK`) over vsock,
  and the VM gets `SSH_AUTH_SOCK=/run/fcvm/ssh-agent.sock` for the app and
  every `exec`/`shell` session. `git clone git@github.com:...` and `ssh`
  work; the private keys stay on the host. The agent socket is the one of
  whoever runs `fcvm start` (or the one recorded at `create`, when the
  fcvm service starts the VM). Run `ssh-add -c` on the host to confirm each
  use.
- **`git`.** The VM's git asks the host for credentials
  (`git credential fill`): whatever your helpers hold (`gh auth`, a
  credential store, a keychain) works for `https://` remotes in the VM.
  Only lookups are forwarded; the VM can't store or erase anything.

Both are logged in `vms/VM/forward.log`. In system VMs, `exec` sessions
have the variables; ssh logins can `export
SSH_AUTH_SOCK=/run/fcvm/ssh-agent.sock` themselves.

**What this trusts.** A forwarded agent signs for whatever is running in
the VM while it runs, and the git relay answers for any host your helpers
know. That's the deal with any forwarding; use it with code you'd give
your laptop, not with `run --jail --allow` sandboxes for untrusted code.

## Jailed VMs

```sh
fcvm create box alpine-latest --idle --jail      # or JAIL=1 for every new VM
```

`--jail` runs the VM's Firecracker under the jailer: its own uid, a chroot,
cgroup limits and a network namespace. It needs `fcvm jail-setup` once.
Everything on this page works the same for jailed VMs. See
[Security](security.md#jailed-vms-in-detail).

## Without the exec agent

```sh
fcvm create appliance my-image --no-agent
```

Every VM normally runs fcvm's exec agent, which comes from fcvm's init, not
the image. Only the host can reach it (see [Security](security.md#the-virtual-machine-boundary)).
For the very specific case of a VM with nothing inside that accepts
commands, `--no-agent` leaves it out. You lose `exec`, `shell` and `cp`,
live `mount`/`umount` on the running VM, snapshots and forks, metrics from
inside the guest, and the MCP server's tools. Host directories given at
create time, the serial console, logs, published ports and host-side stats
still work. `fcvm ls` marks it `no-agent`.

`create` lists all that and asks before going on (`FCVM_NO_AGENT_OK=1`
answers yes, for scripts). It can't be changed later: recreate the VM to
get the agent back. It's a command-line option only, and `run` takes it for
app images.

## Listing and inspecting

```sh
fcvm ls                 # NAME, STATE, IP, IMAGE, MEM (used/allocated), DISK, network/ports/volumes
fcvm ls --json
fcvm inspect web        # everything about one VM, as JSON (including its last exit)
```
