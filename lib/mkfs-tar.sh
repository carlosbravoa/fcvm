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

need newuidmap newgidmap
grep -q "^$(id -un):" /etc/subuid && grep -q "^$(id -un):" /etc/subgid ||
    die "no subuid/subgid range for $(id -un) (run: fcvm host-setup)"
mkdir -p "$BUILD_DIR"
dir=$(mktemp -d "$BUILD_DIR/unpack.XXXXXX")
# Inside: root is you, and the image's uids map onto your subuids. Unpack,
# build, and clean up there (outside, you couldn't delete subuid-owned files).
python3 "$FCVM_ROOT/lib/userns.py" bash -c '
    set -uo pipefail
    tar="$1" dir="$2"; shift 2
    tar --numeric-owner --same-owner --same-permissions --xattrs --xattrs-include="*" \
        -xf "$tar" -C "$dir" 2> "$dir.err"
    rc=$?
    if [ $rc != 0 ]; then
        nodes=$(grep -c "Cannot mknod" "$dir.err" || true)
        others=$(grep -v "Cannot mknod\|Exiting with failure status" "$dir.err" || true)
        if [ -n "$others" ]; then echo "$others" >&2; rm -rf "$dir" "$dir.err"; exit 1; fi
        [ "$nodes" = 0 ] || echo "==> skipped $nodes device node(s) (VMs get /dev from devtmpfs)" >&2
    fi
    mkfs.ext4 "$@" -d "$dir" 2>&1 | grep -v "^Copying files into the device" >&2
    rc=${PIPESTATUS[0]}
    rm -rf "$dir" "$dir.err"
    exit $rc
' mkfs-tar "$tar" "$dir" "${opts[@]}" "$@"
