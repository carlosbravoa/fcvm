#!/usr/bin/env bash
# Fetch the newest kernel from kernel.org and build a trimmed Firecracker guest
# vmlinux: `make allnoconfig` + kernel/microvm-$ARCH.config, nothing else.
#
#   fcvm kernel            # newest $KERNEL_CHANNEL release (stable by default)
#   fcvm kernel longterm   # newest LTS
#   fcvm kernel 6.18.54    # exact version
. "$(dirname "$0")/common.sh"
need curl jq make gcc flex bison bc xz

channel=${1:-$KERNEL_CHANNEL}
case $ARCH in
    x86_64)  karch=x86_64; image=vmlinux ;;
    aarch64) karch=arm64;  image=arch/arm64/boot/Image ;;
    *)       die "unsupported arch $ARCH" ;;
esac
fragment=$FCVM_ROOT/kernel/microvm-$ARCH.config
[ -f "$fragment" ] || die "no config fragment for $ARCH ($fragment)"

releases=$(curl -fsSL https://www.kernel.org/releases.json)
case $channel in
    stable|mainline|longterm)
        ver=$(jq -r --arg m "$channel" '[.releases[] | select(.moniker == $m)][0].version' <<<"$releases") ;;
    *)  ver=$channel ;;
esac
[ -n "$ver" ] && [ "$ver" != null ] || die "could not resolve kernel version for '$channel'"
url=$(jq -r --arg v "$ver" '.releases[] | select(.version == $v) | .source' <<<"$releases")
if [ -z "$url" ]; then   # older point release no longer listed on the front page
    url=https://cdn.kernel.org/pub/linux/kernel/v${ver%%.*}.x/linux-$ver.tar.xz
fi
log "kernel $ver ($channel)"

out=$KERNELS_DIR/vmlinux-$ver
if [ -f "$out" ] && [ "${FORCE:-0}" != 1 ]; then
    log "$out already built (FORCE=1 to rebuild)"
    ln -sfn "vmlinux-$ver" "$KERNELS_DIR/vmlinux"; exit 0
fi

mkdir -p "$CACHE_DIR" "$BUILD_DIR" "$KERNELS_DIR"
tarball=$CACHE_DIR/${url##*/}
if [ ! -f "$tarball" ]; then
    log "downloading $url"
    curl -fL --progress-bar -o "$tarball.part" "$url"
    mv "$tarball.part" "$tarball"
fi
# kernel.org publishes a sha256sums.asc per major series (not for -rc snapshots).
if [[ $url == https://cdn.kernel.org/* ]]; then
    sums=$(curl -fsSL "${url%/*}/sha256sums.asc" || true)
    want=$(awk -v f="${url##*/}" '$2 == f {print $1}' <<<"$sums")
    if [ -n "$want" ]; then
        [ "$(sha256sum "$tarball" | cut -d' ' -f1)" = "$want" ] || { rm -f "$tarball"; die "sha256 mismatch for ${url##*/}"; }
        log "sha256 verified"
    else
        warn "no published checksum found for ${url##*/}"
    fi
fi

src=$BUILD_DIR/linux-$ver
if [ ! -f "$src/Makefile" ]; then
    log "extracting to $src"
    rm -rf "$src"; mkdir -p "$src"
    tar -xf "$tarball" -C "$src" --strip-components=1
fi

cd "$src"
log "configuring: allnoconfig + ${fragment#$FCVM_ROOT/}"
make -s ARCH=$karch allnoconfig
KCONFIG_CONFIG=.config scripts/kconfig/merge_config.sh -m .config "$fragment" >/dev/null
make -s ARCH=$karch olddefconfig

# Report fragment options that did not survive (renamed, or unmet dependencies).
missing=0
while IFS= read -r line; do
    sym=${line%%=*}
    if [[ $line == *=* ]]; then grep -qx "$line" .config || { warn "requested $line, got: $(grep -E "^(# )?$sym[= ]" .config || echo 'not present')"; missing=1; }; fi
done < <(grep -E '^CONFIG_' "$fragment")
[ $missing = 0 ] && log "all fragment options applied"

log "building $image with $(nproc) jobs"
make -s ARCH=$karch -j"$(nproc)" "${image##*/}"
[ -f "$image" ] || die "build failed"

install -m 0644 "$image" "$out"
install -m 0644 .config "$KERNELS_DIR/config-$ver"
ln -sfn "vmlinux-$ver" "$KERNELS_DIR/vmlinux"
log "kernel ready: $out ($(du -h "$out" | cut -f1))"
