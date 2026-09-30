#!/usr/bin/env bash
# Install everything the build and runtime need. Run once; uses sudo.
#   --check   report what's missing and exit 1 if anything is (no sudo)
. "$(dirname "$0")/common.sh"

PKGS=(
    # kernel build
    build-essential flex bison bc libelf-dev libssl-dev cpio
    # rootfs build (rootless via user namespaces)
    mmdebstrap uidmap e2fsprogs
    # runtime / tooling (acl: jailed VMs, fcvm jail-setup)
    python3 curl jq iproute2 nftables openssh-client acl
)
missing_pkgs() {
    local p
    for p in "${PKGS[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || echo "$p"
    done
}
if [ "${1:-}" = --check ]; then
    todo=()
    command -v dpkg-query >/dev/null || { echo "not a Debian/Ubuntu host: install the equivalents of: ${PKGS[*]}"; exit 1; }
    mapfile -t miss < <(missing_pkgs)
    [ ${#miss[@]} -eq 0 ] || todo+=("packages: ${miss[*]}")
    { [ -r /dev/kvm ] && [ -w /dev/kvm ]; } || todo+=("access to /dev/kvm (kvm group)")
    grep -q "^$USER:" /etc/subuid 2>/dev/null || todo+=("a subuid/subgid range for $USER")
    [ ${#todo[@]} -eq 0 ] && exit 0
    printf '%s\n' "${todo[@]}"; exit 1
fi

command -v apt-get >/dev/null || die "host-setup uses apt; on this system install the equivalents of: ${PKGS[*]}"
log "installing: ${PKGS[*]}"
sudo apt-get update
sudo apt-get install -y --no-install-recommends "${PKGS[@]}"

if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    log "/dev/kvm is accessible"
else
    log "granting $USER access to /dev/kvm (kvm group; log out/in to take effect)"
    sudo usermod -aG kvm "$USER"
fi

grep -q "^$USER:" /etc/subuid || { log "adding subuid/subgid range for $USER"; sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"; }
log "done. Next: ./fcvm setup (or by hand: ./fcvm net-up && ./fcvm firecracker && ./fcvm kernel)"
