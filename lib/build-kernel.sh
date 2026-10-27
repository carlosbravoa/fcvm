#!/usr/bin/env bash
# Fetch the newest kernel from kernel.org and build a trimmed Firecracker guest
# vmlinux: `make allnoconfig` + kernel/microvm-$ARCH.config, nothing else.
#
#   fcvm kernel            # newest $KERNEL_CHANNEL release (stable by default)
#   fcvm kernel longterm   # newest LTS
#   fcvm kernel 6.18.54    # exact version
#
# New VMs boot kernels/vmlinux. Before it moves to another build, that build
# must have every fragment option and boot a throwaway VM (--no-check skips).
. "$(dirname "$0")/common.sh"

usage="usage: fcvm kernel [stable|mainline|longterm|X.Y[.Z]] [--no-check]"
channel="" check=1
for a; do
    case $a in
        -h|--help)  cat <<EOT
$usage

Builds kernels/vmlinux-X.Y.Z from kernel.org sources (default channel:
$KERNEL_CHANNEL) and points kernels/vmlinux, the kernel new VMs boot, at it.
A version already built is not rebuilt (FORCE=1 rebuilds it).

kernels/vmlinux only moves to a build that has every option of
kernel/microvm-$ARCH.config and boots a throwaway VM; otherwise it stays
where it was. --no-check moves it anyway.
EOT
                    exit 0 ;;
        --no-check) check=0 ;;
        -*)         die_code invalid "unknown option $a ($usage)" ;;
        *)          [ -z "$channel" ] || die_code invalid "$usage"; channel=$a ;;
    esac
done
channel=${channel:-$KERNEL_CHANNEL}
case $channel in
    stable|mainline|longterm) ;;
    *) [[ $channel =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?(-rc[0-9]+)?$ ]] ||
           die_code invalid "'$channel' is neither a channel nor a kernel version ($usage)" ;;
esac
need curl jq make gcc flex bison bc xz

case $ARCH in
    x86_64)  karch=x86_64; image=vmlinux ;;
    aarch64) karch=arm64;  image=arch/arm64/boot/Image ;;
    *)       die "unsupported arch $ARCH" ;;
esac
fragment=$FCVM_ROOT/kernel/microvm-$ARCH.config
[ -f "$fragment" ] || die "no config fragment for $ARCH ($fragment)"

# Fragment options a kernel config lacks (renamed, or unmet dependencies): a
# warning each, and the count on stdout.
config_missing() {   # CONFIG-FILE
    local line n=0
    while IFS= read -r line; do
        grep -qxF "$line" "$1" && continue
        warn "requested $line, got: $(grep -E "^(# )?${line%%=*}[= ]" "$1" || echo 'not present')"
        n=$((n + 1))
    done < <(grep -E '^CONFIG_[A-Z0-9_]+=' "$fragment")
    echo $n
}

check_vm=""
check_rm() {   # remove the boot test's VM
    [ -n "$check_vm" ] && [ -f "$VMS_DIR/$check_vm/vm.json" ] || { check_vm=""; return 0; }
    QUIET_STOP=1 "$FCVM_ROOT/fcvm" stop "$check_vm" >/dev/null 2>&1 || true
    "$FCVM_ROOT/fcvm" rm "$check_vm" >/dev/null 2>&1 || warn "could not remove the boot test VM '$check_vm' (fcvm rm $check_vm)"
    check_vm=""
}

# Boot a throwaway VM on the build before new VMs get it: a kernel can build
# and still not boot on Firecracker. A container image, no network, ~2 s.
# 0 = booted (or nothing to boot it with: a warning says so), 1 = didn't.
boot_check() {   # VERSION
    local ver=$1 img="" n vm dir i code want
    for n in alpine-latest $(ls "$IMAGES_DIR" 2>/dev/null | sed -n 's/\.json$//p' | grep -v '^_'); do
        [ "$(jq -r '.type // "app"' "$IMAGES_DIR/$n.json" 2>/dev/null)" = app ] && { img=$n; break; }
    done
    if [ ! -x "$FIRECRACKER" ] || [ ! -f "$BUILD_DIR/initramfs.cpio" ] || [ -z "$img" ]; then
        warn "vmlinux-$ver not boot-tested: that needs Firecracker, the initramfs and a container image (fcvm import alpine:latest)"
        return 0
    fi
    vm=kernel-check-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')
    dir=$VMS_DIR/$vm check_vm=$vm
    # uname -r spells 7.3 and 7.4-rc1 as 7.3.0 and 7.4.0-rc1
    want=${ver%%-*}; [[ $want == *.*.* ]] || want+=.0; [[ $ver != *-* ]] || want+=-${ver#*-}
    log "boot test: a throwaway VM from $img on vmlinux-$ver"
    # Started here rather than by the service, which would boot kernels/vmlinux.
    # The console interleaves fc-init's messages, so the verdict is the exit code.
    export FCVM_DAEMON=1
    trap check_rm EXIT
    mkdir -p "$BUILD_DIR"
    if ! "$FCVM_ROOT/fcvm" create "$vm" "$img" --net none --ttl 10m -- \
            sh -c 'echo "fcvm-kernel-check $(uname -r)"; [ "$(uname -r)" = "$1" ]' sh "$want" >"$BUILD_DIR/kernel-check.log" 2>&1 ||
       ! FCVM_KERNEL=$KERNELS_DIR/vmlinux-$ver QUIET_START=1 "$FCVM_ROOT/fcvm" start "$vm" >>"$BUILD_DIR/kernel-check.log" 2>&1; then
        cat "$BUILD_DIR/kernel-check.log" >&2
        check_rm
        warn "the boot test VM didn't start"
        return 1
    fi
    for ((i = 0; i < 300; i++)); do [ -f "$dir/pid" ] || break; sleep 0.2; done
    code=$(jq -r '.code // empty' "$dir/last-exit" 2>/dev/null || true)
    [ "$code" = 0 ] || tail -n 30 "$dir/console.log" >&2 2>/dev/null || true
    check_rm
    case $code in
        0)  ;;
        1)  warn "the boot test VM booted another kernel than $want (console above)"; return 1 ;;
        "") warn "the boot test VM didn't come up$([ $i -lt 300 ] || echo " in 60 s") (console above)"; return 1 ;;
        *)  warn "the boot test command failed with exit code $code (console above)"; return 1 ;;
    esac
    log "boot test passed: Linux $want ran a command"
}

# Point kernels/vmlinux at a build, unless it fails the checks.
switch_to() {   # VERSION MISSING-OPTIONS
    local ver=$1 missing=$2 cur
    cur=$(readlink "$KERNELS_DIR/vmlinux" 2>/dev/null || true)
    if [ "$cur" = "vmlinux-$ver" ]; then log "new VMs already boot vmlinux-$ver"; return; fi
    if [ $check = 1 ] && [ -n "$cur" ]; then
        local why=""
        if [ "$missing" != 0 ]; then why="$missing option(s) of ${fragment#"$FCVM_ROOT"/} didn't apply (above)"
        elif ! boot_check "$ver"; then why="it failed the boot test"
        fi
        [ -z "$why" ] || die "kept kernels/vmlinux at $cur: $why. To use vmlinux-$ver anyway: fcvm kernel $ver --no-check"
    fi
    ln -sfn "vmlinux-$ver" "$KERNELS_DIR/vmlinux"
    log "new VMs boot vmlinux-$ver${cur:+ (was $cur; restart running VMs to move them)}"
}

case $channel in
    stable|mainline|longterm)
        releases=$(curl -fsSL https://www.kernel.org/releases.json)
        ver=$(jq -r --arg m "$channel" '[.releases[] | select(.moniker == $m)][0].version' <<<"$releases")
        [ -n "$ver" ] && [ "$ver" != null ] || die "could not resolve kernel version for '$channel'" ;;
    *)  ver=$channel ;;
esac
log "kernel $ver${releases:+ (newest $channel)}"

out=$KERNELS_DIR/vmlinux-$ver
if [ -f "$out" ] && [ "${FORCE:-0}" != 1 ]; then
    log "$out already built (FORCE=1 to rebuild)"
    missing=0
    [ ! -f "$KERNELS_DIR/config-$ver" ] || missing=$(config_missing "$KERNELS_DIR/config-$ver")
    switch_to "$ver" "$missing"; exit 0
fi

[ -n "${releases:-}" ] || releases=$(curl -fsSL https://www.kernel.org/releases.json)
url=$(jq -r --arg v "$ver" '.releases[] | select(.version == $v) | .source' <<<"$releases")
if [ -z "$url" ]; then   # older point release no longer listed on the front page
    url=https://cdn.kernel.org/pub/linux/kernel/v${ver%%.*}.x/linux-$ver.tar.xz
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
log "configuring: allnoconfig + ${fragment#"$FCVM_ROOT"/}"
make -s ARCH=$karch allnoconfig
KCONFIG_CONFIG=.config scripts/kconfig/merge_config.sh -m .config "$fragment" >/dev/null
make -s ARCH=$karch olddefconfig
missing=$(config_missing .config)
[ "$missing" != 0 ] || log "all fragment options applied"

log "building $image with $(nproc) jobs"
make -s ARCH=$karch -j"$(nproc)" "${image##*/}"
[ -f "$image" ] || die "build failed"

install -m 0644 "$image" "$out"
install -m 0644 .config "$KERNELS_DIR/config-$ver"
log "kernel built: $out ($(du -h "$out" | cut -f1))"
switch_to "$ver" "$missing"
