# Security and isolation

This document describes what separates an fcvm guest from your host and from
other guests, how each layer works, how to configure it, and how to check
that it is in place. [Networking](networking.md) and
[Running VMs](vms.md#jailed-vms) have the short, practical version; this is
the reference.

- [Threat model](#threat-model)
- [Layers at a glance](#layers-at-a-glance)
- [Recommended setup for untrusted code](#recommended-setup-for-untrusted-code)
- [The virtual machine boundary](#the-virtual-machine-boundary)
- [Rootless and jailed VMMs](#rootless-and-jailed-vmms)
- [Jailed VMs in detail](#jailed-vms-in-detail)
- [Network isolation](#network-isolation)
- [Published ports](#published-ports)
- [Restricted egress](#restricted-egress)
- [Host directories, volumes and snapshots](#host-directories-volumes-and-snapshots)
- [Web console and MCP server](#web-console-and-mcp-server)
- [Configuration reference](#configuration-reference)
- [Verifying the setup](#verifying-the-setup)
- [Troubleshooting](#troubleshooting)
- [Limitations and open items](#limitations-and-open-items)

## Threat model

fcvm assumes the code inside a VM may be hostile: a build script from the
internet, a dependency with a malicious install hook, an agent following a
prompt injection. Such code gets root inside its VM (containers routinely run
as root, and system images give you a root shell), so **the guest is treated
as fully compromised**. The layers below exist to keep that compromise inside
the VM.

What fcvm tries to prevent:

| a compromised guest trying to... | stopped by |
|---|---|
| read or change your files | the VM boundary (KVM); in addition, when jailed, a VMM with its own uid and a chroot |
| read or change other VMs' disks and memory | the VM boundary; when jailed, per-VM uids |
| exhaust the host's CPU, memory or processes | when jailed, cgroup v2 limits |
| talk to other VMs | bridge port isolation plus a forward rule against routing through the host |
| use services on the host (sshd, databases, dev servers, Docker API...) | an input rule on the bridge |
| impersonate another VM or the host on the network | anti-spoofing: each port passes only its own MAC and IPv4 address |
| slip past the IPv4 rules over IPv6 | no guest IPv6 at all |
| be reached from your LAN through published ports | ports bind to `127.0.0.1` by default |
| send data anywhere it likes | restricted mode: an allowlisting egress proxy |

What fcvm doesn't claim to prevent: see [Limitations](#limitations-and-open-items).
In short, a guest in full network mode can still reach the internet and your
LAN, a Firecracker or KVM vulnerability is a vulnerability, and you (the
fcvm owner) are trusted.

## Layers at a glance

```
 guest (root, untrusted)
 ├─ KVM hardware virtualization, minimal guest kernel, a few virtio devices
 ├─ Firecracker VMM: seccomp filter per thread
 │    rootless: runs as you
 │    jailed:   runs as uid 900000+N, chroot, cgroups, own network namespace
 └─ network: tap/veth port on fcbr0/fcbr1
      nft bridge table: MAC + IPv4 pinned per port, IPv4/ARP only
      bridge port isolation: no guest-to-guest frames
      nft ip table: no hairpin through the host, no host services, NAT out
      restricted bridge: nothing but the egress proxy
```

## Recommended setup for untrusted code

```sh
fcvm net-up          # isolation, host services blocked, anti-spoofing, no IPv6 (sudo)
fcvm jail-setup      # the jailer helper (sudo, once; re-run after updating fcvm)

# a sandbox that can reach only package mirrors, jailed
fcvm create box python-3.13-slim --idle --jail --allow @pypi,@github
```

or `JAIL=1` in your environment to jail every new VM. Sandboxes created by
the MCP server are jailed automatically once `fcvm-jaild` is installed, and
you can pin their network with `FCVM_MCP_NETWORK` (see
[Web console and MCP server](#web-console-and-mcp-server)).

## The virtual machine boundary

Every fcvm guest is a Firecracker microVM: its own kernel, running under
KVM hardware virtualization. Nothing about a guest runs as a host process
except the Firecracker VMM itself. Its attack surface toward the host is
deliberately small:

- **Devices.** Firecracker emulates a handful of virtio devices (block, net,
  vsock, entropy), a serial port and a keyboard controller used for
  Ctrl-Alt-Del. There is no USB, GPU, sound, PCI passthrough or BIOS.
- **Guest kernel.** It's built from `allnoconfig` plus a short fragment
  (`kernel/microvm-x86_64.config`). It has no loadable modules and only the
  drivers these devices need, and it's rebuilt from kernel.org sources on
  your machine.
- **Seccomp.** Firecracker installs a seccomp filter on each of its threads,
  limiting the VMM to the system calls it needs.
- **Exec agent.** Host-to-guest control (`exec`, `shell`, `cp`, file
  operations, live mounts) runs over vsock to an agent inside the guest.
  The host only ever connects *to* the guest. The guest can't open a vsock
  connection to anything on the host except the 9P servers you explicitly
  exported to it (see [Host directories](#host-directories-volumes-and-snapshots)).
  Processes inside the guest can't reach the agent either: the guest kernel
  has no vsock loopback. For the rare VM that must have nothing inside that
  accepts commands, `fcvm create --no-agent` boots it without the agent,
  at the cost of exec, cp, snapshots and guest metrics
  ([Running VMs](vms.md#without-the-exec-agent)).

## Rootless and jailed VMMs

fcvm can run the VMM two ways:

| | rootless (default) | jailed (`--jail`, `JAIL=1`) |
|---|---|---|
| Firecracker runs as | you | its own uid/gid, 900000 + slot |
| privileges | yours | none; `NoNewPrivs`, seccomp |
| filesystem it can see | everything you can | a chroot with its own disks, `/dev/kvm`, `/dev/net/tun`, its sockets |
| resource limits | none | cgroup v2: CPU, memory, pids; open files |
| network devices it can see | all of the host's | its own namespace: `lo`, `tap0`, `veth0`, `br0` |
| needs | nothing beyond `host-setup` / `net-up` | the `fcvm-jaild` helper (`fcvm jail-setup`) |

**Rootless** is enough when the only question is "can guest code reach my
files?". KVM and Firecracker's seccomp filters answer that. But if a guest
ever broke out into the VMM (a Firecracker bug), it would be running as
you, with your files and your SSH keys.

**Jailed** adds a second wall behind the first. A VMM compromise lands in
an unprivileged uid, locked in a chroot with nothing but that VM's own
disks, with resource limits, and unable to see the host's network
interfaces. This is how Firecracker is run in production (AWS Lambda,
Fargate).

Everything works the same in both modes: `exec`, `shell`, `console`, `cp`,
`stop`, published ports, egress control, host directories (at boot and
live), snapshots and fork, the web console and the MCP server.

## Jailed VMs in detail

### The helper, `fcvm-jaild`

The jailer must start as root: it creates the chroot, device nodes and
cgroups, enters the network namespace, and then drops to the VM's uid.
Rather than asking for `sudo` on every start, `fcvm jail-setup` installs a
small root service. `fcvm` asks it to launch VMs.

- **Where it runs.** `fcvm-jaild.service` runs `/usr/local/lib/fcvm/jaild.py`
  with a delegated cgroup (`Delegate=yes`) and a private mount namespace
  (`PrivateMounts=yes`).
- **Root-owned copies.** `jail-setup` installs root-owned copies of
  `jaild.py`, `jailer` and `firecracker` into `/usr/local/lib/fcvm`. The
  service never runs files from your (user-writable) fcvm tree, so
  compromising your account doesn't give root through the helper. The flip
  side: **re-run `fcvm jail-setup` after updating fcvm or
  `fcvm firecracker`**. `fcvm start` warns when the installed Firecracker
  differs from `bin/`.
- **Who may talk to it.** It listens on `/run/fcvm/jaild.sock`, mode 0600,
  and checks the peer's uid (`SO_PEERCRED`) on every connection. Only the
  user who ran `jail-setup` (recorded in `/etc/fcvm/jaild.json`) is served.
- **What it can do.** Four operations: `launch`, `kill`, `status` and
  `snapshot_collect`. Nothing else.

### What a launch request is checked against

`fcvm start` sends a JSON request naming the VM, kernel, initramfs, drives,
boot arguments, vCPUs, memory and tap. The helper trusts none of it. Every
path must:

1. resolve (after following symlinks) inside the owner's fcvm tree, under
   `images/`, `vms/<this vm>/`, `volumes/`, `kernels/`, `build/` or
   `snapshots/`;
2. be a regular file owned by the owner.

Only the VM's own disk (`vms/<vm>/...`) and its read-write volumes may be
writable. Images, kernels and snapshots are always mounted read-only. The
tap name must match the pool pattern, and vCPU and memory values must be
numbers.

### What each jailed VM gets

- **Its own uid and gid**: 900000 + slot, with slots 0–255. Change the base
  with `JAIL_UID_BASE` when running `jail-setup`.
- **A chroot** at `/srv/jailer/firecracker/<vm>/root`. The jail id is the VM
  name, with other characters replaced and a hash suffix added when needed.
  It contains:
  - `firecracker` (copied by the jailer), plus `dev/kvm` and `dev/net/tun`;
  - `vmlinux` and `initramfs.cpio`, bind-mounted read-only;
  - `drive0.ext4`, `drive1.ext4`, ...: the image layers (read-only), the
    VM's writable layer and its volumes, bind-mounted;
  - `fc.json`: the VM config with chroot-relative paths;
  - `fc.sock`, `vsock.sock` and `firecracker.log`.

  The chroot lives under `/srv/jailer` rather than in your fcvm tree
  because the tree may be on a `nosuid,nodev` filesystem, where the
  jailer's device nodes wouldn't work. For the same reason, disks are
  bind-mounted, not hard-linked. The mounts live in the helper's private
  mount namespace, so they don't clutter the host's mount table.
- **cgroup v2 limits**, under the service's delegated cgroup:
  - `cpu.max`: the VM's vCPUs worth of CPU time (2 vCPUs = 200%);
  - `memory.max`: guest memory + 256 MiB for the VMM;
  - `pids.max`: 128;
  - open files: `no-file` = 4096.
- **Its own network namespace**, `fcvm-<vm>`, holding only `lo`, `tap0`
  (owned by the jail uid), `veth0` and a bridge `br0` joining the two.
  - The veth's other end sits on the host's bridge: `fcv<i>` for full
    network, `fcrv<i>` for restricted.
  - That host end gets the same bridge isolation and anti-spoofing rules
    as a tap. The VM keeps its usual address `<prefix>.(10+i)`.
  - IPv6 is disabled inside the namespace and on the host end.
- **Access by ACL, not ownership.**
  - The jail uid gets `rw` ACLs on the VM's writable files, and `r` on
    snapshot files while it restores from them.
  - You keep ownership, so `commit`, `cp`, exit codes and every other
    command keep working as before.
  - A default ACL on the chroot lets both you and the VM use the sockets
    created in it.
  - The helper also repairs the ACL mask, which the jailer's `chmod 0700`
    and Firecracker's 0755 sockets would otherwise clear.
- **Convenience links.** `vms/<vm>/fc.sock`, `vsock.sock` and
  `firecracker.log` are symlinks into the chroot, so every fcvm command
  finds them where it always does.

### Snapshots and fork of jailed VMs

- **Snapshot.** `fcvm snapshot VM NAME` pauses the VM and asks Firecracker
  to write its state to `/snap.vmstate` and `/snap.mem` *inside the chroot*.
  The helper (`snapshot_collect`) then moves them into `snapshots/NAME/`,
  owned by you, mode 0600, because they contain guest memory. fcvm copies
  the writable disk and resumes the VM. `meta.json` records `"jail": true`.
- **Fork.** `fcvm fork NAME` of a jailed snapshot builds a fresh jail with
  the new VM's own disk copy, then loads the snapshot through the API.
  - Paths inside a jail are the same for every VM (`/drive0.ext4`,
    `/vsock.sock`, `tap0`), so the snapshot loads without any path or
    network overrides.
  - The fork's uid gets read access to the snapshot files for as long as
    it runs.
  - After loading, the exec agent gives the guest its new address, MAC
    and hostname.
- **Speed.** A jailed fork takes about a second, mostly spent building the
  jail and namespace. A rootless fork takes about 150 ms.
- **Mixing modes.** Snapshots of jailed VMs fork jailed, and snapshots of
  rootless VMs fork rootless.

### Lifecycle and cleanup

- **Stopping.**
  - `fcvm stop` works as for any VM: Ctrl-Alt-Del through the API, then the
    guest shuts down.
  - Killing a jailed VM (when it doesn't shut down in time, or when a start
    fails halfway) goes through the helper, because you can't signal a
    process of another uid.
  - Liveness checks use `/proc/<pid>`, because `kill -0` fails with EPERM
    across uids.
- **When Firecracker exits**, the helper:
  - unmounts the bind mounts;
  - removes the ACLs it granted;
  - deletes the network namespace (tap, bridge and veth go with it);
  - deletes the chroot.
- **Stopping or restarting the service** (including `jail-setup`) stops
  all jailed VMs.
- **On startup**, the helper sweeps anything a previous run left behind:
  chroots under `/srv/jailer/firecracker`, `fcvm-*` network namespaces, and
  ACL entries for jail uids on files in `vms/` and `snapshots/`.

### Why there is no PID namespace

The jailer can put Firecracker in a new PID namespace (`--new-pid-ns`).
fcvm doesn't, for two reasons:

- **It would cost fcvm the pid.** Firecracker would be pid 1 of a namespace
  the helper reaches only through the jailer's fork, and fcvm uses the real
  pid for liveness checks, stats and `stop`.
- **It would add little.** The uid already covers the protection a PID
  namespace gives: a process of another uid can't signal or ptrace your
  processes or other VMs. The chroot has no `/proc` to browse.

### Removing it

```sh
fcvm jail-setup --remove    # stops jailed VMs, removes the service and /usr/local/lib/fcvm
```

VMs created with `--jail` then refuse to start until the helper is
installed again. Their `vm.json` has `"jail": true`.

## Network isolation

### Topology

```
                      host
  ┌───────────────────────────────────────────────────────────┐
  │  fcbr0 172.30.0.1/24  (full: NAT out)                     │
  │   ├─ fctap0  ── VM 172.30.0.10        (rootless)          │
  │   ├─ fcv1    ── netns fcvm-web: veth0─br0─tap0 ── VM .11  │
  │   └─ ...        (jailed)                                  │
  │                                                           │
  │  fcbr1 172.30.1.1/24  (restricted: proxy only)            │
  │   ├─ fcrtap0 ── VM 172.30.1.10                            │
  │   └─ egress proxy listening on 172.30.1.1:3128            │
  └───────────────────────────────────────────────────────────┘
```

`fcvm net-up` (sudo, once per boot) creates both bridges, 64 persistent taps
on each (owned by you, so rootless VMs need no privileges), and the firewall
rules below. Re-running it is safe, and it's how you apply changed settings.
Jailed VMs don't use the pool taps directly: the helper creates a veth for
the same slot and index.

### What is enforced

**Guests can't reach each other.**
- Every tap and host-side veth is an *isolated* bridge port
  (`bridge link set dev X isolated on`), so the bridge never forwards a
  frame between two guest ports. Only guest ↔ host traffic passes.
- A guest could still try to route through the host: send to the host's
  MAC, addressed to another guest's IP. The rule
  `iifname "fcbr0" oifname "fcbr0" drop` stops that.
- Restricted VMs (`fcbr1`) are always isolated. For `fcbr0`,
  `NET_ISOLATE=0` lets full-network VMs talk to each other again.

**Guests can't reach services on the host.**
- From `fcbr0`, the host accepts only replies to connections it opened
  (published ports, the web console's proxying) and ICMP echo (ping).
  Everything else gets a reject.
- The rule matches on the incoming interface, so it covers every address
  the host has: `172.30.0.1`, your LAN address, other bridges, Tailscale.
  Services bound to `0.0.0.0`, such as sshd, a database or a dev server,
  are all unreachable from VMs.
- `NET_HOST_ACCESS=1` lifts this, for when a VM really needs a host
  service.
- Restricted VMs can reach only the egress proxy, always.

**Anti-spoofing.** An `nft` table in the bridge family
(`table bridge fcvm`) checks every frame entering from a guest port
(`fctap*`, `fcrtap*`, `fcv*`, `fcrv*`):

| check | action |
|---|---|
| source MAC ≠ that port's MAC | drop |
| not IPv4 or ARP (IPv6, VLAN tags, anything else) | drop |
| IPv4 source ≠ that port's address | drop |
| ARP sender MAC or IP ≠ that port's | drop |

Each slot's MAC and address are fixed:

| | full | restricted |
|---|---|---|
| address | `172.30.0.(10+i)` | `172.30.1.(10+i)` |
| MAC | `06:00:` + the address's four bytes in hex | `06:01:` + the address's four bytes in hex |

The rules therefore list every allowed port, MAC and address in two sets
(`guest_mac`, `guest_ip`), built once by `net-up`. As a result:
- A guest can't pretend to be another guest or the host, and can't poison
  ARP caches.
- The egress proxy's source-address identification of VMs can be trusted
  (see [Restricted egress](#restricted-egress)).

A fork keeps the snapshot's MAC and address until the exec agent
reconfigures it, a few milliseconds after the snapshot loads. Frames sent in
between are dropped, which is harmless.

**No IPv6.**
- IPv6 is disabled on both bridges, on every tap and host-side veth, and
  inside jail namespaces.
- The bridge table drops any IPv6 frame from a guest.
- Guests still configure link-local addresses on their own `eth0`. That
  traffic goes nowhere.
- Full IPv6 support (addresses, NAT66 or routing, equivalent rules) is on
  the [roadmap](roadmap.md#e2-network-isolation-and-policy-). Until then,
  no IPv6 at all is the safe choice: rules written for IPv4 can't be
  bypassed over v6.

**Outbound.**
- Full-network VMs reach the internet through NAT (masquerade) on the
  host's default route.
- Restricted VMs have no forwarding at all.

### The rules as installed

```sh
sudo nft list table ip fcvm        # NAT, forward and input rules
sudo nft list table bridge fcvm    # anti-spoofing sets and rules
bridge -d link show | grep -E 'fc(r)?(tap|v)[0-9]+.*' -A1 | grep -o 'isolated [a-z]*'
```

With `ufw` active, `net-up` adds only what's needed:
- `ufw route allow in on fcbr0`, so VMs can reach the internet;
- the proxy port on `fcbr1`;
- `ufw allow in on fcbr0`, only with `NET_HOST_ACCESS=1`. Otherwise
  `net-up` removes it.

## Published ports

- **Binding.** `-p HOST:GUEST` listens on `127.0.0.1:HOST`. Only programs
  on your machine can connect. To publish on every interface, or on a
  particular one, say so: `-p 0.0.0.0:8080:80` or `-p 192.168.1.5:8080:80`.
- **The forwarder.** Ports are relayed by `lib/portfwd.py`, a small rootless
  TCP relay that exits with its VM.
- **Source address.** The guest sees connections coming from the bridge
  address (`172.30.0.1`), not from the real client. These connections are
  host-initiated, so they pass the "no host services" rule as replies.
- **Scope.** TCP only.

## Restricted egress

`--allow HOSTS` (for example `--allow @pypi,github.com`) puts a VM on
`fcbr1`:
- **No route anywhere.** No NAT, no forwarding, no host services. Its
  only destination is the egress proxy on `172.30.1.1:3128`.
- **The proxy.** `lib/egress_proxy.py` is one per user, rootless. It
  allows `CONNECT` (HTTPS) and plain-HTTP requests to allowlisted host
  names, and logs every decision (`fcvm egress VM`).
- **Live changes.** `fcvm egress VM --allow ...` edits the allowlist of a
  running VM.

**Identifying VMs.**
- The proxy knows which VM a request comes from by its source address.
- Anti-spoofing is what makes that sound: without it, a restricted VM
  could take another VM's address and borrow its allowlist.

**Limits.**
- Only HTTP(S) and proxy-aware tools can be allowed. Raw TCP, UDP, ICMP
  and direct DNS are refused, and name resolution happens on the host.
- The proxy checks host names, not content. An allowlisted host that
  accepts uploads (a git forge, a pastebin) can still receive data.

## Host directories, volumes and snapshots

Every one of these gives a guest access to something outside it:

- **Host directories** (`-v /host/dir:/path[:ro]`, `fcvm mount`):
  - They're served over vsock by `lib/share9p.py`, running as **you**.
  - A guest can read (and, without `:ro`, write) everything under that
    directory, and nothing outside it. Symlinks are resolved on the host
    and confined to the directory.
  - Mount the narrowest directory you can, read-only where you can.
  - Jailing doesn't change this: the 9P server is yours.
- **Named volumes** (`-v NAME:/path`) are disks. A read-write volume can
  be attached to only one running VM at a time.
- **Snapshots** contain the VM's full memory: anything that was in RAM,
  secrets included. They're stored mode 0600 under `snapshots/`. Treat
  them like the VM itself.
- **Committed images** (`fcvm commit`) keep whatever was on disk. Identity
  files (machine-id, SSH host keys, `authorized_keys`) are scrubbed, but
  credentials you put there aren't.
- **SSH keys** aren't part of any image. Each system VM gets
  `authorized_keys` and its own host key at `create`, and `fcvm ssh`
  verifies that host key strictly. VMs created before fcvm 0.6.1 have no
  recorded host key: `fcvm ssh` warns and skips the check for them.

## Web console and MCP server

**Web console** (`fcvm serve`):
- It binds `127.0.0.1` only, and checks the Host and Origin headers.
- The URL it prints carries a random token, which it exchanges for an
  HttpOnly cookie. Every request needs that cookie.
- It can create, start, stop and exec into VMs as you, so anyone with the
  URL and token has your fcvm.
- The create form's "Run under the Firecracker jailer" box is checked by
  default when `fcvm-jaild` is installed.

**The HTTP API** is the web console's own, and scripts can use it with
`Authorization: Bearer <token>` ([docs/api.md](api.md)). A bearer token
works only under `/api/`, and it's the same token as the console's: whoever
has it has your fcvm. `vms/.serve.json` and `vms/.serve-token` hold it,
mode 0600.

**The fcvm service** (`fcvm service install`):
- **`fcvm-net.service`** runs as root at boot. It runs a root-owned copy
  of `net.sh` (`/usr/local/lib/fcvm/net.sh`) with root-owned settings
  (`/etc/fcvm/net.env`). Like `fcvm-jaild`, it never executes files from
  your user-writable fcvm tree as root, so that tree can't be used to gain
  root. Re-run `service install` after updating fcvm.
- **`fcvm.service`** runs `fcvm serve` as you, with no more privileges
  than you have. It's a system unit (with `User=`) rather than a user unit
  so it can start at boot without a login, after the network and the
  jailer helper.
- **`_shutdown`.** At host shutdown, `fcvm _shutdown` stops VMs cleanly.
  It acts only while the system is shutting down, so restarting the
  service doesn't stop anything.

**MCP server** (`fcvm mcp`):
- An agent gets tools to create and use sandboxes.
- **Jailing.** Sandboxes are created with `--jail` whenever
  `/run/fcvm/jaild.sock` exists. `FCVM_MCP_JAIL=0` disables that.
- **Network.** `FCVM_MCP_NETWORK` pins the network policy an agent may
  use, for example `@pypi,@github`. The agent can then only choose that
  policy or `none`.
- **Registration**, with both settings:

  ```sh
  claude mcp add fcvm -e FCVM_MCP_NETWORK=@pypi,@github -- /path/to/fcvm mcp
  ```

## Configuration reference

| variable | default | read by | effect |
|---|---|---|---|
| `JAIL` | `0` | `create`, `run` | `1`: new VMs are jailed (as `--jail`; `--no-jail` overrides) |
| `NET_ISOLATE` | `1` | `net-up`, `jail-setup` | `0`: VMs on `fcbr0` may reach each other |
| `NET_HOST_ACCESS` | `0` | `net-up` | `1`: VMs on `fcbr0` may reach host services |
| `NET_DNS` | `auto` | `start` | DNS servers for full-network VMs; `auto` = the host's upstream resolvers |
| `NET_TAPS` | `64` | `net-up` | taps (and anti-spoofing entries) per bridge |
| `NET_PREFIX`, `NET_R_PREFIX` | `172.30.0`, `172.30.1` | everything | the two /24s |
| `EGRESS_PORT` | `3128` | `net-up`, proxy | the egress proxy's port on `NET_R_PREFIX.1` |
| `JAIL_UID_BASE` | `900000` | `jail-setup` | first jail uid (256 are used) |
| `FCVM_MCP_JAIL` | `1` | `mcp` | `0`: MCP sandboxes aren't jailed |
| `FCVM_MCP_NETWORK` | unset | `mcp` | pins the sandboxes' network policy |

After changing a `net-up` or `jail-setup` variable, re-run the command with
it set (both use sudo), for example:

```sh
NET_HOST_ACCESS=1 fcvm net-up
NET_ISOLATE=0 fcvm net-up && NET_ISOLATE=0 fcvm jail-setup   # the helper sets veth isolation
```

## Verifying the setup

From the host:

```sh
systemctl is-active fcvm-jaild                       # the helper
fcvm ls                                            # jailed VMs show "jail"
ps -o user,pid,cmd -p "$(cat vms/box/pid)"           # 900000+N, /firecracker --id box ...
awk 'NR>2 {print $1}' /proc/"$(cat vms/box/pid)"/net/dev   # lo: veth0: br0: tap0:
ss -ltn | grep 8080                                  # published ports on 127.0.0.1
```

From inside a VM (Alpine's busybox tools shown):

```sh
fcvm exec box -- ping -c1 -W1 172.30.0.11          # another VM: fails
fcvm exec box -- nc -z -w2 172.30.0.1 22           # host sshd: fails
fcvm exec box -- ping -c1 -W1 172.30.0.1           # host ping: works
fcvm exec box -- wget -qO- http://example.com      # internet (full mode): works

# spoofing: take another address, then try to get out
fcvm exec box -- sh -c 'ip addr add 172.30.0.50/24 dev eth0;
    ping -c1 -W2 -I 172.30.0.50 1.1.1.1 || echo dropped; ip addr del 172.30.0.50/24 dev eth0'
```

## Troubleshooting

**A VM needs a service on the host** (a local registry, a database, a dev
server).
- Prefer running the service in a VM, or publishing it the other way
  round.
- Otherwise: `NET_HOST_ACCESS=1 fcvm net-up`. This opens every host
  service to every full-network VM.

**Two VMs need to talk.** `NET_ISOLATE=0 fcvm net-up`, and also
`NET_ISOLATE=0 fcvm jail-setup` if either is jailed.

**A published port isn't reachable from another machine.** Ports bind to
`127.0.0.1` by default. Publish with `-p 0.0.0.0:HOST:GUEST`.

**"'VM' runs jailed, but fcvm-jaild isn't running".** Run
`fcvm jail-setup`, or check `journalctl -u fcvm-jaild`.

**"fcvm-jaild has a different Firecracker than bin/".** You updated
Firecracker or fcvm since installing the helper. Re-run
`fcvm jail-setup`.

**The helper refuses a request** ("not an fcvm file this VM may use", "not
a regular file owned by the fcvm owner"). A path in the VM's config
resolves outside your fcvm tree, or to a file you don't own. Typical causes
are an image or volume file created by root, or a symlink pointing out of
the tree.

**Leftovers after a crash** (`/srv/jailer/firecracker/*`, `ip netns list`
showing `fcvm-*`). Restart the helper; it sweeps them on startup:
`sudo systemctl restart fcvm-jaild`.

**A VM has no network after `net-up`.**
- Check the rules loaded: `sudo nft list table bridge fcvm`.
- Check the VM's MAC follows the slot formula
  (`jq '."network-interfaces"' vms/VM/fc.json`).
- VMs are always given the right MAC. A hand-edited config would be
  dropped by anti-spoofing.

## Limitations and open items

- **Full-network VMs reach your LAN.**
  - NAT forwards to any destination, including private ranges and other
    bridges on the host (LXD, Multipass, Docker networks). "No host
    services" covers the host's own addresses, not other machines.
  - Use restricted mode (`--allow`) or `--net none` for code that
    shouldn't see your network.
- **You are trusted.** Anyone who can run commands as the fcvm owner
  controls every VM, and can ask the helper to launch VMs. The helper
  limits that to files in your fcvm tree. It isn't a boundary against you.
- **Rootless VMMs run as you.** A Firecracker escape in rootless mode
  reaches your account. Jail anything you don't trust.
- **Host directories and the egress proxy run as you**, so a bug in
  `share9p.py` or `egress_proxy.py` is reachable from guests that use
  them.
- **Only jailed VMs have resource limits.** A rootless VM can take all of
  its configured memory, and its vCPUs can compete freely.
- **Disk and network rate limits** (Firecracker's rate limiters) aren't
  set yet. That's [roadmap item E6](roadmap.md#e6-resource-governance).
- **Side channels** between VMs sharing a CPU are the host kernel's and
  hardware's business. Keep your host kernel and microcode up to date.
- **IPv6** for guests is off, not supported.
