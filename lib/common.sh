# shellcheck shell=bash
# Shared settings and helpers. Sourced by fcvm and every lib/*.sh script.
set -euo pipefail

# FCVM_ROOT is the code (a git checkout, or an installed release); FCVM_HOME is
# the state: images, VMs, volumes, snapshots, kernels, Firecracker. A checkout
# that already holds state keeps using it (as before 0.5); anything else uses
# ~/.local/share/fcvm. Settings come from fcvm.conf next to the state in a
# checkout, else from ~/.config/fcvm/fcvm.conf (FCVM_CONF overrides).
FCVM_ROOT=${FCVM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
if [ -z "${FCVM_HOME:-}" ]; then
    if [ -d "$FCVM_ROOT/vms" ] || [ -d "$FCVM_ROOT/images" ] || [ -d "$FCVM_ROOT/kernels" ]; then
        FCVM_HOME=$FCVM_ROOT
    else
        FCVM_HOME=${XDG_DATA_HOME:-$HOME/.local/share}/fcvm
    fi
fi
export FCVM_ROOT FCVM_HOME
if [ "$FCVM_HOME" = "$FCVM_ROOT" ]; then
    : "${FCVM_CONF:=$FCVM_ROOT/fcvm.conf}"
else
    : "${FCVM_CONF:=${XDG_CONFIG_HOME:-$HOME/.config}/fcvm/fcvm.conf}"
fi
[ -f "$FCVM_CONF" ] && . "$FCVM_CONF"

# --- Tunables (override in fcvm.conf or the environment) ---
: "${ARCH:=$(uname -m)}"                       # x86_64 (aarch64 untested)
: "${FC_VERSION:=latest}"                      # Firecracker release tag, or "latest"
: "${KERNEL_CHANNEL:=stable}"                  # stable | mainline | longterm | explicit version (e.g. 6.12.40)
: "${UBUNTU_SUITE:=resolute}"                  # Ubuntu 26.04
: "${UBUNTU_MIRROR:=http://archive.ubuntu.com/ubuntu}"
: "${BASE_PACKAGES:=systemd-sysv,udev,dbus,iproute2,iputils-ping,netbase,ca-certificates,openssh-server,curl,less,nano,sudo,kmod}"
: "${BASE_SIZE:=4G}"
: "${NET_BRIDGE:=fcbr0}"                        # full network: NAT to everything
: "${NET_PREFIX:=172.30.0}"                    # /24; host is .1, VMs get .10 + tap index
: "${NET_TAPS:=64}"                            # taps per bridge
: "${NET_ISOLATE:=1}"                          # 0: VMs on $NET_BRIDGE may reach each other
: "${NET_HOST_ACCESS:=0}"                      # 1: VMs may reach services on the host
: "${NET_R_BRIDGE:=fcbr1}"                     # restricted network (--allow): proxy only
: "${NET_R_PREFIX:=172.30.1}"
: "${EGRESS_PORT:=3128}"                       # egress proxy, on $NET_R_PREFIX.1
: "${NET_DNS:=auto}"                           # auto: the host's upstream resolvers
: "${VM_VCPUS:=2}"
: "${VM_MEM_MIB:=1024}"
: "${VM_KERNEL_ARGS:=}"                        # extra kernel args, e.g. "loglevel=7" to debug boot
: "${VOLUME_SIZE:=10G}"                         # default size of new volumes (sparse)
: "${VM_DISK:=8G}"                              # per-VM writable layer (sparse)
: "${IMPORT_FREE_MB:=64}"                      # free space added to imported disks (for --copy VMs)

BIN_DIR=$FCVM_HOME/bin
BUILD_DIR=$FCVM_HOME/build
CACHE_DIR=$FCVM_HOME/cache
KERNELS_DIR=$FCVM_HOME/kernels
IMAGES_DIR=$FCVM_HOME/images
VMS_DIR=$FCVM_HOME/vms
VOLUMES_DIR=$FCVM_HOME/volumes
SNAPSHOTS_DIR=$FCVM_HOME/snapshots
SSH_DIR=$FCVM_HOME/ssh
FIRECRACKER=$BIN_DIR/firecracker
[ -d "$FCVM_HOME" ] || mkdir -p "$FCVM_HOME"

# The path to run this fcvm by, for things that outlive a release (systemd
# units, MCP registrations): an installed release's stable `current` link
# rather than one version's directory, so `fcvm upgrade` carries them along.
fcvm_entry() {
    local cur; cur=$(dirname "$FCVM_ROOT")/current
    if [ -L "$cur" ] && [ "$(readlink -f "$cur")" = "$(readlink -f "$FCVM_ROOT")" ]; then echo "$cur/fcvm"
    else echo "$FCVM_ROOT/fcvm"; fi
}

# Is build/initramfs.cpio built from this code's init/fc-init.c? (After an
# upgrade it isn't, and VMs must not boot an init older than the host side.)
initramfs_current() {
    [ -f "$BUILD_DIR/initramfs.cpio" ] &&
        [ "$(cat "$BUILD_DIR/initramfs.src" 2>/dev/null)" = "$(sha256sum < "$FCVM_ROOT/init/fc-init.c" | cut -d' ' -f1)" ]
}

# The release (VERSION), plus the commit when this is a git checkout that isn't
# exactly at that release's tag: 0.5.0, 0.5.0+12.gabc1234, 0.5.0+12.gabc1234.dirty
fcvm_version() {
    local v d
    v=$(cat "$FCVM_ROOT/VERSION" 2>/dev/null || echo unknown)
    if [ -e "$FCVM_ROOT/.git" ] && command -v git >/dev/null; then
        if d=$(git -C "$FCVM_ROOT" describe --tags --match "v$v" --dirty 2>/dev/null); then
            d=${d#"v$v"}; d=${d#-}
        else
            d=$(git -C "$FCVM_ROOT" describe --always --dirty 2>/dev/null) && d="g$d"
        fi
        [ -z "$d" ] || v="$v+${d//-/.}"
    fi
    echo "$v"
}

log()  { printf '\e[1;34m==>\e[0m %s\n' "$*" >&2; }
warn() { printf '\e[1;33mwarning:\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[1;31merror:\e[0m %s\n' "$*" >&2; exit 1; }
need() {
    local c
    for c; do command -v "$c" >/dev/null || die "missing command '$c' (run: fcvm host-setup)"; done
}

# Default kernel: the newest one built, via the kernels/vmlinux symlink.
default_kernel() {
    [ -e "$KERNELS_DIR/vmlinux" ] || die "no kernel built yet (run: fcvm kernel)"
    readlink -f "$KERNELS_DIR/vmlinux"
}

# Image $1 and its parents, topmost first (committed images are layers on a parent).
image_chain() {
    local img=$1
    while [ -n "$img" ]; do
        echo "$img"
        img=$(jq -r '.parent // empty' "$IMAGES_DIR/$img.json" 2>/dev/null)
    done
}

# What depends on image $1: VMs booting from it (anywhere in their image's
# chain, overlay mode) and images committed on top of it ("image:NAME").
image_users() {
    local j
    for j in "$VMS_DIR"/*/vm.json; do
        [ -f "$j" ] && [ ! -f "${j%/vm.json}/disk.ext4" ] || continue
        if image_chain "$(jq -r .image "$j")" | grep -qx -- "$1"; then basename "${j%/vm.json}"; fi
    done
    for j in "$IMAGES_DIR"/*.json; do
        [ -f "$j" ] || continue
        if [ "$(jq -r '.parent // empty' "$j")" = "$1" ]; then echo "image:$(basename "$j" .json)"; fi
    done
    for j in "$SNAPSHOTS_DIR"/*/vm.json; do   # snapshots boot from their image too
        [ -f "$j" ] && [ ! -f "${j%/vm.json}/disk.ext4" ] || continue
        if image_chain "$(jq -r .image "$j")" | grep -qx -- "$1"; then echo "snapshot:$(basename "${j%/vm.json}")"; fi
    done
}

# DNS servers for VMs with full network ("a b", at most two). auto: the host's
# real upstream resolvers (systemd-resolved's list, else /etc/resolv.conf),
# skipping loopback stubs a VM can't reach; 1.1.1.1 as a last resort.
vm_dns() {
    if [ "$NET_DNS" != auto ]; then echo "$NET_DNS"; return; fi
    local f ns
    for f in /run/systemd/resolve/resolv.conf /etc/resolv.conf; do
        [ -r "$f" ] || continue
        ns=$(awk '$1 == "nameserver" && $2 !~ /^127\./ && $2 !~ /:/ {print $2}' "$f" | head -2 | tr '\n' ' ')
        [ -n "$ns" ] && { echo "$ns"; return; }
    done
    echo 1.1.1.1
}

# One-time migration: image/VM types were named "container" and "systemd"
# before they became "app" and "system". Cheap no-op once nothing old is left.
migrate_types() {
    local f
    for f in $(grep -l -E '"type": *"(container|systemd)"' "$IMAGES_DIR"/*.json "$VMS_DIR"/*/vm.json 2>/dev/null); do
        jq '.type |= (if . == "container" then "app" elif . == "systemd" then "system" else . end)' "$f" > "$f.tmp" &&
            mv "$f.tmp" "$f"
    done
}
migrate_types
