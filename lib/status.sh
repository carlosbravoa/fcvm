#!/usr/bin/env bash
# fcvm status [--offline]: is everything installed, current and healthy?
#
# Checks the host, each component (Firecracker, kernel, initramfs, base
# image), the network, the service and the jailer, flags what's out of date
# with the command that fixes it, then lists VMs and images. Newest versions
# come from kernel.org and GitHub (cached for 6 h; --offline skips them).
# Exit status: 1 if something is broken (✗), 0 otherwise (even with ! notes).
. "$(dirname "$0")/common.sh"

OFFLINE=0 BROKEN=() NOTES=()
case ${1:-} in
    --offline) OFFLINE=1 ;;
    "")        ;;
    *)         die "usage: fcvm status [--offline]" ;;
esac

ok()   { printf '  \e[32m✓\e[0m %-14s %s\n' "$1" "$2"; }
note() { printf '  \e[33m!\e[0m %-14s %s\n' "$1" "$2"; NOTES+=("$3"); }
bad()  { printf '  \e[31m✗\e[0m %-14s %s\n' "$1" "$2"; BROKEN+=("$3"); }
info() { printf '  \e[2m-\e[0m %-14s %s\n' "$1" "$2"; }
head_() { printf '\n\e[1m%s\e[0m\n' "$1"; }
day()  { date -d "@$(stat -c %Y "$1")" +%F; }
same() { cmp -s "$1" "$2" 2>/dev/null; }   # identical files (installed copy vs tree)

# Newest versions, cached: a status check shouldn't hammer kernel.org or GitHub.
cached_fetch() {   # name url -> body (empty when offline or unreachable)
    local f=$CACHE_DIR/status-$1.json
    [ $OFFLINE = 0 ] || { cat "$f" 2>/dev/null || true; return; }
    if [ ! -s "$f" ] || [ $(( $(date +%s) - $(stat -c %Y "$f") )) -gt 21600 ]; then
        mkdir -p "$CACHE_DIR"
        curl -fsSL -m 5 "$2" -o "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f" || rm -f "$f.tmp"
    fi
    cat "$f" 2>/dev/null || true
}

VERSION_NOW=$(fcvm_version)
printf '\e[1mfcvm %s\e[0m\n' "$VERSION_NOW"
printf '  code   %s\n  state  %s\n' "$FCVM_ROOT" "$FCVM_HOME"
[ ! -f "$FCVM_CONF" ] || printf '  config %s\n' "$FCVM_CONF"

# Newer fcvm releases: the highest vX.Y.Z tag on GitHub.
latest=$(cached_fetch fcvm https://api.github.com/repos/carlosbravoa/fcvm/tags |
    jq -r '.[].name' 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//' | sort -V | tail -1 || true)
base=${VERSION_NOW%%+*}
if [ -n "$latest" ] && [ "$(printf '%s\n' "$base" "$latest" | sort -V | tail -1)" != "$base" ]; then
    how=$([ -e "$FCVM_ROOT/.git" ] && echo "git pull" || echo "fcvm upgrade")
    printf '  \e[33m!\e[0m fcvm %s is available\n' "$latest"
    NOTES+=("fcvm $latest: $how")
fi


# --- host ---------------------------------------------------------------------------
head_ "Host"
if [ ! -e /dev/kvm ]; then bad KVM "no /dev/kvm (virtualization disabled, or no nested virtualization)" "enable virtualization"
elif [ -r /dev/kvm ] && [ -w /dev/kvm ]; then ok KVM "/dev/kvm accessible"
else bad KVM "no access to /dev/kvm" "fcvm host-setup, then log out and back in"; fi
if missing=$("$FCVM_ROOT/lib/host-setup.sh" --check 2>/dev/null); then ok packages "all installed"
else bad packages "$(tr '\n' ';' <<<"$missing" | sed 's/;$//; s/;/; /g')" "fcvm host-setup"; fi
read -r avail size < <(df -B1 --output=avail,size "$FCVM_HOME" | tail -1)
used=$(du -s -B1 "$FCVM_HOME" --exclude=build --exclude=cache --exclude=.git --exclude=lib --exclude=docs 2>/dev/null | cut -f1)   # real blocks: disks are sparse
msg="$(numfmt --to=iec "$avail") free of $(numfmt --to=iec "$size"); fcvm uses $(numfmt --to=iec "${used:-0}") (+ build caches)"
if [ "$avail" -lt $((5 << 30)) ]; then note disk "$msg" "low disk space: fcvm prune, or delete build/linux-* after kernel builds"
else ok disk "$msg"; fi
room=$(( 107 - ${#VMS_DIR} - 18 ))
[ $room -ge 16 ] || note path "VM names limited to $room characters by the checkout's path length" "move fcvm to a shorter path (e.g. ~/fcvm)"

# --- components ----------------------------------------------------------------------
head_ "Components"
latest_fc=$(cached_fetch firecracker https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | jq -r '.tag_name // empty' 2>/dev/null)
if [ ! -x "$FIRECRACKER" ]; then
    bad firecracker "not installed" "fcvm firecracker"
else
    fcv=$("$FIRECRACKER" --version | head -1 | awk '{print $2}')
    if [ -n "$latest_fc" ] && [ "$latest_fc" != "$fcv" ] && [ "$FC_VERSION" = latest ]; then
        note firecracker "$fcv; $latest_fc is available" "Firecracker $latest_fc: fcvm firecracker (snapshots are tied to the version; re-run jail-setup if installed)"
    else
        ok firecracker "$fcv$([ -n "$latest_fc" ] && [ "$latest_fc" = "$fcv" ] && echo " (latest)")$([ "$FC_VERSION" = latest ] || echo " (pinned: FC_VERSION=$FC_VERSION)")"
    fi
fi

frag=$FCVM_ROOT/kernel/microvm-$ARCH.config
if [ ! -e "$KERNELS_DIR/vmlinux" ]; then
    bad kernel "not built" "fcvm kernel"
else
    kv=$(basename "$(readlink -f "$KERNELS_DIR/vmlinux")"); kv=${kv#vmlinux-}
    line="$kv ($KERNEL_CHANNEL), built $(day "$KERNELS_DIR/vmlinux-$kv")"
    rel=$(cached_fetch kernel https://www.kernel.org/releases.json)
    case $KERNEL_CHANNEL in stable|mainline|longterm)
        read -r lv ldate < <(jq -r --arg m "$KERNEL_CHANNEL" \
            '[.releases[] | select(.moniker == $m)][0] | "\(.version) \(.released.isodate)"' <<<"$rel" 2>/dev/null || true) ;;
    esac
    # Options in the fragment that the build doesn't have: after an edit or a
    # git pull, the kernel needs a rebuild to pick them up.
    missing=()
    if [ -f "$KERNELS_DIR/config-$kv" ]; then
        while IFS= read -r opt; do
            grep -qxF "$opt" "$KERNELS_DIR/config-$kv" || missing+=("${opt%%=*}")
        done < <(grep -E '^CONFIG_[A-Z0-9_]+=' "$frag")
    fi
    if [ ${#missing[@]} -gt 0 ] && [ "$frag" -nt "$KERNELS_DIR/config-$kv" ]; then
        note kernel "$line; the config fragment changed since (${missing[*]:0:4}$([ ${#missing[@]} -gt 4 ] && echo ", ..."))" \
            "kernel config changed: FORCE=1 fcvm kernel, then restart VMs"
    elif [ -n "${lv:-}" ] && [ "$lv" != null ] && [ "$lv" != "$kv" ]; then
        note kernel "$line; $lv is available (released $ldate)" "kernel $lv: fcvm kernel, then restart VMs"
    else
        ok kernel "$line$([ "${lv:-}" = "$kv" ] && echo ", latest $KERNEL_CHANNEL (released $ldate)")"
    fi
fi

if [ ! -f "$BUILD_DIR/initramfs.cpio" ]; then
    info initramfs "not built yet (built automatically at the first start)"
elif ! initramfs_current; then
    note initramfs "built $(day "$BUILD_DIR/initramfs.cpio") from a different init/fc-init.c (rebuilt at the next VM start)" "initramfs out of date: fcvm init, then restart VMs"
else
    ok initramfs "built $(day "$BUILD_DIR/initramfs.cpio"), current"
fi

if [ -f "$IMAGES_DIR/ubuntu-26.04.json" ]; then ok "ubuntu base" "ubuntu-26.04, built $(day "$IMAGES_DIR/ubuntu-26.04.ext4")"
else info "ubuntu base" "not built (optional: fcvm base)"; fi

# --- services -------------------------------------------------------------------------
head_ "Services"
boot=$(systemctl is-enabled -q fcvm-net.service 2>/dev/null && echo "set up at boot (fcvm-net)" || echo "until reboot (fcvm net-up)")
if ip link show "$NET_BRIDGE" &>/dev/null && ip link show "$NET_R_BRIDGE" &>/dev/null; then
    taps=$(ip -br link | grep -cE '^fc(r)?tap[0-9]+')
    iso=$(bridge -d link show dev "${NET_R_BRIDGE/fcbr1/fcrtap}0" 2>/dev/null | grep -o 'isolated on' || true)
    [ -n "$iso" ] || iso=$(bridge -d link show 2>/dev/null | grep -A2 fcrtap0 | grep -o 'isolated on' || true)
    ok network "$NET_BRIDGE and $NET_R_BRIDGE up, $taps taps${iso:+, isolation on}; $boot"
else
    bad network "bridges are down" "fcvm net-up (or fcvm service install, to set them up at every boot)"
fi
if [ -f /etc/fcvm/net.env ] && ! same /usr/local/lib/fcvm/net.sh "$FCVM_ROOT/lib/net.sh"; then
    note "boot network" "the installed copy of net.sh differs from this tree" "refresh the boot-time network: fcvm service install"
fi

if systemctl is-active -q fcvm.service 2>/dev/null; then
    ok service "fcvm.service active: $(jq -r .url "$VMS_DIR/.serve.json" 2>/dev/null)"
    spid=$(systemctl show -p MainPID --value fcvm.service)
    started=$(date -d "$(ps -o lstart= -p "$spid")" +%s 2>/dev/null || echo 0)
    newest=$(stat -c %Y "$FCVM_ROOT/lib/web/server.py" "$FCVM_ROOT/lib/web/supervisor.py" | sort -n | tail -1)
    if [ "$newest" -gt "$started" ]; then
        note service "running code older than this tree" "reload the service: fcvm service install (running VMs keep running)"
    fi
elif systemctl is-enabled -q fcvm.service 2>/dev/null; then
    bad service "fcvm.service installed but not running" "sudo systemctl start fcvm (see: journalctl -u fcvm)"
elif [ -f "$VMS_DIR/.serve.json" ] && [ -e "/proc/$(jq -r .pid "$VMS_DIR/.serve.json")" ]; then
    ok service "fcvm serve running (not as a service): $(jq -r .url "$VMS_DIR/.serve.json")"
else
    info service "not installed: no console at boot, no restart policies (fcvm service install)"
fi

if [ -S /run/fcvm/jaild.sock ]; then
    stale=()
    same /usr/local/lib/fcvm/jaild.py "$FCVM_ROOT/lib/jaild.py" || stale+=(jaild.py)
    same /usr/local/lib/fcvm/firecracker "$FIRECRACKER" || stale+=(firecracker)
    same /usr/local/lib/fcvm/jailer "$BIN_DIR/jailer" || stale+=(jailer)
    if [ ${#stale[@]} -gt 0 ]; then
        note jailer "fcvm-jaild active, but its copies differ from this tree (${stale[*]})" "refresh the jailer helper: fcvm jail-setup"
    else
        ok jailer "fcvm-jaild active, up to date"
    fi
elif systemctl is-enabled -q fcvm-jaild.service 2>/dev/null; then
    bad jailer "fcvm-jaild installed but not running" "sudo systemctl start fcvm-jaild (see: journalctl -u fcvm-jaild)"
else
    info jailer "not installed (optional: fcvm jail-setup)"
fi

# --- running VMs on old components ---------------------------------------------------------
cur_kernel=$(readlink -f "$KERNELS_DIR/vmlinux" 2>/dev/null || true)
old_k=() old_i=()
for d in "$VMS_DIR"/*/; do
    [ -f "$d/pid" ] && [ -f "$d/fc.json" ] || continue
    vm=$(basename "$d"); p=$(cat "$d/pid")
    [ -e "/proc/$p" ] || continue
    k=$(jq -r '."boot-source".kernel_image_path' "$d/fc.json")
    [ "$(readlink -f "$k")" = "$cur_kernel" ] || old_k+=("$vm")
    [ "$BUILD_DIR/initramfs.cpio" -nt "$d/pid" ] && old_i+=("$vm")
done
if [ ${#old_k[@]} -gt 0 ] || [ ${#old_i[@]} -gt 0 ]; then
    head_ "Running VMs on older components"
    [ ${#old_k[@]} -eq 0 ] || note kernel "${old_k[*]}" "restart VMs to use the current kernel: ${old_k[*]}"
    [ ${#old_i[@]} -eq 0 ] || note initramfs "${old_i[*]}" "restart VMs to use the current initramfs: ${old_i[*]}"
fi

# --- VMs and images ----------------------------------------------------------------------
head_ "VMs"
"$FCVM_ROOT/lib/vm.sh" ls 2>/dev/null | sed 's/^/  /'
head_ "Images"
"$FCVM_ROOT/lib/vm.sh" images 2>/dev/null | sed 's/^/  /'
vols=("$VOLUMES_DIR"/*.ext4) snaps=("$SNAPSHOTS_DIR"/*/)
nvol=0 nsnap=0
[ -e "${vols[0]}" ] && nvol=${#vols[@]}
[ -e "${snaps[0]}" ] && nsnap=${#snaps[@]}
printf '\n  %d volume(s), %d snapshot(s)\n' "$nvol" "$nsnap"

# --- summary ---------------------------------------------------------------------------
if [ ${#BROKEN[@]} -eq 0 ] && [ ${#NOTES[@]} -eq 0 ]; then
    printf '\n\e[32mAll good.\e[0m\n'
else
    printf '\n\e[1mTo do:\e[0m\n'
    for m in "${BROKEN[@]}"; do printf '  \e[31m✗\e[0m %s\n' "$m"; done
    for m in "${NOTES[@]}"; do printf '  \e[33m!\e[0m %s\n' "$m"; done
fi
[ ${#BROKEN[@]} -eq 0 ]
