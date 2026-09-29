#!/usr/bin/env bash
# Download a Firecracker release (binary + jailer) into bin/, checksum-verified.
. "$(dirname "$0")/common.sh"
need curl jq tar sha256sum

tag=${1:-$FC_VERSION}
if [ "$tag" = latest ]; then
    tag=$(curl -fsSL https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | jq -r .tag_name)
fi
if [ -x "$FIRECRACKER" ] && "$FIRECRACKER" --version 2>/dev/null | grep -q "Firecracker ${tag}\$"; then
    log "firecracker $tag already installed"; exit 0
fi

base=https://github.com/firecracker-microvm/firecracker/releases/download/$tag
tgz=firecracker-$tag-$ARCH.tgz
mkdir -p "$CACHE_DIR" "$BIN_DIR"
log "downloading $tgz"
curl -fL --progress-bar -o "$CACHE_DIR/$tgz" "$base/$tgz"
curl -fsSL -o "$CACHE_DIR/$tgz.sha256.txt" "$base/$tgz.sha256.txt"
(cd "$CACHE_DIR" && sha256sum -c "$tgz.sha256.txt") >/dev/null || die "checksum mismatch for $tgz"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
tar -xzf "$CACHE_DIR/$tgz" -C "$tmp"
rel=$tmp/release-$tag-$ARCH
install -m 0755 "$rel/firecracker-$tag-$ARCH" "$BIN_DIR/firecracker"
install -m 0755 "$rel/jailer-$tag-$ARCH" "$BIN_DIR/jailer"
log "installed $("$FIRECRACKER" --version | head -1) to $BIN_DIR"
