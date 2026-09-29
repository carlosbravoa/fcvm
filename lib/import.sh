#!/usr/bin/env bash
# fcvm import REF [NAME]: registry image -> images/NAME.ext4 (+ NAME.json)
. "$(dirname "$0")/common.sh"
need python3 mkfs.ext4

ref=${1:?usage: fcvm import REF [NAME]}
name=${2:-$(sed -E 's|^.*/||; s|[@:]|-|g; s|[^A-Za-z0-9_.-]|_|g' <<<"$ref")}
users=$(image_users "$name" | tr "\n" " ")
[ -z "$users" ] || die "image '$name' is the shared base of VMs: $users (remove them, or import under another name)"

mkdir -p "$IMAGES_DIR" "$BUILD_DIR"
tar=$BUILD_DIR/$name.rootfs.tar
trap 'rm -f "$tar"' EXIT
python3 "$FCVM_ROOT/lib/oci_import.py" "$ref" \
    --out "$tar" --meta "$IMAGES_DIR/$name.json" \
    --hostname "${name%%-*}" --cache "$CACHE_DIR/blobs" --arch "$ARCH"

# Size: content + 4K per entry for metadata/dirs, +10% slack, + IMPORT_FREE_MB free
# (used only by --copy VMs; overlay VMs write to their own disk).
read -r bytes entries < <(jq -r '"\(.content_bytes) \(.entries)"' "$IMAGES_DIR/$name.json")
size_mb=$(( (bytes + entries * 4096) * 11 / 10 / 1048576 + IMPORT_FREE_MB ))
inodes=$(( entries * 2 + 16384 ))
img=$IMAGES_DIR/$name.ext4
rm -f "$img"
mkfs.ext4 -q -F -L rootfs -N "$inodes" -d "$tar" "$img" "${size_mb}M"
chmod a-w "$img"   # shared read-only by every VM created from it
jq --arg type container --arg size "${size_mb}M" '. + {type: $type, disk_size: $size}' \
    "$IMAGES_DIR/$name.json" > "$IMAGES_DIR/$name.json.tmp" && mv "$IMAGES_DIR/$name.json.tmp" "$IMAGES_DIR/$name.json"
log "image ready: $img (${size_mb} MiB). Try: ./fcvm run $name"
