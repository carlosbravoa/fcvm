# Networking and egress

Every VM gets one of three network modes when it's created. By default,
VMs are isolated from each other and from your host's services.
[Security](security.md#network-isolation) has the firewall rules behind
this page and how to verify them.

- [Network modes](#network-modes)
- [Full network](#full-network)
- [No network](#no-network)
- [Restricted network: egress allowlists](#restricted-network-egress-allowlists)
- [Isolation defaults](#isolation-defaults)
- [Addresses and reaching VMs](#addresses-and-reaching-vms)
- [Published ports](#published-ports)
- [Changing subnets and limits](#changing-subnets-and-limits)

## Network modes

| mode | option | the VM can reach |
|---|---|---|
| full | (default) | the internet and your LAN, through NAT on `fcbr0`, as `172.30.0.(10+i)` |
| none | `--net none` | nothing: the VM has no network card |
| restricted | `--allow HOST,*.DOMAIN,HOST:PORT,@PRESET` | only an egress proxy that applies the VM's allowlist; on `fcbr1`, as `172.30.1.(10+i)` |

In every mode, `exec`, `shell`, `cp`, the console and host directories keep
working. They use vsock, not the network.

## Full network

The default. The VM gets an address on `fcbr0` and reaches the outside
world through NAT on your host's default route. DNS points at your host's
upstream resolvers (`NET_DNS=auto`), so no resolver has to be reachable on
the host itself. The guest configures `eth0` from a kernel argument, so no
DHCP client runs.

**Private destinations.** Full mode reaches private destinations too: your
LAN, and other bridges on the host such as LXD, Multipass or Docker. For
code you don't trust, use restricted mode or no network.

## No network

```sh
./fcvm create box alpine-latest --idle --net none
```

The VM has no network card at all. It takes no tap, and published ports are
refused.

## Restricted network: egress allowlists

```sh
./fcvm run python-3.13-slim --allow @pypi -- pip install requests       # works
./fcvm create box alpine-latest --idle --allow @alpine,github.com        # a restricted VM
./fcvm egress box                     # its allowlist and recent ALLOW/DENY decisions
./fcvm egress box --allow example.com # change it live, no restart
./fcvm egress box --deny github.com
./fcvm egress box -f                  # follow decisions as they happen
```

**How it works.**
- A restricted VM sits on `fcbr1`, which has no NAT and no forwarding. Its
  only reachable destination is an egress proxy on `172.30.1.1:3128`.
- The proxy checks each request's host name against the VM's allowlist.
  It allows HTTPS tunnels (`CONNECT`) and plain HTTP to allowed hosts, and
  refuses everything else.
- `http_proxy` and `https_proxy` are set in the VM for the app and every
  `exec` session, so pip, npm, apt, apk, git, curl and Go work unchanged.
- Names are resolved on the host, so split-DNS and VPN names work.

**Allowlist entries:**

| entry | allows |
|---|---|
| `example.com` | that host, ports 80 and 443 |
| `*.example.com` | any subdomain, but not `example.com` itself |
| `example.com:8443` | that host on that port |
| `@pypi`, `@npm`, `@github`, ... | a preset: a named set of hosts |

**Presets.** Presets live in `lib/egress-presets.conf`: `@pypi`, `@npm`,
`@github`, `@gitlab`, `@golang`, `@crates`, `@rubygems`, `@maven`,
`@ubuntu`, `@debian`, `@alpine` and `@anthropic`. Add your own mirrors or
internal hosts there ([Configuration](configuration.md#egress-presets)).

**When a request is refused.** A denied request gets a 403 from the proxy,
for example `urlopen error Tunnel connection failed: 403 Forbidden` from
Python, and shows up as `DENY` in `fcvm egress`. A denied plain-HTTP
request's 403 names the command that would allow it.

**Limits.**
- **Protocols.** Only HTTP(S) and proxy-aware traffic can be allowed.
  Anything else (SSH, raw TCP, UDP, ICMP, direct DNS) is refused, which is
  the point. Clone git repositories over HTTPS.
- **Content.** The proxy checks host names, not content. An allowed host
  that accepts uploads can still receive data.
- **Logs.** Every decision is logged in `vms/VM/egress.log`.

## Isolation defaults

`fcvm net-up` sets these up:

- **VMs can't reach each other**, neither across the bridge nor routed
  through the host. `NET_ISOLATE=0` lets VMs on `fcbr0` talk to each other.
  Restricted VMs are always isolated.
- **VMs can't reach services on your host.** The host lets in only replies
  and ping from VMs, on any of its addresses (`172.30.0.1`, your LAN
  address, others). `NET_HOST_ACCESS=1` opens host services to
  full-network VMs.
- **Anti-spoofing.** Each VM can only send from its own MAC and IP address.
- **No IPv6** for guests.

To change a setting, run `net-up` again with it set. Put it in `fcvm.conf`
to keep it. If the fcvm service is installed, re-run `service install`
instead, since it applies the network at boot.

```sh
echo 'NET_HOST_ACCESS=1' >> fcvm.conf && ./fcvm net-up      # or: ./fcvm service install
```

## Addresses and reaching VMs

- **Addresses.** A VM's address comes from the tap slot it takes when it
  starts: `172.30.0.(10+i)` on the full network, `172.30.1.(10+i)` on the
  restricted one. It can change between starts. `fcvm ls` shows the
  current one.
- **Capacity.** Each pool has 64 slots (`NET_TAPS`), so 64 full-network
  plus 64 restricted VMs can run at once. `--net none` VMs don't count.
- **From the host.** You can reach any VM directly at its address, on any
  port. From other machines, publish ports.
- **Hostname.** A system VM's hostname is the VM's name. An app VM's is
  the image's short name (`nginx`, `alpine`), as set at import. Forks get
  their own name.

## Published ports

`-p HOST:GUEST` listens on `127.0.0.1`. Use `-p 0.0.0.0:HOST:GUEST` to
publish on every interface. See [Running VMs](vms.md#published-ports).

## Changing subnets and limits

The bridge names, subnets, tap count and proxy port are settings
(`NET_BRIDGE`, `NET_PREFIX`, `NET_R_BRIDGE`, `NET_R_PREFIX`, `NET_TAPS`,
`EGRESS_PORT`). Change them in `fcvm.conf` when stopped, then `net-down`
and `net-up`. See [Configuration](configuration.md#network).
