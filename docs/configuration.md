# Configuration

fcvm works with no configuration. This page lists what you can change and
where.

- [How settings work](#how-settings-work)
- [VM defaults](#vm-defaults)
- [Images](#images)
- [Network](#network)
- [Firecracker](#firecracker)
- [The guest kernel](#the-guest-kernel)
- [Egress presets](#egress-presets)
- [Jailer, service and MCP](#jailer-service-and-mcp)
- [Per-command variables](#per-command-variables)

## How settings work

Settings are shell variables with defaults at the top of `lib/common.sh`.
Override them either way:

- **Permanently**, in `fcvm.conf` in the checkout (not tracked by git):

  ```sh
  # fcvm.conf
  VM_MEM_MIB=2048
  KERNEL_CHANNEL=longterm
  NET_HOST_ACCESS=1
  ```

- **For one command**, in the environment:
  `KERNEL_CHANNEL=longterm ./fcvm kernel`.

**Where a setting takes effect.**
- Most settings are read each time a command runs.
- Network settings take effect when the network is set up: at `net-up`,
  or, with the service installed, at the next `service install`, which
  copies them into `/etc/fcvm/net.env` for boot.
- Jailer settings are copied into the helper's config at `jail-setup`.

## VM defaults

| setting | default | |
|---|---|---|
| `VM_VCPUS` | `2` | vCPUs when `--vcpus` isn't given |
| `VM_MEM_MIB` | `1024` | memory when `--mem` isn't given |
| `VM_DISK` | `8G` | size of each VM's writable layer (sparse) |
| `VOLUME_SIZE` | `10G` | size of new named volumes (sparse) |
| `VM_KERNEL_ARGS` | | extra guest kernel arguments, for example `loglevel=7` to debug boot |
| `JAIL` | `0` | `1`: new VMs are jailed unless `--no-jail` |

## Images

| setting | default | |
|---|---|---|
| `UBUNTU_SUITE` | `resolute` | the Ubuntu release `fcvm base` builds (26.04) |
| `UBUNTU_MIRROR` | `http://archive.ubuntu.com/ubuntu` | where it downloads from; use a local mirror to speed it up |
| `BASE_PACKAGES` | systemd-sysv, udev, dbus, iproute2, iputils-ping, netbase, ca-certificates, openssh-server, curl, less, nano, sudo, kmod | packages in the base image; append yours |
| `BASE_SIZE` | `4G` | the base image's disk size |
| `IMPORT_FREE_MB` | `64` | free space added to imported images (used only by `--copy` VMs) |
| `REGISTRY_USER`, `REGISTRY_PASSWORD` | | credentials for `import` from a private registry |

For example, to add packages to the base image:

```sh
echo 'BASE_PACKAGES="$BASE_PACKAGES,git,build-essential,python3"' >> fcvm.conf
./fcvm base ubuntu-dev
```

## Network

| setting | default | |
|---|---|---|
| `NET_BRIDGE`, `NET_PREFIX` | `fcbr0`, `172.30.0` | the full-network bridge and its /24 (host `.1`, VMs from `.10`) |
| `NET_R_BRIDGE`, `NET_R_PREFIX` | `fcbr1`, `172.30.1` | the restricted bridge and its /24 |
| `NET_TAPS` | `64` | taps per bridge: how many VMs of each kind can run at once |
| `NET_ISOLATE` | `1` | `0`: full-network VMs may reach each other (also re-run `jail-setup`) |
| `NET_HOST_ACCESS` | `0` | `1`: full-network VMs may reach services on the host |
| `EGRESS_PORT` | `3128` | the egress proxy's port on the restricted bridge |
| `NET_DNS` | `auto` | DNS servers for full-network VMs ("a b"). `auto`: the host's upstream resolvers |

Change subnets or bridge names only with no VMs running, then run
`net-down` and `net-up` (and `service install` if installed).

## Firecracker

| setting | default | |
|---|---|---|
| `FC_VERSION` | `latest` | the release `fcvm firecracker` downloads, e.g. `v1.17.0` |
| `ARCH` | `uname -m` | only x86_64 is supported today |

Snapshots are tied to the Firecracker version that took them. After
changing it, re-run `jail-setup` if installed, since the helper runs its
own root-owned copy.

## The guest kernel

| setting | default | |
|---|---|---|
| `KERNEL_CHANNEL` | `stable` | `stable`, `longterm`, `mainline`, or an exact version such as `6.18.54` |

**Building.**

```sh
./fcvm kernel                    # newest of KERNEL_CHANNEL
./fcvm kernel 6.18.54            # an exact version
FORCE=1 ./fcvm kernel            # rebuild a version already built (e.g. after editing the fragment)
```

**Customizing.** Every VM boots `kernels/vmlinux`, a symlink to the newest
build. The kernel is monolithic, with no loadable modules: features come
from the fragment `kernel/microvm-x86_64.config`, applied on top of
`allnoconfig`. To add a feature:
1. Add its options to the fragment.
2. Rebuild with `FORCE=1 ./fcvm kernel`.
3. Restart the VMs that need it.

The build reports any option that didn't make it into the final `.config`,
because of a renamed symbol or an unmet dependency.

**Rolling back.** Earlier builds stay in `kernels/`:

```sh
ls kernels/                                # vmlinux-X.Y.Z builds, with their config-X.Y.Z
ln -sfn vmlinux-OLD kernels/vmlinux        # point at an earlier build, then restart VMs
```

Firecracker officially validates guest kernels 5.10, 6.1 and 6.18.
`KERNEL_CHANNEL=longterm` stays close to that. Notes on what the fragment
needs, and why, are in [How it works](internals.md#kernel-configuration-notes).

## Egress presets

Presets for `--allow @NAME` are in `lib/egress-presets.conf`, one per line:
the name, then host patterns:

```
@pypi      pypi.org files.pythonhosted.org
@corp      artifactory.corp.example:8443 *.pkg.corp.example
```

- **Patterns.** They follow the allowlist rules: `*.x` means subdomains
  only, and without a port, 80 and 443 are allowed.
- **When changes apply.** Presets are expanded when a VM starts, so edits
  apply to VMs started afterwards. `fcvm egress VM --allow` changes a
  running VM.
- **Updates.** The file is tracked by git, so keep your additions in mind
  when you pull.

## Jailer, service and MCP

| setting | read by | |
|---|---|---|
| `JAIL_UID_BASE` (`900000`) | `jail-setup` | first uid for jailed VMs (256 are used) |
| `--port` | `service install` | the console's port (default 8686) |
| `FCVM_MCP_NETWORK` | `mcp` | pins sandboxes' network policy, e.g. `@pypi,@github` |
| `FCVM_MCP_JAIL` (`1`) | `mcp` | `0`: don't jail sandboxes even when the helper is installed |

## Per-command variables

| variable | used by | |
|---|---|---|
| `FORCE=1` | `kernel` | rebuild a version that's already built |
| `JAIL=1` | `create`, `run` | jail by default |
| `VM_KERNEL_ARGS` | `start` | extra kernel arguments for this boot |
