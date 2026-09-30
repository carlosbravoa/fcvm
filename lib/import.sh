#!/usr/bin/env bash
# fcvm import REF [NAME]: registry or local image -> images/NAME.ext4 (+ NAME.json)
#   REF: nginx:latest, ghcr.io/org/app:tag, docker-archive:img.tar[:TAG],
#        oci:DIR[:TAG], oci-archive:img.tar[:TAG], or a path to a .tar / OCI dir
. "$(dirname "$0")/common.sh"
need python3 mkfs.ext4

ref=${1:?usage: fcvm import REF [NAME]}
name=${2:-$(python3 "$FCVM_ROOT/lib/oci_import.py" --suggest-name "$ref")}
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
rm -f "$new"
mkfs.ext4 -q -F -L rootfs -N "$inodes" -d "$tar" "$new" "${size_mb}M"
chmod a-w "$new"   # shared read-only by every VM created from it
jq --arg type app --arg size "${size_mb}M" '. + {type: $type, disk_size: $size}' "$meta" > "$meta.tmp"
mv -f "$new" "$img"
mv "$meta.tmp" "$IMAGES_DIR/$name.json"
log "image ready: $img (${size_mb} MiB). Try: ./fcvm run $name"
