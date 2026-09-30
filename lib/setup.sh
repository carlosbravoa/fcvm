#!/usr/bin/env bash
# fcvm setup [-y]: guided first-time setup (and safe to re-run).
#
# Checks each step and skips what's already done, asks before anything
# optional, and orders the work so the steps that need sudo come first and
# the long kernel build runs unattended at the end. -y takes the default
# answer for every question.
. "$(dirname "$0")/common.sh"

YES=0
case ${1:-} in
    -y|--yes) YES=1 ;;
    "")       ;;
    *)        die "usage: fcvm setup [-y]" ;;
esac

bold() { printf '\n\e[1m%s\e[0m\n' "$*"; }
ok()   { printf '  \e[32m✓\e[0m %s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
# ask QUESTION y|n: the default is taken with -y or without a terminal.
ask() {
    local def=$2 a hint
    hint=$([ "$def" = y ] && echo "[Y/n]" || echo "[y/N]")
    if [ $YES = 1 ] || ! { : </dev/tty; } 2>/dev/null; then
        note "$1 $hint ${def}"
        [ "$def" = y ]; return
    fi
    read -rp "  $1 $hint " a </dev/tty || a=""
    [[ ${a:-$def} =~ ^[Yy] ]]
}
SUDO_OK=0
sudo_once() {   # one password prompt up front, instead of one per step
    [ $SUDO_OK = 1 ] && return 0
    # Passwordless sudo first: `sudo -v` can still demand a password there
    # (sudoers' verifypw), and fails without a terminal.
    if ! sudo -n true 2>/dev/null; then
        note "(this step needs sudo)"
        sudo -v || die "sudo is needed for this step; re-run fcvm setup from a terminal where sudo works"
    fi
    SUDO_OK=1
}
RELOGIN=0

printf '\e[1mfcvm setup\e[0m: checks each step, skips what is done, asks before anything optional.\n'

# --- 0. the platform ------------------------------------------------------------
[ "$(uname -m)" = x86_64 ] || die "fcvm needs an x86_64 host (this is $(uname -m))"
[ -e /dev/kvm ] || die "no /dev/kvm: enable virtualization (VT-x/AMD-V) in the firmware, or use a host with nested virtualization or bare metal"
# Unix sockets live in vms/<vm>/, and their paths are limited to 107 bytes.
room=$(( 107 - ${#VMS_DIR} - 18 ))
[ $room -ge 1 ] || die "the state directory's path is too long for VM sockets ($FCVM_HOME): set FCVM_HOME to a shorter path"
[ $room -ge 16 ] || warn "the state directory's path is long ($FCVM_HOME), so VM names can have at most $room characters; a shorter FCVM_HOME avoids that"

# --- 1. host packages and KVM access ------------------------------------------------
bold "1/7  Host packages and KVM access"
if missing=$("$FCVM_ROOT/lib/host-setup.sh" --check); then
    ok "installed"
else
    printf '%s\n' "$missing" | sed 's/^/  missing: /'
    command -v apt-get >/dev/null || die "install those with your package manager, then re-run fcvm setup"
    ask "Install them now (apt, sudo)?" y || die "fcvm can't run without them; re-run fcvm setup when ready"
    sudo_once
    had_kvm=$({ [ -r /dev/kvm ] && [ -w /dev/kvm ]; } && echo 1 || echo 0)
    "$FCVM_ROOT/lib/host-setup.sh"
    [ "$had_kvm" = 1 ] || RELOGIN=1
fi

# --- 2. Firecracker ---------------------------------------------------------------
bold "2/7  Firecracker"
if [ -x "$FIRECRACKER" ]; then
    ok "$("$FIRECRACKER" --version | head -1)"
else
    "$FCVM_ROOT/lib/fetch-firecracker.sh"
fi

# --- 3. the jailer (before the service, which orders itself after it) (optional) -----------------------------------------------------
bold "3/7  Jailer (stronger isolation, optional)"
if [ -S /run/fcvm/jaild.sock ]; then
    ok "fcvm-jaild is running"
else
    note "Runs each VM's Firecracker as its own unprivileged user, in a chroot, with cgroup"
    note "limits and its own network namespace. Recommended for untrusted code and for agents."
    if ask "Install the jailer helper (a small root service)?" y; then
        sudo_once
        "$FCVM_ROOT/lib/jail-setup.sh"
    else
        note "skipped; fcvm jail-setup any time"
    fi
fi

# --- 4. the network (now, or at every boot) ---------------------------------------------
bold "4/7  Network"
if systemctl is-enabled -q fcvm-net.service 2>/dev/null; then
    ok "set up at every boot by the fcvm service"
    systemctl is-active -q fcvm-net.service || { sudo_once; sudo systemctl start fcvm-net.service; }
else
    note "VMs need two bridges and firewall rules. The fcvm service can set them up at every"
    note "boot, and keep the web console, API and restart policies running (two systemd units)."
    if ask "Run fcvm as a service, starting at boot?" y; then
        sudo_once
        "$FCVM_ROOT/lib/service.sh" install
    elif ip link show "$NET_BRIDGE" &>/dev/null && ip link show "$NET_R_BRIDGE" &>/dev/null; then
        ok "bridges are up (re-run fcvm net-up after each reboot)"
    else
        sudo_once
        "$FCVM_ROOT/lib/net.sh" up
        note "the network lasts until the next reboot; then run fcvm net-up (or fcvm service install)"
    fi
fi

# --- 5. the guest kernel and initramfs -------------------------------------------------
bold "5/7  Guest kernel"
if [ -e "$KERNELS_DIR/vmlinux" ]; then
    ok "$(basename "$(readlink -f "$KERNELS_DIR/vmlinux")")"
else
    note "Building the newest $KERNEL_CHANNEL kernel from kernel.org sources (a few minutes, once)."
    "$FCVM_ROOT/lib/build-kernel.sh"
fi
initramfs_current || "$FCVM_ROOT/lib/build-init.sh"

# --- 6. images --------------------------------------------------------------------------------
bold "6/7  Images"
# ref | fcvm image name | what it is
IMAGES=("alpine:latest|alpine-latest|a tiny Linux with busybox (~4 MB)"
        "ubuntu:latest|ubuntu-latest|Ubuntu's userland, with apt (~30 MB)"
        "debian:stable-slim|debian-stable-slim|Debian's userland, with apt (~30 MB)"
        "python:3.13-slim|python-3.13-slim|Python 3.13 (~45 MB)"
        "node:22-slim|node-22-slim|Node.js 22 (~75 MB)")
SYSTEM=$(( ${#IMAGES[@]} + 1 ))
note "Container images to start with (any other image later: fcvm import IMAGE):"
for k in "${!IMAGES[@]}"; do
    IFS='|' read -r ref name what <<<"${IMAGES[$k]}"
    printf '    %d) %-20s %s%s\n' $((k + 1)) "$ref" "$what" "$([ -f "$IMAGES_DIR/$name.json" ] && echo "  [imported]")"
done
printf '    %d) %-20s %s%s\n' $SYSTEM "Ubuntu 26.04 system" "boots systemd like a server; built here (a few minutes)" \
    "$([ -f "$IMAGES_DIR/ubuntu-26.04.json" ] && echo "  [built]")"
default=1; [ ! -f "$IMAGES_DIR/alpine-latest.json" ] || default=none
while :; do
    if [ $YES = 1 ] || ! { : </dev/tty; } 2>/dev/null; then
        picks=$default; note "Choose [$default]: $default"
    else
        read -rp "  Choose one or more (e.g. 1 4), all, or none [$default]: " picks </dev/tty || picks=""
        picks=${picks:-$default}
    fi
    case $picks in all) picks=$(seq -s ' ' 1 $SYSTEM) ;; none) picks="" ;; esac
    bad=$(tr ' ,' '\n\n' <<<"$picks" | grep -v '^$' | grep -vxE "[1-9][0-9]*" || true)
    for n in $(tr ',' ' ' <<<"$picks"); do [ "$n" -ge 1 ] 2>/dev/null && [ "$n" -le $SYSTEM ] || bad+=" $n"; done
    [ -z "$bad" ] && break
    note "not a choice:$bad"
    [ $YES = 0 ] || break
done
FIRST=""
for n in $(tr ',' ' ' <<<"$picks"); do
    if [ "$n" = $SYSTEM ]; then
        if [ -f "$IMAGES_DIR/ubuntu-26.04.json" ]; then ok "ubuntu-26.04"; else "$FCVM_ROOT/lib/build-base.sh"; fi
        FIRST=${FIRST:-ubuntu-26.04}
        continue
    fi
    IFS='|' read -r ref name _ <<<"${IMAGES[$((n - 1))]}"
    if [ -f "$IMAGES_DIR/$name.json" ]; then ok "$name"; else "$FCVM_ROOT/lib/import.sh" "$ref"; fi
    FIRST=${FIRST:-$name}
done
if [ -z "$FIRST" ]; then   # nothing picked: test with whatever is already there
    for name in alpine-latest $(ls "$IMAGES_DIR" 2>/dev/null | sed -n 's/\.json$//p' | grep -v '^_'); do
        [ -f "$IMAGES_DIR/$name.json" ] && { FIRST=$name; break; }
    done
fi

# --- 7. a first VM ------------------------------------------------------------------------
bold "7/7  A first microVM"
if [ $RELOGIN = 1 ] && ! { [ -r /dev/kvm ] && [ -w /dev/kvm ]; }; then
    note "skipped: you were just added to the kvm group; log out and back in first"
elif [ -z "$FIRST" ]; then
    note "skipped: no image yet (fcvm import alpine:latest)"
elif ask "Boot a test VM from $FIRST?" y; then
    t0=$(date +%s%N)
    # the console interleaves the guest's own messages, so match just the marker
    out=$("$FCVM_ROOT/lib/vm.sh" run "$FIRST" -- sh -c 'echo "fcvm-ok $(uname -r)"' 2>/dev/null |
        grep -ao 'fcvm-ok [0-9][0-9.]*' | head -1 || true)
    if [ -n "$out" ]; then
        ok "a VM booted Linux ${out#fcvm-ok }, ran a command and was deleted in $(( ($(date +%s%N) - t0) / 1000000 )) ms"
    else
        warn "the test VM didn't answer; try: fcvm run $FIRST"
    fi
fi

# --- next steps ---------------------------------------------------------------------------
bold "Done. Next:"
[ $RELOGIN = 0 ] || printf '  \e[1;33mLog out and back in first\e[0m (for /dev/kvm access)\n'
[ -z "$FIRST" ] || note "$(printf '%-41s' "fcvm run $FIRST")a shell in a throwaway VM"
if [ -f "$VMS_DIR/.serve.json" ] && systemctl is-active -q fcvm.service 2>/dev/null; then
    note "web console: $(jq -r .url "$VMS_DIR/.serve.json")"
else
    note "fcvm serve                               the web console"
fi
note "claude mcp add fcvm -- $(fcvm_entry) mcp    sandboxes for your coding agent"
note "fcvm status                              what's installed, current and running"
note "docs: docs/README.md, starting with docs/use-cases.md"

# The command itself: an installed fcvm lives in ~/.local/bin, which a
# shell only has on its PATH if its profile adds it.
if [ "$(command -v fcvm 2>/dev/null)" = "" ]; then
    bin=$(dirname "$(fcvm_entry)")
    [ -L "$HOME/.local/bin/fcvm" ] && bin=$HOME/.local/bin
    printf '\n  \e[1;33mfcvm isn\x27t on your PATH\e[0m, so the commands above need %s/fcvm.\n' "$bin"
    note "Add it:  echo 'export PATH=\"$bin:\$PATH\"' >> ~/.bashrc && . ~/.bashrc"
fi
