#!/usr/bin/env bash
# mkfs-tar.sh TAR MKFS-OPTIONS... -- DEVICE [SIZE]: an ext4 filesystem with
# TAR's contents, owners included, built without root.
#
# e2fsprogs 1.47.1+ reads the tarball directly (mkfs.ext4 -d TAR). Older ones
# (Ubuntu 24.04 has 1.47.0) take only a directory, so there the tarball is
# unpacked inside a user namespace mapped over the caller's subuid range,
# where tar can restore every owner, and mkfs.ext4 reads it from inside the
# same namespace. Device nodes can't be created there and are skipped (VMs get
# /dev from devtmpfs). FCVM_MKFS_UNPACK=1 forces the second way (for tests).
. "$(dirname "$0")/common.sh"

tar=${1:?usage: mkfs-tar.sh TAR MKFS-OPTIONS... -- DEVICE [SIZE]}; shift
opts=()
while [ $# -gt 0 ] && [ "$1" != -- ]; do opts+=("$1"); shift; done
[ "${1:-}" = -- ] || die "usage: mkfs-tar.sh TAR MKFS-OPTIONS... -- DEVICE [SIZE]"
shift

reads_tarballs() {
    local v; v=$(mke2fs -V 2>&1 | awk '/^mke2fs/ {print $2; exit}')
    [ "$(printf '%s\n' 1.47.1 "$v" | sort -V | head -n 1)" = 1.47.1 ]
}
if [ "${FCVM_MKFS_UNPACK:-0}" != 1 ] && reads_tarballs; then
    exec mkfs.ext4 "${opts[@]}" -d "$tar" "$@"
fi

mkdir -p "$BUILD_DIR"
TMPDIR=$BUILD_DIR exec python3 "$FCVM_ROOT/lib/tar2ext4.py" "$tar" "${opts[@]}" -- "$@"
