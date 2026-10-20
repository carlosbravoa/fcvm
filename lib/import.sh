#!/usr/bin/env bash
# fcvm import REF [NAME] [--tenant T]: registry or local image -> images/NAME.ext4 (+ NAME.json)
#   REF: nginx:latest, ghcr.io/org/app:tag, docker-archive:img.tar[:TAG],
#        oci:DIR[:TAG], oci-archive:img.tar[:TAG], or a path to a .tar / OCI dir
#   --tenant: the image is that tenant's (only its VMs may use it); else public
. "$(dirname "$0")/common.sh"
need python3 mkfs.ext4

usage="usage: fcvm import REF [NAME] [--tenant NAME]"
ref="" name="" tenant=""
while [ $# -gt 0 ]; do
    case $1 in
        --tenant) tenant_ok "${2:?$usage}"; tenant=$2; shift 2 ;;
        -*)       die_code invalid "unknown option $1 ($usage)" ;;
        *)        if [ -z "$ref" ]; then ref=$1; elif [ -z "$name" ]; then name=$1; else die_code invalid "$usage"; fi; shift ;;
    esac
done
[ -n "$ref" ] || die_code invalid "$usage"
# A local image (an archive or an OCI layout) is a host path: node mode's rules apply
case $ref in
    docker-archive:*|oci-archive:*|oci:*) p=${ref#*:}; [ -e "$p" ] || p=${p%:*}; host_path_ok "$p" "a local image" ;;
    *) [ ! -e "$ref" ] || host_path_ok "$ref" "a local image" ;;
esac
[ -n "$name" ] || name=$(python3 "$FCVM_ROOT/lib/oci_import.py" --suggest-name "$ref")
users=$(image_users "$name" | tr "\n" " ")
[ -z "$users" ] || die "image '$name' is the shared base of VMs: $users (remove them, or import under another name)"

mkdir -p "$IMAGES_DIR" "$BUILD_DIR"
tar=$BUILD_DIR/$name.rootfs.tar
# Built under temporary names and moved into place only when complete: a
# failed import leaves nothing behind, and a failed re-import keeps the old
# image.
meta=$BUILD_DIR/$name.meta.json
img=$IMAGES_DIR/$name.ext4
new=$IMAGES_DIR/.$name.ext4.new
trap 'rm -f "$tar" "$meta" "$meta.tmp" "$new"' EXIT
python3 "$FCVM_ROOT/lib/oci_import.py" "$ref" \
    --out "$tar" --meta "$meta" \
    --hostname "${name%%-*}" --cache "$CACHE_DIR/blobs" --arch "$ARCH"

# Size: content + 4K per entry for metadata/dirs, +10% slack, + IMPORT_FREE_MB free
# (used only by --copy VMs; overlay VMs write to their own disk).
read -r bytes entries < <(jq -r '"\(.content_bytes) \(.entries)"' "$meta")
size_mb=$(( (bytes + entries * 4096) * 11 / 10 / 1048576 + IMPORT_FREE_MB ))
inodes=$(( entries * 2 + 16384 ))
python3 "$FCVM_ROOT/lib/node.py" admit ${tenant:+--tenant "$tenant"} --disk "${size_mb}M"   # the tenant's disk quota, the host's capacity
rm -f "$new"
"$FCVM_ROOT/lib/mkfs-tar.sh" "$tar" -q -F -L rootfs -N "$inodes" -- "$new" "${size_mb}M"
chmod a-w "$new"   # shared read-only by every VM created from it
jq --arg type app --arg size "${size_mb}M" --arg t "$tenant" \
    '. + {type: $type, disk_size: $size} + (if $t == "" then {} else {tenant: $t} end)' "$meta" > "$meta.tmp"
mv -f "$new" "$img"
mv "$meta.tmp" "$IMAGES_DIR/$name.json"
log "image ready: $img (${size_mb} MiB${tenant:+, tenant $tenant}). Try: fcvm run $name"
