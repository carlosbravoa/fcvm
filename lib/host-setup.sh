#!/usr/bin/env bash
# Install everything the build and runtime need. Run once; uses sudo.
. "$(dirname "$0")/common.sh"

PKGS=(
    # kernel build
    build-essential flex bison bc libelf-dev libssl-dev cpio
    # rootfs build (rootless via user namespaces)
    mmdebstrap uidmap e2fsprogs
    # runtime / tooling (acl: jailed VMs, fcvm jail-setup)
    python3 curl jq iproute2 nftables openssh-client acl
)
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
log "done. Next: ./fcvm net-up && ./fcvm firecracker && ./fcvm kernel (see README, Quick start)"
