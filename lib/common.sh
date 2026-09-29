# Shared settings and helpers. Sourced by ./fcvm and every lib/*.sh script.
set -euo pipefail

FCVM_ROOT=${FCVM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
[ -f "$FCVM_ROOT/fcvm.conf" ] && . "$FCVM_ROOT/fcvm.conf"

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
: "${NET_ISOLATE:=0}"                          # 1: VMs on $NET_BRIDGE can't reach each other
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

BIN_DIR=$FCVM_ROOT/bin
BUILD_DIR=$FCVM_ROOT/build
CACHE_DIR=$FCVM_ROOT/cache
KERNELS_DIR=$FCVM_ROOT/kernels
IMAGES_DIR=$FCVM_ROOT/images
VMS_DIR=$FCVM_ROOT/vms
VOLUMES_DIR=$FCVM_ROOT/volumes
FIRECRACKER=$BIN_DIR/firecracker

log()  { printf '\e[1;34m==>\e[0m %s\n' "$*" >&2; }
warn() { printf '\e[1;33mwarning:\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[1;31merror:\e[0m %s\n' "$*" >&2; exit 1; }
need() {
    local c
    for c; do command -v "$c" >/dev/null || die "missing command '$c' (run: ./fcvm host-setup)"; done
}

# Default kernel: the newest one built, via the kernels/vmlinux symlink.
default_kernel() {
    [ -e "$KERNELS_DIR/vmlinux" ] || die "no kernel built yet (run: ./fcvm kernel)"
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
