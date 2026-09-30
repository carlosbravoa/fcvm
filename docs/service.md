# The fcvm service

Without the service, fcvm is a set of commands: nothing runs unless you run
it, and after a reboot you run `fcvm net-up` again. The service makes fcvm
behave like a daemon: the network comes up at boot, the web console and API
stay available, and VMs with a restart policy stay up.

- [Install](#install)
- [What gets installed](#what-gets-installed)
- [Restart policies](#restart-policies)
- [How the supervisor behaves](#how-the-supervisor-behaves)
- [Shutdown and boot](#shutdown-and-boot)
- [Status, logs and the console URL](#status-logs-and-the-console-url)
- [Without the service](#without-the-service)
- [Updating and removing](#updating-and-removing)

## Install

```sh
./fcvm service install              # sudo, once; --port N for another console port
./fcvm create web nginx-latest -p 8080:80 --restart unless-stopped
./fcvm start web                    # from now on it survives crashes and reboots
./fcvm service status               # units, console URL, API token
```

## What gets installed

Two systemd units:

- **`fcvm-net.service`** (root, oneshot) brings up the bridges, taps and
  firewall rules at boot, so `fcvm net-up` is no longer needed after a
  reboot. It runs a root-owned copy of `lib/net.sh` with the network
  settings written to `/etc/fcvm/net.env` at install time. It never runs
  files from your (user-writable) fcvm tree as root.
- **`fcvm.service`** runs `fcvm serve --service` as you, after the network
  and `fcvm-jaild` (if installed). That's the web console, its
  [HTTP API](api.md), and the supervisor.

## Restart policies

Set a policy at `create`, change it with `fcvm update`, or pick it in the
web console:

```sh
./fcvm create web nginx-latest --restart unless-stopped
./fcvm update web --restart on-failure
```

| policy | the supervisor restarts the VM when... |
|---|---|
| `no` (default) | never |
| `on-failure` | an app exits non-zero, or Firecracker dies (killed, crashed) instead of the guest shutting down. Also after a host crash or reboot |
| `unless-stopped` | it exits for any reason, unless you stopped it (`fcvm stop`, or Stop in the console) |
| `always` | as `unless-stopped`, and it's also started at every boot even if you had stopped it |

Throwaway VMs (`fcvm run`) can't have a policy, since they're deleted when
they stop.

## How the supervisor behaves

- **Backoff.** Restarts back off from 1 s, doubling up to 60 s. The count
  resets once a VM has stayed up for a minute. The web console shows
  "restarting in N s", and the last error if a start failed.
- **Your stop wins.** `fcvm stop` (or Stop in the console) leaves a
  marker, and the supervisor leaves the VM alone. `fcvm start` clears it,
  and the policy applies again.
- **Exit records.** Each exit is recorded in `vms/VM/last-exit` and shown
  by `fcvm inspect`: the app's exit code, Firecracker's status, and whether
  the host went down under it.
- **One change at a time.** Your commands and the supervisor take a per-VM
  lock, so they never change the same VM at once.

## Shutdown and boot

- **Clean shutdown.** When the host shuts down, the service first stops
  every running VM cleanly, in parallel, and marks it to resume.
- **At boot**, VMs with a policy other than `no` come back. That includes
  VMs that were running when the host crashed. A crashed VM's leftover
  state is recognized, because each start records the boot id and process
  start time, and it's cleaned up before the restart.
- **Restarting the service** (`sudo systemctl restart fcvm`) leaves running
  VMs alone.

## Status, logs and the console URL

```sh
./fcvm service status          # fcvm-net, fcvm-jaild and fcvm; the console URL and API token
journalctl -u fcvm -f          # the supervisor's decisions ("web: exited ...; restarting in 2 s")
```

The service keeps its token across restarts (`vms/.serve-token`), so a
bookmarked console URL keeps working. The URL and token are also in
`vms/.serve.json`, for scripts ([API](api.md#connecting)).

## Without the service

- **A plain `fcvm serve`** runs the same console, API and supervisor for as
  long as it runs, with a new token each time.
- **With neither running**, restart policies simply aren't applied.
  Everything else works the same.

## Updating and removing

```sh
./fcvm service install      # after updating fcvm or changing NET_* settings: refreshes the copies and units
./fcvm service remove       # removes both units; running VMs keep running
```

After `service remove`, you're back to running `./fcvm net-up` after each
reboot.
