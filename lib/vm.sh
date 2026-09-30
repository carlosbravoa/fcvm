#!/usr/bin/env bash
# VM lifecycle. Each VM lives in vms/<name>/:
#   rw.ext4     writable layer (sparse); the image disk is shared read-only and
#               fc-init overlays the two in the guest. With --copy, disk.ext4 is
#               a private full copy of the image instead.
#   vm.json     image, type, vcpus, mem, ports, ephemeral
#   fc.json     generated Firecracker config (per start)
#   fc.sock     API socket, vsock.sock, console.log, firecracker.log, pid, ip,
#               portfwd.pid, portfwd.log
. "$(dirname "$0")/common.sh"

cmd=$1; shift

vm_dir()     { echo "$VMS_DIR/$1"; }
vm_exists()  { [ -f "$VMS_DIR/$1/vm.json" ]; }
vm_pid()     { cat "$VMS_DIR/$1/pid" 2>/dev/null || true; }
vm_jailed()  { [ "$(jq -r '.jail // false' "$VMS_DIR/$1/vm.json" 2>/dev/null)" = true ]; }
boot_id()    { cat /proc/sys/kernel/random/boot_id; }
proc_start() {   # a process's start time (clock ticks since boot), from /proc/PID/stat
    local s; s=$(cat "/proc/$1/stat" 2>/dev/null) || return 0
    s=${s##*) }; set -- $s; echo "${20}"
}
# A VM runs if its pid file names that VM's live Firecracker. pid.id (boot id
# and start time, recorded at start) keeps a stale pid file, left by a crash or
# a host reboot, from matching whatever process reuses the pid. Jailed VMs run
# as another uid, so this reads /proc rather than using kill -0.
vm_running() {
    local p id; p=$(vm_pid "$1")
    [ -n "$p" ] && [ -e "/proc/$p" ] || return 1
    if id=$(cat "$VMS_DIR/$1/pid.id" 2>/dev/null); then
        [ "$id" = "$(boot_id) $(proc_start "$p")" ]
    else
        [ "$(cat "/proc/$p/comm" 2>/dev/null)" = firecracker ]
    fi
}
record_pid_id() { echo "$(boot_id) $(proc_start "$2")" > "$VMS_DIR/$1/pid.id"; }   # vm pid

# Kill pid $1 only if it is still one of our helpers (its command line has $2):
# after a crash or reboot, a recorded pid may belong to anything.
kill_ours() {
    [ -n "$1" ] && tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null | grep -qF -- "$2" && kill "$1" 2>/dev/null || true
}

# Lifecycle changes to one VM (create, start, stop, rm, update, commit,
# snapshot, fork) take its lock, so the supervisor in `fcvm serve` and your
# own commands don't race. Background processes started meanwhile close
# VM_LOCK, or they would hold the lock for their whole life.
exec {VM_LOCK}</dev/null
VM_LOCKED=""
lock_vm() {
    [ "$VM_LOCKED" = "$1" ] && return 0
    mkdir -p "$VMS_DIR/.locks"
    exec {VM_LOCK}>&- {VM_LOCK}>"$VMS_DIR/.locks/vm-$1"
    flock -w 30 "$VM_LOCK" || die "VM '$1' is busy (another fcvm command is changing it)"
    VM_LOCKED=$1
}
unlock_vm() { exec {VM_LOCK}>&- {VM_LOCK}</dev/null; VM_LOCKED=""; }

# Unix socket paths are limited to 108 bytes (107 + NUL), and the longest one
# a VM gets is vms/<vm>/vsock.sock_100NN. Checked up front, because Firecracker
# only says "path must be shorter than SUN_LEN".
sock_room() {
    local p="$VMS_DIR/$1/vsock.sock_10099"
    [ ${#p} -le 107 ] && return 0
    local room=$(( 107 - ${#VMS_DIR} - 18 ))
    if [ $room -ge 1 ]; then
        die "the path to VM '$1' is too long for its sockets (${#p} bytes; Linux allows 107): use a name of at most $room characters, or move fcvm to a shorter directory"
    fi
    die "fcvm's directory is too long for VM sockets (${#p} bytes; Linux allows 107): move it somewhere shorter, e.g. ~/fcvm"
}

RESTART_POLICIES="no on-failure unless-stopped always"
valid_restart() { [[ " $RESTART_POLICIES " == *" $1 "* ]] || die "--restart wants one of: $RESTART_POLICIES"; }

# jaild, printing the helper's reply only when it refuses.
jaild_quiet() { local r; r=$(jaild "$@") || { jq -r '.error // .' <<<"$r" >&2; return 1; }; }

# One request to fcvm-jaild (the root helper for jailed VMs).
jaild() {
    python3 - "$@" <<'PY'
import json, socket, sys
req = {"op": sys.argv[1], "vm": sys.argv[2] if len(sys.argv) > 2 else None}
req.update(a.split("=", 1) for a in sys.argv[3:])
s = socket.socket(socket.AF_UNIX); s.connect("/run/fcvm/jaild.sock")
s.sendall(json.dumps(req).encode() + b"\n")
r = json.loads(s.makefile().readline())
print(json.dumps(r)); sys.exit(0 if r.get("ok") else 1)
PY
}
image_json() { local f=$IMAGES_DIR/$1.json; [ -f "$f" ] || die "no image '$1' (see: ./fcvm images)"; echo "$f"; }

# Writable layer: sparse ext4 holding overlay upper/ and work/ (root-owned via
# tar, no root needed). Optional args replace the container command.
make_rw() {
    local disk=$1 size=$2; shift 2
    local stage; stage=$(mktemp -d)
    mkdir -p "$stage/upper" "$stage/work"
    if [ $# -gt 0 ]; then
        mkdir "$stage/upper/.fcvm"
        printf '%s\0' "$@" > "$stage/upper/.fcvm/argv"
    fi
    chmod -R u=rwX,go=rX "$stage"
    tar -C "$stage" --owner=0 --group=0 --numeric-owner -cf "$stage.tar" upper work
    truncate -s "$size" "$disk"
    # fresh sparse file is all zeros, so skipping journal/inode-table init is safe
    mkfs.ext4 -q -F -L fcvm-rw -E lazy_itable_init=1,lazy_journal_init=1 -d "$stage.tar" "$disk"
    rm -rf "$stage" "$stage.tar"
}

# Replace the container command inside a --copy disk.
set_argv() {
    local disk=$1; shift
    local tmp; tmp=$(mktemp)
    printf '%s\0' "$@" > "$tmp"
    debugfs -w -R "rm /.fcvm/argv" "$disk" >/dev/null 2>&1
    debugfs -w -f - "$disk" >/dev/null 2>&1 <<EOF
write $tmp /.fcvm/argv
sif /.fcvm/argv uid 0
sif /.fcvm/argv gid 0
sif /.fcvm/argv mode 0100644
EOF
    rm -f "$tmp"
}

# Exit status fc-init recorded for the container's main process (empty if none).
exit_code() {
    local dir; dir=$(vm_dir "$1")
    if [ -f "$dir/rw.ext4" ]; then
        debugfs -R "cat /upper/.fcvm/exit-status" "$dir/rw.ext4" 2>/dev/null | tr -dc 0-9
    elif [ -f "$dir/disk.ext4" ]; then
        debugfs -R "cat /.fcvm/exit-status" "$dir/disk.ext4" 2>/dev/null | tr -dc 0-9
    fi
}

create() {
    local usage="usage: fcvm create VM IMAGE [--vcpus N] [--mem MiB] [--disk SIZE] [--copy] [--idle] [--entrypoint CMD] [--net full|none] [--allow HOST,...]... [-p [BIND:]HOST:GUEST]... [-v VOLUME:/PATH[:ro]]... [--restart POLICY] [-- CMD...]"
    local vm=${1:?$usage} image=${2:?$usage}
    shift 2
    local vcpus=$VM_VCPUS mem=$VM_MEM_MIB disk="" copy=0 idle=0 ports=() vols=() shares=() argv=() netmode=full allow=()
    local entrypoint=() set_entrypoint=0 jail=${JAIL:-0} restart=no
    while [ $# -gt 0 ]; do
        case $1 in
            --vcpus)        vcpus=$2; shift 2 ;;
            --mem)          mem=$2; shift 2 ;;
            --disk)         disk=$2; shift 2 ;;
            --copy)         copy=1; shift ;;
            --idle)         idle=1; shift ;;
            --jail)         jail=1; shift ;;
            --no-jail)      jail=0; shift ;;
            --restart)      valid_restart "${2:-}"; restart=$2; shift 2 ;;
            --entrypoint)   set_entrypoint=1; [ -z "${2-}" ] || entrypoint=("$2"); shift 2 ;;
            -p|--publish)   ports+=("$2"); shift 2 ;;
            -v|--volume)    case $2 in
                                /*|./*|../*|~*) shares+=("$2") ;;   # a host directory, mounted live
                                *)              vols+=("$2") ;;     # a named volume
                            esac; shift 2 ;;
            --net)          [[ ${2:-} =~ ^(full|none)$ ]] || die "--net wants full or none (use --allow for a restricted network)"
                            netmode=$2; shift 2 ;;
            --allow)        IFS=, read -ra _a <<<"${2:?--allow wants HOST[,HOST...]}"; allow+=("${_a[@]}"); shift 2 ;;
            --)             shift; argv=("$@"); break ;;
            *)              die "unknown option $1 ($usage)" ;;
        esac
    done
    [[ $vm =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "invalid VM name '$vm'"
    sock_room "$vm"
    lock_vm "$vm"
    vm_exists "$vm" && die "VM '$vm' already exists"
    [ "$restart" = no ] || [ -z "${EPHEMERAL:-}" ] || die "--restart doesn't apply to throwaway VMs (fcvm run); use create"
    local meta type
    meta=$(image_json "$image")
    type=$(jq -r '.type // "app"' "$meta")
    if [ $idle = 1 ]; then
        [ ${#argv[@]} -eq 0 ] && [ $set_entrypoint = 0 ] || die "--idle excludes -- CMD and --entrypoint"
        argv=(/.fcvm/bin/fc-init --idle)   # stay up for exec; stop ends it cleanly
    elif [ ${#argv[@]} -gt 0 ] || [ $set_entrypoint = 1 ]; then
        # docker semantics: -- CMD replaces the image's CMD and keeps its
        # ENTRYPOINT; --entrypoint replaces the entrypoint ("" clears it)
        [ $set_entrypoint = 1 ] || mapfile -t entrypoint < <(jq -r '.entrypoint[]?' "$meta")
        [ ${#argv[@]} -gt 0 ] || mapfile -t argv < <(jq -r '.cmd[]?' "$meta")
        argv=("${entrypoint[@]}" "${argv[@]}")
        [ ${#argv[@]} -gt 0 ] || die "nothing to run: empty entrypoint and command"
    fi
    [ ${#argv[@]} -eq 0 ] || [ "$type" = app ] || die "-- CMD, --idle and --entrypoint only apply to app images (system images boot systemd)"
    [ $copy = 0 ] || [ -z "$(jq -r '.parent // empty' "$meta")" ] || die "--copy needs a base image; '$image' is a committed layer"
    if [ ${#allow[@]} -gt 0 ]; then
        [ "$netmode" = full ] || die "--allow and --net none are mutually exclusive"
        netmode=restricted
        expand_allow "${allow[@]}" >/dev/null   # validate now, expand at start
    fi
    [ "$netmode" != none ] || [ ${#ports[@]} -eq 0 ] || die "-p needs a network; the VM has --net none"
    local p
    for p in "${ports[@]}"; do
        [[ $p =~ ^(([0-9.]+):)?[0-9]+:[0-9]+(/tcp)?$ ]] || die "bad port spec '$p' (want [BIND:]HOSTPORT:GUESTPORT, TCP only)"
    done
    local sharejson='[]' hdir gpath sro
    for p in "${shares[@]}"; do
        [[ $p =~ ^([^:]+):(/[^:,]*)(:ro)?$ ]] || die "bad mount '$p' (want /HOST/DIR:/GUEST/PATH[:ro])"
        hdir=${BASH_REMATCH[1]} gpath=${BASH_REMATCH[2]} sro=${BASH_REMATCH[3]}
        hdir=${hdir/#\~/$HOME}
        [ -d "$hdir" ] || die "not a directory: $hdir"
        sharejson=$(jq --arg h "$(realpath "$hdir")" --arg g "$gpath" --argjson ro "$([ -n "$sro" ] && echo true || echo false)" \
            '. + [{host: $h, path: $g, ro: $ro}]' <<<"$sharejson")
    done
    for p in "${vols[@]}"; do
        [[ $p =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*:/[^:,]*(:ro)?$ ]] || die "bad volume spec '$p' (want NAME:/PATH[:ro] for a named volume, or /HOST/DIR:/PATH[:ro] for a host directory)"
        [ -f "$VOLUMES_DIR/${p%%:*}.ext4" ] || volume_create "${p%%:*}"
    done

    local dir; dir=$(vm_dir "$vm")
    mkdir -p "$dir"
    if [ $copy = 1 ]; then
        cp --sparse=always "$IMAGES_DIR/$image.ext4" "$dir/disk.ext4"
        chmod u+w "$dir/disk.ext4"
        if [ -n "$disk" ]; then
            truncate -s "$disk" "$dir/disk.ext4"
            e2fsck -fy "$dir/disk.ext4" >/dev/null 2>&1 || true
            resize2fs -f "$dir/disk.ext4" >/dev/null 2>&1 || die "resize failed"
        fi
        [ ${#argv[@]} -eq 0 ] || set_argv "$dir/disk.ext4" "${argv[@]}"
    else
        make_rw "$dir/rw.ext4" "${disk:-$VM_DISK}" "${argv[@]}"
    fi
    jq -n --arg image "$image" --arg type "$type" --argjson vcpus "$vcpus" --argjson mem "$mem" \
        --argjson ephemeral "${EPHEMERAL:-false}" \
        --argjson ports "$(jq -n '$ARGS.positional' --args "${ports[@]}")" \
        --argjson volumes "$(jq -n '$ARGS.positional' --args "${vols[@]}")" --argjson shares "$sharejson" \
        --argjson jail "$([ "$jail" = 1 ] && echo true || echo false)" --arg restart "$restart" \
        --argjson net "$(jq -n --arg mode "$netmode" '{mode: $mode, allow: $ARGS.positional}' --args "${allow[@]}")" \
        '{image: $image, type: $type, vcpus: $vcpus, mem_mib: $mem, ports: $ports, volumes: $volumes,
          shares: $shares, net: $net, jail: $jail, restart: $restart, ephemeral: $ephemeral, created: (now | todate)}' > "$dir/vm.json"
    [ -n "${EPHEMERAL:-}" ] || log "created VM '$vm' from $image ($([ $copy = 1 ] && echo 'private copy' || echo 'shared image + writable layer'))"
}

# Claim a free tap: the flock is inherited by firecracker and held for its lifetime.
claim_tap() {   # tap-name-prefix
    mkdir -p "$VMS_DIR/.locks"
    local i
    for ((i = 0; i < NET_TAPS; i++)); do
        [ -e "/sys/class/net/$1$i" ] || continue
        exec {TAP_FD}>"$VMS_DIR/.locks/$1$i"
        if flock -n "$TAP_FD"; then TAP_INDEX=$i; return 0; fi
        exec {TAP_FD}>&-
    done
    return 1
}

# --- egress policy (restricted network, --allow) --------------------------------

# Allowlist entries with @presets expanded (lib/egress-presets.conf).
expand_allow() {
    local e hosts
    for e in "$@"; do
        if [[ $e == @* ]]; then
            hosts=$(awk -v p="$e" '$1 == p {for (i = 2; i <= NF; i++) print $i}' "$FCVM_ROOT/lib/egress-presets.conf")
            [ -n "$hosts" ] || die "unknown egress preset '$e' (see lib/egress-presets.conf)"
            echo "$hosts"
        else
            [[ $e =~ ^(\*\.)?[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "bad allowlist entry '$e' (want host, *.domain, host:port or @preset)"
            echo "$e"
        fi
    done
}

# Policy file the egress proxy reads for the VM at $IP (re-read per request).
write_policy() {   # vm ip
    local dir; dir=$(vm_dir "$1")
    mkdir -p "$VMS_DIR/.egress"
    jq -n --arg vm "$1" --arg log "$dir/egress.log" \
        --argjson allow "$(jq -n '$ARGS.positional' --args $(expand_allow $(jq -r '.net.allow[]?' "$dir/vm.json")))" \
        '{vm: $vm, allow: $allow, log: $log}' > "$VMS_DIR/.egress/$2.json.tmp"
    mv "$VMS_DIR/.egress/$2.json.tmp" "$VMS_DIR/.egress/$2.json"
}

# One egress proxy per user, shared by all restricted VMs, kept alive by a
# restart loop: a crash (or killing the python process to reload it) costs at
# most a second of refused connections. proxy.pid is the loop; kill it to stop.
ensure_proxy() {
    local pidf=$VMS_DIR/.egress/proxy.pid
    if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then return 0; fi
    mkdir -p "$VMS_DIR/.egress"
    (   # don't let the long-lived proxy inherit this VM's tap lock
        if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
        exec {VM_LOCK}>&-
        exec setsid bash -c 'while :; do python3 "$1" --listen "$2" --policy-dir "$3"; sleep 1; done' egress-proxy \
            "$FCVM_ROOT/lib/egress_proxy.py" "$NET_R_PREFIX.1:$EGRESS_PORT" "$VMS_DIR/.egress" \
            </dev/null >>"$VMS_DIR/.egress/proxy.log" 2>&1
    ) &
    echo $! > "$pidf"
    local i
    for ((i = 0; i < 20; i++)); do
        (exec 3<>"/dev/tcp/$NET_R_PREFIX.1/$EGRESS_PORT") 2>/dev/null && return 0
        sleep 0.1
    done
    cat "$VMS_DIR/.egress/proxy.log" >&2; die "egress proxy failed to start"
}


# Publish ports for the VM whose firecracker runs as pid $1 (see portfwd.py).
start_portfwd() {
    [ ${#PORTS[@]} -gt 0 ] || return 0
    if [ -z "$IP" ]; then warn "no network: ports not published"; return 0; fi
    setsid python3 "$FCVM_ROOT/lib/portfwd.py" --watch "$1" --target "$IP" "${PORTS[@]}" \
        </dev/null >"$DIR/portfwd.log" 2>&1 {VM_LOCK}>&- &
    echo $! > "$DIR/portfwd.pid"
    sleep 0.3
    kill -0 $! 2>/dev/null || { cat "$DIR/portfwd.log" >&2; return 1; }
}

# fcvm start [-a] VM: boot in the background (like docker start); -a attaches the
# console afterwards. The serial console is held by lib/console.py, which logs
# it and lets clients attach/detach; when Firecracker exits it runs `_reap`.
start() {
    local usage="usage: fcvm start [-a] VM" attach=0 vm=""
    while [ $# -gt 0 ]; do
        case $1 in
            -a|--attach) attach=1; shift ;;
            -d)          shift ;;   # background is the default now
            -*)          die "unknown option $1 ($usage)" ;;
            *)           vm=$1; shift ;;
        esac
    done
    [ -n "$vm" ] || die "$usage"
    lock_vm "$vm"
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" && die "VM '$vm' is already running (pid $(vm_pid "$vm"))"
    rm -f "$VMS_DIR/$vm/stopped" "$VMS_DIR/$vm/resume"
    [ -x "$FIRECRACKER" ] || die "firecracker not installed (run: ./fcvm firecracker)"
    sock_room "$vm"
    local kernel type vcpus mem image
    DIR=$(vm_dir "$vm")
    kernel=$(default_kernel)
    image=$(jq -r .image "$DIR/vm.json")
    type=$(jq -r .type "$DIR/vm.json")
    vcpus=$(jq -r .vcpus "$DIR/vm.json")
    mem=$(jq -r .mem_mib "$DIR/vm.json")
    mapfile -t PORTS < <(jq -r '.ports[]?' "$DIR/vm.json")
    mapfile -t VOLUMES < <(jq -r '.volumes[]?' "$DIR/vm.json")

    local args="console=ttyS0 reboot=k panic=1" net='[]' mode mac
    mode=$(jq -r '.net.mode // "full"' "$DIR/vm.json")
    IP=""
    rm -f "$DIR/ip"
    case $mode in
        none) ;;
        restricted)
            claim_tap fcrtap || die "no free restricted tap (fcrtap*); run: ./fcvm net-up"
            IP=$NET_R_PREFIX.$((10 + TAP_INDEX))
            local proxy=http://$NET_R_PREFIX.1:$EGRESS_PORT v
            args+=" ip=$IP::$NET_R_PREFIX.1:255.255.255.0:$vm:eth0:off:$NET_R_PREFIX.1 fcvm.proxy=$proxy"
            if [ "$type" = system ]; then
                for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do args+=" systemd.setenv=$v=$proxy"; done
                args+=" systemd.setenv=no_proxy=localhost,127.0.0.1"
            fi
            write_policy "$vm" "$IP"
            ensure_proxy
            net=$(jq -n --arg tap "fcrtap$TAP_INDEX" --arg mac "$(printf '06:01:%02x:%02x:%02x:%02x' ${IP//./ })" \
                '[{iface_id: "eth0", guest_mac: $mac, host_dev_name: $tap}]') ;;
        *)
            if claim_tap fctap; then
                IP=$NET_PREFIX.$((10 + TAP_INDEX))
                local dns; dns=$(vm_dns)
                args+=" ip=$IP::$NET_PREFIX.1:255.255.255.0:$vm:eth0:off:$(tr ' ' : <<<"${dns% }")"
                net=$(jq -n --arg tap "fctap$TAP_INDEX" --arg mac "$(printf '06:00:%02x:%02x:%02x:%02x' ${IP//./ })" \
                    '[{iface_id: "eth0", guest_mac: $mac, host_dev_name: $tap}]')
            else
                warn "no free tap device; starting without network (run: ./fcvm net-up)"
            fi ;;
    esac
    [ -z "$IP" ] || echo "$IP" > "$DIR/ip"

    # Drives, in attach order: vda, vdb, ... (Firecracker keeps config order).
    # fc-init (initramfs) assembles the root from the fcvm.* arguments.
    [ -f "$BUILD_DIR/initramfs.cpio" ] || "$FCVM_ROOT/lib/build-init.sh"
    local drives='[]' n=0 dev letters=abcdefghijklmnopqrstuvwxyz
    add_drive() {   # id path read_only
        dev=/dev/vd${letters:n:1}
        drives=$(jq --arg id "$1" --arg path "$2" --argjson ro "$3" \
            '. + [{drive_id: $id, path_on_host: $path, is_root_device: false, is_read_only: $ro}]' <<<"$drives")
        n=$((n + 1))
    }
    if [ -f "$DIR/disk.ext4" ]; then
        add_drive root "$DIR/disk.ext4" false; args+=" fcvm.root=$dev"
    else
        local chain=() layers=() i
        mapfile -t chain < <(image_chain "$image")
        for i in "${chain[@]}"; do
            [ -f "$IMAGES_DIR/$i.ext4" ] || die "image '$i' is gone; VM '$vm' cannot boot"
        done
        add_drive root "$IMAGES_DIR/${chain[-1]}.ext4" true; args+=" fcvm.root=$dev"
        add_drive rw "$DIR/rw.ext4" false; args+=" fcvm.rw=$dev"
        for ((i = 0; i < ${#chain[@]} - 1; i++)); do
            add_drive "layer$i" "$IMAGES_DIR/${chain[i]}.ext4" true; layers+=("$dev")
        done
        [ ${#layers[@]} -eq 0 ] || args+=" fcvm.layers=$(IFS=,; echo "${layers[*]}")"
    fi
    local vol vname vols=() busy
    for vol in "${VOLUMES[@]}"; do
        vname=${vol%%:*}
        [ -f "$VOLUMES_DIR/$vname.ext4" ] || die "volume '$vname' is gone"
        if [[ $vol != *:ro ]]; then
            busy=$(volume_users "$vname" running rw | grep -vx -- "$vm" || true)
            [ -z "$busy" ] || die "volume '$vname' is in use by running VM(s): $busy"
        fi
        add_drive "vol-$vname" "$VOLUMES_DIR/$vname.ext4" "$([[ $vol == *:ro ]] && echo true || echo false)"
        vols+=("$dev:${vol#*:}")
    done
    [ ${#vols[@]} -eq 0 ] || args+=" fcvm.vols=$(IFS=,; echo "${vols[*]}")"
    # Live host directories: vsock ports 10000+, served by lib/share9p.py.
    local nshares exports=() gspecs=() i h
    nshares=$(jq '.shares // [] | length' "$DIR/vm.json")
    for ((i = 0; i < nshares; i++)); do
        h=$(jq -r ".shares[$i].host" "$DIR/vm.json")
        [ -d "$h" ] || die "shared host directory is gone: $h"
        exports+=("$((10000 + i))=$h$(jq -r "if .shares[$i].ro then \":ro\" else \"\" end" "$DIR/vm.json")")
        gspecs+=("$((10000 + i)):$(jq -r ".shares[$i].path + (if .shares[$i].ro then \":ro\" else \"\" end)" "$DIR/vm.json")")
    done
    [ $nshares = 0 ] || args+=" fcvm.shares=$(IFS=,; echo "${gspecs[*]}")"
    [ "$type" = system ] && args+=" fcvm.exec=/sbin/init"
    if [ "$type" = app ]; then
        args+=" quiet loglevel=1"   # keep the console to the app's output
    else
        args+=" systemd.hostname=$vm"
    fi
    args+=${VM_KERNEL_ARGS:+ $VM_KERNEL_ARGS}

    jq -n --arg kernel "$kernel" --arg initrd "$BUILD_DIR/initramfs.cpio" --arg args "$args" --argjson drives "$drives" \
        --argjson vcpus "$vcpus" --argjson mem "$mem" --argjson net "$net" --arg vsock "$DIR/vsock.sock" '{
        "boot-source": {kernel_image_path: $kernel, initrd_path: $initrd, boot_args: $args},
        "drives": $drives,
        "machine-config": {vcpu_count: $vcpus, mem_size_mib: $mem},
        "network-interfaces": $net,
        "vsock": {guest_cid: 3, uds_path: $vsock},
        "entropy": {}
    }' > "$DIR/fc.json"

    local jailed=0
    if vm_jailed "$vm"; then
        jailed=1
        [ -S /run/fcvm/jaild.sock ] || die "'$vm' runs jailed, but fcvm-jaild isn't running (run: ./fcvm jail-setup)"
        [ "$(/usr/local/lib/fcvm/firecracker --version 2>/dev/null | head -1)" = "$("$FIRECRACKER" --version | head -1)" ] ||
            warn "fcvm-jaild has a different Firecracker than bin/ (re-run ./fcvm jail-setup)"
        # The helper re-validates everything in this request (paths, ownership, writability).
        jq -n --arg vm "$vm" --arg kernel "$kernel" --arg initrd "$BUILD_DIR/initramfs.cpio" --arg args "$args" \
            --argjson drives "$(jq '[.[] | {drive_id, path: .path_on_host, read_only: .is_read_only}]' <<<"$drives")" \
            --argjson vcpus "$vcpus" --argjson mem "$mem" \
            --arg tap "$(jq -r '.[0].host_dev_name // empty' <<<"$net")" --arg mac "$(jq -r '.[0].guest_mac // empty' <<<"$net")" \
            '{op: "launch", vm: $vm, kernel: $kernel, initrd: $initrd, drives: $drives, boot_args: $args,
              vcpus: $vcpus, mem_mib: $mem, tap: (if $tap == "" then null else $tap end), mac: $mac}
              + (if $restore == "" then {} else {restore: $restore} end)' --arg restore "${RESTORE:-}" > "$DIR/jail-request.json"
    fi
    rm -f "$DIR/fc.sock" "$DIR/vsock.sock" "$DIR/console.sock" "$DIR/pid" "$DIR/waiter" "$DIR/firecracker.log"
    [ $jailed = 1 ] || : > "$DIR/firecracker.log"
    : > "$DIR/console.log"
    local fc=("$FIRECRACKER" --api-sock "$DIR/fc.sock" --config-file "$DIR/fc.json"
              --log-path "$DIR/firecracker.log" --level Warning)
    local info="$type, ${vcpus} vCPU, ${mem} MiB, kernel ${kernel##*/}${IP:+, ip $IP}"
    case $mode in
        none)       info+=", no network" ;;
        restricted) info+=", egress: $(jq -r '.net.allow | join(" ")' "$DIR/vm.json")" ;;
    esac
    [ ${#PORTS[@]} -eq 0 ] || info+=", ports ${PORTS[*]}"
    log "starting '$vm' ($info)"

    # Host directories: a 9P server listening at <vsock uds>_<port>. Unjailed it
    # starts before the VM; jailed, once the helper has made the chroot (the
    # guest retries its connection for a few seconds).
    start_shares() {   # uds-prefix
        (   # exits with the VM
            if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
            exec {VM_LOCK}>&-
            exec setsid python3 "$FCVM_ROOT/lib/share9p.py" --uds-prefix "$1" --pidfile "$DIR/pid" \
                "${exports[@]}" </dev/null >"$DIR/share.log" 2>&1
        ) &
        echo $! > "$DIR/share.pid"
        for ((i = 0; i < 50; i++)); do [ -S "${1}_10000" ] && break; sleep 0.02; done
        [ -S "${1}_10000" ] || { cat "$DIR/share.log" >&2; die "host directory server failed to start"; }
    }
    if [ ${#exports[@]} -gt 0 ] && [ $jailed = 0 ]; then start_shares "$DIR/vsock.sock"; fi
    local launch=(-- "${fc[@]}")
    [ $jailed = 0 ] || launch=(--jail "$DIR/jail-request.json" --link-dir "$DIR")
    setsid python3 "$FCVM_ROOT/lib/console.py" serve --sock "$DIR/console.sock" --log "$DIR/console.log" \
        --pidfile "$DIR/pid" --on-exit "$(printf '%q _reap %q' "$FCVM_ROOT/fcvm" "$vm")" "${launch[@]}" \
        </dev/null >"$DIR/relay.log" 2>&1 {VM_LOCK}>&- &
    local i pid=""
    for ((i = 0; i < 100; i++)); do pid=$(vm_pid "$vm"); [ -n "$pid" ] && break; sleep 0.02; done
    [ -n "$pid" ] || { cat "$DIR/relay.log" >&2; die "console relay failed to start"; }
    record_pid_id "$vm" "$pid"
    if [ ${#exports[@]} -gt 0 ] && [ $jailed = 1 ]; then start_shares "$(dirname "$(readlink "$DIR/vsock.sock")")/vsock.sock"; fi
    if ! start_portfwd "$pid"; then
        if [ $jailed = 1 ]; then jaild kill "$vm" >/dev/null || true; else kill "$pid" 2>/dev/null || true; fi
        die "cannot publish ports; VM not started"
    fi
    if [ -n "${RESTORE:-}" ]; then   # fork of a jailed snapshot: same chroot paths, same tap0
        if ! fc_api "$DIR/fc.sock" PUT /snapshot/load '{"snapshot_path": "/snap.vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": "/snap.mem"},
                "clock_realtime": true, "resume_vm": true}'; then
            jaild kill "$vm" >/dev/null || true
            die "restore of '$RESTORE' failed"
        fi
        local gw=""; [ -z "$IP" ] || gw=${IP%.*}.1
        python3 "$FCVM_ROOT/lib/exec_client.py" --netconf "${IP:+$IP/24},$gw,$(jq -r '.[0].guest_mac // empty' <<<"$net"),$vm" \
            "$DIR/vsock.sock" || warn "'$vm' could not be re-addressed"
    fi
    sleep 0.2
    if ! vm_running "$vm"; then
        cat "$DIR/console.log" "$DIR/firecracker.log" >&2
        die "firecracker failed to start"
    fi
    unlock_vm
    if [ $attach = 1 ]; then
        attach_vm "$vm"
    elif [ -z "${QUIET_START:-}" ]; then
        log "running (pid $pid). $([ "$type" = system ] && echo "fcvm shell $vm" || echo "fcvm console $vm") | fcvm logs $vm | fcvm stop $vm"
    fi
}

# Attach this terminal to a running VM's console until it exits (returning a
# container's exit code) or the user detaches with Ctrl-] (returning 0).
attach_vm() {
    local vm=$1 replay=${2:-tail} dir rc=0 i
    dir=$(vm_dir "$vm")
    touch "$dir/waiter"   # ask the reaper to keep the exit code for us
    [ -t 0 ] && log "attached to '$vm' console (Ctrl-] to detach)"
    python3 "$FCVM_ROOT/lib/console.py" attach "$dir/console.sock" --replay "$replay" || rc=$?
    if [ $rc = 2 ]; then
        rm -f "$dir/waiter"
        printf '\n' >&2
        log "detached; '$vm' is still running. fcvm console $vm | fcvm stop $vm"
        return 0
    fi
    for ((i = 0; i < 100; i++)); do [ -f "$dir/pid" ] || break; sleep 0.05; done   # reaper done
    local code=""
    if [ -f "$VMS_DIR/.exit/$vm" ]; then
        code=$(cat "$VMS_DIR/.exit/$vm"); rm -f "$VMS_DIR/.exit/$vm"
    fi
    rm -f "$dir/waiter"
    return "${code:-0}"
}

stop() {
    local vm=${1:?usage: fcvm stop VM} dir pid i
    lock_vm "$vm"
    vm_exists "$vm" || die "no VM '$vm'"
    dir=$(vm_dir "$vm")
    # Stopped by you: restart policies leave it alone until you start it again.
    # A stop for host shutdown (fcvm _shutdown) marks it to resume instead.
    [ -n "${FCVM_SYSTEM_STOP:-}" ] || touch "$dir/stopped"
    if vm_running "$vm"; then
        pid=$(vm_pid "$vm")
        # Resent every 2 s: during early boot the guest's keyboard driver isn't listening yet.
        for ((i = 0; i < 100; i++)); do
            [ -e "/proc/$pid" ] || break
            if ((i % 10 == 0)); then
                curl -fsS --unix-socket "$dir/fc.sock" -X PUT http://localhost/actions \
                    -H 'Content-Type: application/json' -d '{"action_type": "SendCtrlAltDel"}' >/dev/null 2>&1 || true
            fi
            sleep 0.2
        done
        if vm_running "$vm"; then
            warn "guest did not shut down; killing"
            if vm_jailed "$vm"; then jaild kill "$vm" >/dev/null || true; else kill -9 "$pid"; fi
        fi
        for ((i = 0; i < 50; i++)); do [ -f "$dir/pid" ] || break; sleep 0.1; done   # relay reaps
        [ -n "${QUIET_STOP:-}" ] || log "stopped '$vm'"
    fi
    [ -f "$dir/pid" ] && reap "$vm"   # relay gone (crash, host reboot): reap here
    return 0
}

# Run by the console relay once Firecracker has exited: records the container
# exit code (for an attached `run`), stops the port forwarder, removes runtime
# files and deletes throwaway VMs.
reap() {
    local vm=${1:?} dir code
    dir=$(vm_dir "$vm")
    [ -f "$dir/vm.json" ] || return 0
    if [ -f "$dir/waiter" ]; then
        code=$(exit_code "$vm")
        mkdir -p "$VMS_DIR/.exit"
        echo "${code}" > "$VMS_DIR/.exit/$vm"
    fi
    # What ended it, for restart policies (fcvm serve) and inspect: the app's
    # exit code, and Firecracker's own status (0 = the guest shut down or
    # rebooted; otherwise killed or crashed). A stale reap (the VM was running
    # when the host crashed or rebooted) knows neither.
    local fcs=${FCVM_FC_STATUS:-} app=""
    if [ -z "${FCVM_STALE:-}" ]; then
        if [ -z "$fcs" ] && vm_jailed "$vm"; then
            fcs=$(jaild exit_status "$vm" 2>/dev/null | jq -r '.status // empty' 2>/dev/null || true)
        fi
        [ "$(jq -r .type "$dir/vm.json")" != app ] || app=$(exit_code "$vm")
    fi
    jq -n --arg code "$app" --arg fcs "$fcs" --argjson stale "$([ -n "${FCVM_STALE:-}" ] && echo true || echo false)" \
        '{code: ($code | tonumber? // null), fc_status: ($fcs | tonumber? // null), stale: $stale, at: now}' > "$dir/last-exit"
    kill_ours "$(cat "$dir/portfwd.pid" 2>/dev/null)" portfwd.py
    kill_ours "$(cat "$dir/share.pid" 2>/dev/null)" share9p.py; rm -f "$dir/share.pid"
    if [ -f "$dir/share.live" ]; then
        local sp; for sp in $(awk '{print $2}' "$dir/share.live"); do kill_ours "$sp" share9p.py; done
        rm -f "$dir/share.live"
    fi
    rm -f "$dir"/vsock.sock_*
    if [ -f "$dir/ip" ]; then rm -f "$VMS_DIR/.egress/$(cat "$dir/ip").json"; fi
    rm -f "$dir/fc.sock" "$dir/vsock.sock" "$dir/console.sock" "$dir/portfwd.pid" "$dir/pid" "$dir/pid.id"
    if [ -L "$dir/firecracker.log" ]; then rm -f "$dir/firecracker.log"; fi
    if [ "$(jq -r .ephemeral "$dir/vm.json")" = true ]; then rm -rf "$dir" "$VMS_DIR/.locks/vm-$vm"; fi
}

# fcvm run IMAGE [-d] [opts] [-- CMD...]: throwaway VM, deleted when it stops.
#   container image: attached to its console like `docker run` (Ctrl-] detaches);
#                    exits with the container's exit code
#   systemd image:   boots, then `fcvm shell` (or CMD via exec); the VM is
#                    stopped and deleted when the shell/command ends
run() {
    local usage="usage: fcvm run IMAGE [-d] [-p [BIND:]HOST:GUEST]... [--vcpus N] [--mem MiB] [--disk SIZE] [-- CMD...]"
    local image=${1:?$usage}
    shift
    local bg=0 opts=() argv=()
    while [ $# -gt 0 ]; do
        case $1 in
            -d) bg=1; shift ;;
            --) shift; argv=("$@"); break ;;
            *)  opts+=("$1"); shift ;;
        esac
    done
    local type vm meta
    meta=$(image_json "$image")
    type=$(jq -r '.type // "app"' "$meta")
    vm=${image%%-*}-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')

    if [ "$type" = app ]; then
        EPHEMERAL=true create "$vm" "$image" "${opts[@]}" ${argv[@]+-- "${argv[@]}"}
        if [ $bg = 1 ]; then start "$vm"; return; fi
        QUIET_START=1 start "$vm"
        attach_vm "$vm" all
        return
    fi

    EPHEMERAL=true create "$vm" "$image" "${opts[@]}"
    if [ $bg = 1 ]; then start "$vm"; return; fi
    QUIET_START=1 start "$vm"
    local rc=0
    if [ ${#argv[@]} -gt 0 ]; then
        if [ -t 0 ]; then exec_vm -it "$vm" "${argv[@]}" || rc=$?; else exec_vm -i "$vm" "${argv[@]}" || rc=$?; fi
    else
        shell_vm "$vm" || rc=$?
    fi
    QUIET_STOP=1 stop "$vm"
    log "throwaway VM '$vm' stopped and removed"
    return $rc
}

# fcvm console VM: attach to the live serial console (Ctrl-] detaches).
console() {
    local vm=${1:?usage: fcvm console VM}
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" || die "VM '$vm' is not running (see: fcvm logs $vm)"
    attach_vm "$vm" tail
}

# fcvm logs [-f] VM: console output of the current/last boot.
logs() {
    local follow=0 vm=""
    while [ $# -gt 0 ]; do
        case $1 in -f|--follow) follow=1 ;; *) vm=$1 ;; esac
        shift
    done
    [ -n "$vm" ] || die "usage: fcvm logs [-f] VM"
    local f; f=$(vm_dir "$vm")/console.log
    [ -f "$f" ] || die "no console log for '$vm'"
    if [ $follow = 1 ]; then tail -n +1 -f "$f"; else cat "$f"; fi
}

ssh_vm() {
    local vm=${1:?usage: fcvm ssh VM [args]}; shift
    local ip; ip=$(cat "$(vm_dir "$vm")/ip" 2>/dev/null) || die "VM '$vm' has no network"
    exec ssh -i "$FCVM_ROOT/ssh/id_ed25519" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR "root@$ip" "$@"
}

# fcvm exec [-i] [-t] [-u USER] VM [CMD...]: like `docker exec`, over vsock
# (no network or sshd needed). USER is name|uid[:group|gid], resolved in the guest.
exec_vm() {
    local usage="usage: fcvm exec [-i] [-t] [-u USER] [-w DIR] [-e KEY=VAL]... [--timeout SECS] VM [CMD...]" flags=()
    while [[ ${1:-} == -* ]]; do
        case $1 in
            -u|--user)     flags+=(-u "${2:?$usage}"); shift 2 ;;
            -w|--workdir)  flags+=(-w "${2:?$usage}"); shift 2 ;;
            -e|--env)      [[ ${2:-} == *=* ]] || die "-e wants KEY=VALUE"; flags+=(-e "$2"); shift 2 ;;
            --timeout)     [[ ${2:-} =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--timeout wants seconds"; flags+=(--timeout "$2"); shift 2 ;;
            -i|-t|-it|-ti) [[ $1 == *i* ]] && flags+=(-i); [[ $1 == *t* ]] && flags+=(-t); shift ;;
            *)             die "unknown option $1 ($usage)" ;;
        esac
    done
    local vm=${1:?$usage}; shift
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" || die "VM '$vm' is not running"
    [ "$(jq -r '.net.mode // "full"' "$(vm_dir "$vm")/vm.json")" != restricted ] || ensure_proxy
    python3 "$FCVM_ROOT/lib/exec_client.py" "${flags[@]}" "$(vm_dir "$vm")/vsock.sock" -- "$@"
}

# fcvm shell [-u USER] VM: interactive shell (bash, else sh), as the image's user by default.
shell_vm() {
    local usage="usage: fcvm shell [-u USER] VM" user=() vm=""
    while [ $# -gt 0 ]; do
        case $1 in
            -u|--user) user=(-u "${2:?$usage}"); shift 2 ;;
            -*)        die "unknown option $1 ($usage)" ;;
            *)         vm=$1; shift ;;
        esac
    done
    [ -n "$vm" ] || die "$usage"
    if [ -t 0 ]; then exec_vm -it "${user[@]}" "$vm"; else exec_vm -i "${user[@]}" "$vm"; fi
}

# --- volumes: persistent ext4 disks, attached with -v NAME:/PATH[:ro] ---------

volume_create() {
    local name=${1:?usage: fcvm volume create NAME [SIZE]} size=${2:-$VOLUME_SIZE}
    [[ $name =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "invalid volume name '$name'"
    local f=$VOLUMES_DIR/$name.ext4
    [ ! -e "$f" ] || die "volume '$name' already exists"
    mkdir -p "$VOLUMES_DIR"
    truncate -s "$size" "$f"
    mkfs.ext4 -q -F -L "vol-${name:0:12}" -E lazy_itable_init=1,lazy_journal_init=1,root_owner=0:0 "$f"
    debugfs -w -R "rmdir /lost+found" "$f" >/dev/null 2>&1   # an empty root, like a docker volume
    log "created volume '$name' ($size, sparse)"
}

# VMs that reference volume $1. Filters: "running" (only running VMs), "rw" (only rw mounts).
volume_users() {
    local name=$1 want_running=${2:-} want_rw=${3:-} j vm spec
    for j in "$VMS_DIR"/*/vm.json; do
        [ -f "$j" ] || continue
        vm=$(basename "${j%/vm.json}")
        [ -z "$want_running" ] || vm_running "$vm" || continue
        while read -r spec; do
            [ "${spec%%:*}" = "$name" ] || continue
            [ -z "$want_rw" ] || [[ $spec != *:ro ]] || continue
            echo "$vm"; break
        done < <(jq -r '.volumes[]?' "$j")
    done
}

volume_cmd() {
    local sub=${1:-ls}; shift || true
    case $sub in
        create) volume_create "$@" ;;
        ls)
            local json=0 f name
            [ "${1:-}" = --json ] && json=1
            for f in "$VOLUMES_DIR"/*.ext4; do
                [ -f "$f" ] || continue
                name=$(basename "$f" .ext4)
                jq -n --arg name "$name" --argjson size "$(stat -c %s "$f")" \
                    --argjson used "$(( $(stat -c %b "$f") * 512 ))" \
                    --argjson vms "$(jq -n '$ARGS.positional' --args $(volume_users "$name"))" \
                    '{name: $name, size_bytes: $size, used_bytes: $used, vms: $vms}'
            done | if [ $json = 1 ]; then jq -s .; else
                jq -rs '(["NAME","SIZE","USED","VMS"] | @tsv), (.[] | [.name, "\(.size_bytes / 1073741824 | floor)G",
                    "\(.used_bytes / 1048576 | floor)M", (.vms | join(","))] | @tsv)' | column -t -s $'\t'; fi ;;
        rm)
            local name=${1:?usage: fcvm volume rm NAME} users
            [ -f "$VOLUMES_DIR/$name.ext4" ] || die "no volume '$name'"
            users=$(volume_users "$name" | tr '\n' ' ')
            [ -z "$users" ] || die "volume '$name' is used by VM(s): $users"
            rm -f "$VOLUMES_DIR/$name.ext4"
            log "removed volume '$name'" ;;
        *) die "usage: fcvm volume create NAME [SIZE] | ls [--json] | rm NAME" ;;
    esac
}

# --- images: commit and remove --------------------------------------------------

# fcvm commit VM IMAGE: the VM's writable layer becomes a read-only layer on top
# of the VM's image (docker commit). Instant: the layer disk is copied, not
# merged. A --copy VM's private disk becomes a standalone base image instead.
commit() {
    local vm=${1:?usage: fcvm commit VM IMAGE} image=${2:?usage: fcvm commit VM IMAGE}
    lock_vm "$vm"
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" && die "VM '$vm' is running; stop it first (fcvm stop $vm)"
    [[ $image =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "invalid image name '$image'"
    [ ! -e "$IMAGES_DIR/$image.json" ] || die "image '$image' already exists"
    local dir parent src out rc=0
    dir=$(vm_dir "$vm")
    parent=$(jq -r .image "$dir/vm.json")
    out=$IMAGES_DIR/$image.ext4
    if [ -f "$dir/disk.ext4" ]; then src=$dir/disk.ext4; else src=$dir/rw.ext4; fi
    cp --sparse=always "$src" "$out"
    chmod u+w "$out"
    e2fsck -fy "$out" >/dev/null 2>&1 || rc=$?   # replay the journal if the VM was killed
    [ $rc -lt 4 ] || { rm -f "$out"; die "the VM's disk has errors e2fsck could not fix"; }
    # Per-VM state, not image content: the last exit status, and the command
    # override from create --idle / -- CMD (the image keeps its own command).
    local f pre; pre=$([ "$src" = "$dir/rw.ext4" ] && echo /upper || true)
    for f in exit-status argv; do
        [ "$f" = argv ] && [ -z "$pre" ] && continue   # --copy disk: argv is the image's own
        debugfs -w -R "rm $pre/.fcvm/$f" "$out" >/dev/null 2>&1 || true
    done
    [ "$(jq -r .type "$dir/vm.json")" != system ] || scrub_identity "$out" "$pre"
    chmod a-w "$out"
    if [ -f "$dir/disk.ext4" ]; then
        jq --arg vm "$vm" --arg parent "$parent" 'del(.parent) + {ref: "commit of \($vm) (from \($parent))"}' \
            "$IMAGES_DIR/$parent.json" > "$IMAGES_DIR/$image.json"
    else
        jq --arg vm "$vm" --arg parent "$parent" '. + {parent: $parent, ref: "commit of \($vm) (from \($parent))"}' \
            "$IMAGES_DIR/$parent.json" > "$IMAGES_DIR/$image.json"
    fi
    log "committed '$vm' as image '$image' ($(du -h "$out" | cut -f1) on disk$([ -f "$dir/disk.ext4" ] || echo ", layer on $parent"))"
}

# Merge committed layers (images, OLDEST first) into one new layer disk $1,
# in a throwaway VM that boots only the initramfs (fc-init merge mode).
squash_layers() {
    local out=$1; shift
    local drives='[]' devs=() n=0 letters=abcdefghijklmnopqrstuvwxyz img bytes=0 tmp rc=0
    for img in "$@"; do
        drives=$(jq --arg id "l$n" --arg p "$IMAGES_DIR/$img.ext4" \
            '. + [{drive_id: $id, path_on_host: $p, is_root_device: false, is_read_only: true}]' <<<"$drives")
        devs+=("/dev/vd${letters:n:1}"); n=$((n + 1))
        bytes=$((bytes + $(stat -c %b "$IMAGES_DIR/$img.ext4") * 512))
    done
    make_rw "$out" "$(( bytes * 13 / 10 / 1048576 + 512 ))M"
    drives=$(jq --arg p "$out" '. + [{drive_id: "out", path_on_host: $p, is_root_device: false, is_read_only: false}]' <<<"$drives")
    tmp=$(mktemp -d)
    jq -n --arg kernel "$(default_kernel)" --arg initrd "$BUILD_DIR/initramfs.cpio" --argjson drives "$drives" \
        --arg args "console=ttyS0 reboot=k panic=1 quiet fcvm.merge=$(IFS=,; echo "${devs[*]}") fcvm.merge_out=/dev/vd${letters:n:1}" '{
        "boot-source": {kernel_image_path: $kernel, initrd_path: $initrd, boot_args: $args},
        "drives": $drives, "machine-config": {vcpu_count: 2, mem_size_mib: 512}}' > "$tmp/fc.json"
    timeout 1800 "$FIRECRACKER" --no-api --config-file "$tmp/fc.json" --log-path "$tmp/fc.log" \
        </dev/null >"$tmp/console.log" 2>&1 || rc=$?
    if [ "$(debugfs -R "cat /merge-ok" "$out" 2>/dev/null)" != ok ]; then
        grep -a 'fc-init' "$tmp/console.log" >&2 || tail -20 "$tmp/console.log" >&2
        rm -rf "$tmp" "$out"
        die "layer merge failed (firecracker status $rc)"
    fi
    debugfs -w -R "rm /merge-ok" "$out" >/dev/null 2>&1
    e2fsck -fy "$out" >/dev/null 2>&1 || true
    rm -rf "$tmp"
}

# fcvm squash IMAGE NEW: IMAGE's committed layers merged into one layer on the
# same base image (keeps layer chains, and so drive counts, small).
squash() {
    local image=${1:?usage: fcvm squash IMAGE NEW} new=${2:?usage: fcvm squash IMAGE NEW}
    image_json "$image" >/dev/null
    [[ $new =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "invalid image name '$new'"
    [ ! -e "$IMAGES_DIR/$new.json" ] || die "image '$new' already exists"
    local chain=() layers=() i
    mapfile -t chain < <(image_chain "$image")
    [ ${#chain[@]} -gt 2 ] || die "'$image' has ${#chain[@]} level(s); nothing to squash"
    for ((i = ${#chain[@]} - 2; i >= 0; i--)); do layers+=("${chain[i]}"); done
    log "merging ${#layers[@]} layers of '$image' onto ${chain[-1]}"
    squash_layers "$IMAGES_DIR/$new.ext4" "${layers[@]}"
    chmod a-w "$IMAGES_DIR/$new.ext4"
    jq --arg base "${chain[-1]}" --arg image "$image" '. + {parent: $base, ref: "squash of \($image)"}' \
        "$IMAGES_DIR/$image.json" > "$IMAGES_DIR/$new.json"
    log "image '$new': one layer ($(du -h "$IMAGES_DIR/$new.ext4" | cut -f1)) on ${chain[-1]}"
}

# fcvm prune: delete the build cache (_bc-* images) and leftover build VMs.
# Built images don't depend on the cache (their layer is a copy), so this only
# makes the next build of each file slower.
prune() {
    local vm d removed=0 freed=0 left img
    for d in "$VMS_DIR"/_build-*/; do
        [ -f "$d/vm.json" ] || continue
        vm=$(basename "$d")
        vm_running "$vm" && continue    # a build in progress
        rm -rf "$d"
    done
    while :; do   # leaves first: an image can go once nothing is layered on it
        left=0
        for img in "$IMAGES_DIR"/_bc*.json; do
            [ -f "$img" ] || continue
            img=$(basename "$img" .json)
            if [ -z "$(image_users "$img")" ]; then
                freed=$((freed + $(stat -c %b "$IMAGES_DIR/$img.ext4") * 512))
                rm -f "$IMAGES_DIR/$img.ext4" "$IMAGES_DIR/$img.json"
                removed=$((removed + 1)); left=1
            fi
        done
        [ $left = 1 ] || break
    done
    log "removed $removed build cache image(s), $((freed / 1048576)) MiB freed"
}

# Per-machine identity a booted systemd VM leaves on its disk. Removed from a
# committed layer (the base image's blank versions show through again) so every
# VM from the image generates its own at first boot.
scrub_identity() {   # disk path-prefix ("/upper" for a layer, "" for a --copy disk)
    local disk=$1 pre=$2 f d sub cmds=()
    for f in /etc/machine-id /var/lib/dbus/machine-id /var/lib/systemd/random-seed \
             /etc/ssh/ssh_host_{rsa,ecdsa,ed25519}_key{,.pub}; do
        cmds+=("rm $pre$f")
    done
    for d in $(debugfs -R "ls -p $pre/var/log/journal" "$disk" 2>/dev/null | awk -F/ '$6 != "." && $6 != ".." && $6 != "" {print $6}'); do
        for sub in $(debugfs -R "ls -p $pre/var/log/journal/$d" "$disk" 2>/dev/null | awk -F/ '$6 != "." && $6 != ".." && $6 != "" {print $6}'); do
            cmds+=("rm $pre/var/log/journal/$d/$sub")
        done
        cmds+=("rmdir $pre/var/log/journal/$d")
    done
    if [ -z "$pre" ]; then   # a whole disk: leave an empty machine-id, as the base image has
        local empty; empty=$(mktemp); cmds+=("write $empty /etc/machine-id" "sif /etc/machine-id mode 0100444")
    fi
    printf '%s\n' "${cmds[@]}" | debugfs -w -f - "$disk" >/dev/null 2>&1 || true
    [ -z "${empty:-}" ] || rm -f "$empty"
}

rmi() {
    local image=${1:?usage: fcvm rmi IMAGE} users
    image_json "$image" >/dev/null
    users=$(image_users "$image" | tr '\n' ' ')
    [ -z "$users" ] || die "image '$image' is used by: $users"
    rm -f "$IMAGES_DIR/$image.ext4" "$IMAGES_DIR/$image.json"
    log "removed image '$image'"
}

# --- fcvm cp: files in and out of a running VM (tar over the exec agent) -------
# Needs sh and tar in the guest (busybox counts). Like docker cp: if DST is an
# existing directory, SRC is copied into it; otherwise SRC is copied as DST.

cp_cmd() {
    local usage="usage: fcvm cp [-L] HOST_PATH VM:PATH | fcvm cp [-L] VM:PATH HOST_PATH  (-L: follow symlinks in SRC)"
    local follow=()
    if [ "${1:-}" = -L ]; then follow=(-h); shift; fi
    local src=${1:?$usage} dst=${2:?$usage} vm path base parent newbase xform
    if [[ $dst == *:* && $src != *:* ]]; then          # host -> VM
        vm=${dst%%:*} path=${dst#*:}
        [ -e "$src" ] || die "no such file: $src"
        vm_running "$vm" || die "VM '$vm' is not running"
        src=$(realpath -s "$src"); base=$(basename "$src"); parent=$(dirname "$src")
        if exec_vm "$vm" test -d "$path" 2>/dev/null; then
            xform=() newbase=$base; dst=$path
        else
            newbase=$(basename "$path"); dst=$(dirname "$path")
            xform=(--transform "s|^$(sed 's/[][\.*^$|]/\\&/g' <<<"$base")\(/\|\$\)|$newbase\1|S")
        fi
        tar -C "$parent" --owner=0 --group=0 --numeric-owner "${follow[@]}" "${xform[@]}" -cf - "$base" |
            exec_vm -i "$vm" sh -c 'mkdir -p "$1" && tar -xf - -C "$1"' sh "$dst" ||
            die "copy failed (the image needs sh and tar)"
    elif [[ $src == *:* && $dst != *:* ]]; then        # VM -> host
        vm=${src%%:*} path=${src#*:}
        vm_running "$vm" || die "VM '$vm' is not running"
        path=${path%/}; base=$(basename "$path"); parent=$(dirname "$path")
        if [ -d "$dst" ]; then
            xform=()
        else
            newbase=$(basename "$dst"); dst=$(dirname "$dst")
            xform=(--transform "s|^$(sed 's/[][\.*^$|]/\\&/g' <<<"$base")\(/\|\$\)|$newbase\1|S")
        fi
        exec_vm "$vm" sh -c 'cd "$1" && tar $3 -cf - "$2"' sh "$parent" "$base" "${follow[*]}" |
            tar -xf - -C "$dst" --no-same-owner "${xform[@]}" ||
            die "copy failed (does $path exist? the image needs sh and tar)"
    else
        die "$usage"
    fi
}

# --- snapshots and fork (Firecracker memory snapshots) ---------------------------

# Firecracker API call; prints Firecracker's error message on failure.
fc_api() {   # socket method path [json]
    local out code
    out=$(curl -sS --unix-socket "$1" -X "$2" "http://localhost$3" -H 'Content-Type: application/json' \
        ${4:+-d "$4"} -w '\n%{http_code}') || return 1
    code=${out##*$'\n'}
    [[ $code == 2* ]] || { echo "${out%$'\n'*}" >&2; return 1; }
}

# fcvm snapshot VM NAME: pause a running VM, save its memory, device state and
# writable disk (consistent: taken while paused), then resume it.
snapshot_create() {
    local vm=${1:?usage: fcvm snapshot VM NAME} name=${2:?usage: fcvm snapshot VM NAME}
    lock_vm "$vm"
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" || die "VM '$vm' is not running (snapshots capture a running VM's memory)"
    [[ $name =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die "invalid snapshot name '$name'"
    local dir=$SNAPSHOTS_DIR/$name vdir disk drive t0 ms
    [ ! -e "$dir" ] || die "snapshot '$name' already exists"
    vdir=$(vm_dir "$vm")
    if jq -e '[.volumes[]? | select(endswith(":ro") | not)] | length > 0' "$vdir/vm.json" >/dev/null; then
        die "VM '$vm' has read-write volumes; a fork can't share them (use :ro volumes, or copy the data in)"
    fi
    [ "$(jq '.shares // [] | length' "$vdir/vm.json")" = 0 ] ||
        die "VM '$vm' has live host directories mounted; their connections can't be cloned into a fork"
    local jailed=false vmstate=$dir/vmstate memf=$dir/mem
    if vm_jailed "$vm"; then   # Firecracker writes inside its chroot; the helper moves the files out
        jailed=true vmstate=/snap.vmstate memf=/snap.mem
    fi
    if [ -f "$vdir/disk.ext4" ]; then disk=disk.ext4 drive=root; else disk=rw.ext4 drive=rw; fi
    mkdir -p "$dir"
    t0=$(date +%s%N)
    fc_api "$vdir/fc.sock" PATCH /vm '{"state": "Paused"}' || { rm -rf "$dir"; die "cannot pause '$vm'"; }
    if ! fc_api "$vdir/fc.sock" PUT /snapshot/create "$(jq -n --arg s "$vmstate" --arg m "$memf" \
            '{snapshot_type: "Full", snapshot_path: $s, mem_file_path: $m, sync_snapshot_files: false}')" ||
       ! { [ $jailed = false ] || jaild_quiet snapshot_collect "$vm" "name=$name"; } ||
       ! cp --sparse=always "$vdir/$disk" "$dir/$disk"; then
        fc_api "$vdir/fc.sock" PATCH /vm '{"state": "Resumed"}' || true
        rm -rf "$dir"; die "snapshot failed; '$vm' resumed"
    fi
    fc_api "$vdir/fc.sock" PATCH /vm '{"state": "Resumed"}' || warn "could not resume '$vm'"
    ms=$(( ($(date +%s%N) - t0) / 1000000 ))
    fallocate -d "$dir/mem" 2>/dev/null || true   # untouched guest pages take no disk
    cp "$vdir/vm.json" "$dir/vm.json"
    jq -n --arg src "$vm" --arg disk "$disk" --arg drive "$drive" --argjson ms "$ms" \
        --arg fc "$("$FIRECRACKER" --version | head -1)" --arg srcdisk "$vdir/$disk" \
        --argjson jail "$jailed" \
        '{source: $src, disk: $disk, drive_id: $drive, source_disk: $srcdisk, paused_ms: $ms,
          firecracker: $fc, jail: $jail, created: (now | todate)}' > "$dir/meta.json"
    log "snapshot '$name' of '$vm' (paused ${ms} ms; memory $(du -h "$dir/mem" | cut -f1) on disk). Fork it: fcvm fork $name"
}

# Boot VM $2 from snapshot $1: its own copy of the disk, a free tap, its own
# vsock socket, then a new address, MAC and hostname via the agent.
fork_one() {
    local snap=$1 vm=$2 sdir=$SNAPSHOTS_DIR/$1 dir disk drive mode mac="" gw="" tap="" t0 i pid
    dir=$(vm_dir "$vm")
    sock_room "$vm"
    lock_vm "$vm"
    vm_exists "$vm" && die "VM '$vm' already exists"
    if [ "$(jq -r '.jail // false' "$sdir/meta.json")" = true ]; then
        # Inside a jail every path is the same (/driveN.ext4, /vsock.sock, tap0 in
        # its own netns), so a fresh jail with this VM's own disk restores as is.
        t0=$(date +%s%N)
        disk=$(jq -r .disk "$sdir/meta.json")
        mkdir -p "$dir"
        jq --arg snap "$snap" '. + {ephemeral: false, ports: [], restored_from: $snap, created: (now | todate)}' \
            "$sdir/vm.json" > "$dir/vm.json"
        cp --sparse=always "$sdir/$disk" "$dir/$disk"
        RESTORE=$snap QUIET_START=1 start "$vm"
        log "forked '$vm' from '$snap' in $(( ($(date +%s%N) - t0) / 1000000 )) ms (jailed$([ -f "$dir/ip" ] && echo ", ip $(cat "$dir/ip")"))"
        return
    fi
    disk=$(jq -r .disk "$sdir/meta.json"); drive=$(jq -r .drive_id "$sdir/meta.json")
    t0=$(date +%s%N)
    mkdir -p "$dir"
    jq --arg snap "$snap" '. + {ephemeral: false, ports: [], restored_from: $snap, created: (now | todate)}' \
        "$sdir/vm.json" > "$dir/vm.json"
    cp --sparse=always "$sdir/$disk" "$dir/$disk"
    DIR=$dir IP=""
    mode=$(jq -r '.net.mode // "full"' "$dir/vm.json")
    case $mode in
        restricted)
            claim_tap fcrtap || { rm -rf "$dir"; die "no free restricted tap; run: ./fcvm net-up"; }
            IP=$NET_R_PREFIX.$((10 + TAP_INDEX)) gw=$NET_R_PREFIX.1 tap=fcrtap$TAP_INDEX
            mac=$(printf '06:01:%02x:%02x:%02x:%02x' ${IP//./ })
            write_policy "$vm" "$IP"; ensure_proxy ;;
        none) ;;
        *)
            claim_tap fctap || { rm -rf "$dir"; die "no free tap; run: ./fcvm net-up"; }
            IP=$NET_PREFIX.$((10 + TAP_INDEX)) gw=$NET_PREFIX.1 tap=fctap$TAP_INDEX
            mac=$(printf '06:00:%02x:%02x:%02x:%02x' ${IP//./ }) ;;
    esac
    [ -z "$IP" ] || echo "$IP" > "$dir/ip"
    : > "$dir/firecracker.log"; : > "$dir/console.log"
    setsid python3 "$FCVM_ROOT/lib/console.py" serve --sock "$dir/console.sock" --log "$dir/console.log" \
        --pidfile "$dir/pid" --on-exit "$(printf '%q _reap %q' "$FCVM_ROOT/fcvm" "$vm")" -- \
        "$FIRECRACKER" --api-sock "$dir/fc.sock" --log-path "$dir/firecracker.log" --level Warning \
        </dev/null >"$dir/relay.log" 2>&1 {VM_LOCK}>&- &
    for ((i = 0; i < 100; i++)); do [ -S "$dir/fc.sock" ] && break; sleep 0.01; done
    pid=$(vm_pid "$vm")
    [ -z "$pid" ] || record_pid_id "$vm" "$pid"
    # The snapshot records the source VM's disk path, and loading reopens it
    # before we re-point the drive at the fork's copy. If the source VM is gone,
    # stand in for that path with the snapshot's own disk while loading.
    local srcdisk stand_in=0 made_dir=0 lock
    srcdisk=$(jq -r --arg d "$VMS_DIR/$(jq -r .source "$sdir/meta.json")/$disk" '.source_disk // $d' "$sdir/meta.json")
    mkdir -p "$VMS_DIR/.locks"
    exec {lock}>"$VMS_DIR/.locks/snapshot-load"; flock "$lock"
    if [ ! -e "$srcdisk" ]; then
        [ -d "${srcdisk%/*}" ] || { mkdir -p "${srcdisk%/*}"; made_dir=1; }
        ln -s "$sdir/$disk" "$srcdisk"; stand_in=1
    fi
    local load
    load=$(jq -n --arg s "$sdir/vmstate" --arg m "$sdir/mem" --arg tap "$tap" --arg vsock "$dir/vsock.sock" '
        {snapshot_path: $s, mem_backend: {backend_type: "File", backend_path: $m},
         vsock_override: {uds_path: $vsock}, clock_realtime: true, resume_vm: false}
        + (if $tap != "" then {network_overrides: [{iface_id: "eth0", host_dev_name: $tap}]} else {} end)')
    if ! fc_api "$dir/fc.sock" PUT /snapshot/load "$load" ||
       ! fc_api "$dir/fc.sock" PATCH "/drives/$drive" "$(jq -n --arg id "$drive" --arg p "$dir/$disk" '{drive_id: $id, path_on_host: $p}')" ||
       ! fc_api "$dir/fc.sock" PATCH /vm '{"state": "Resumed"}'; then
        if [ $stand_in = 1 ]; then rm -f "$srcdisk"; fi
        if [ $made_dir = 1 ]; then rmdir "${srcdisk%/*}" 2>/dev/null || true; fi
        if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
        sleep 0.2; rm -rf "$dir"
        die "restore failed (snapshot taken with $(jq -r .firecracker "$sdir/meta.json"); now $("$FIRECRACKER" --version | head -1))"
    fi
    if [ $stand_in = 1 ]; then rm -f "$srcdisk"; fi
    if [ $made_dir = 1 ]; then rmdir "${srcdisk%/*}" 2>/dev/null || true; fi
    exec {lock}>&-
    local ms=$(( ($(date +%s%N) - t0) / 1000000 ))
    python3 "$FCVM_ROOT/lib/exec_client.py" --netconf "${IP:+$IP/24},$gw,$mac,$vm" "$dir/vsock.sock" ||
        warn "'$vm' could not be re-addressed (was the source VM started with an older fcvm?)"
    log "forked '$vm' from '$snap' in ${ms} ms${IP:+ (ip $IP)}"
}

fork_cmd() {
    local usage="usage: fcvm fork SNAPSHOT [NAME] [-n COUNT]" snap="" name="" count=1
    while [ $# -gt 0 ]; do
        case $1 in
            -n|--count) [[ ${2:-} =~ ^[1-9][0-9]*$ ]] || die "$usage"; count=$2; shift 2 ;;
            -*)         die "unknown option $1 ($usage)" ;;
            *)          if [ -z "$snap" ]; then snap=$1; else name=$1; fi; shift ;;
        esac
    done
    [ -n "$snap" ] || die "$usage"
    [ -f "$SNAPSHOTS_DIR/$snap/meta.json" ] || die "no snapshot '$snap' (see: fcvm snapshot ls)"
    [ "$(jq '.ports | length' "$SNAPSHOTS_DIR/$snap/vm.json")" = 0 ] ||
        warn "published ports aren't carried over to forks (they'd conflict with the source)"
    local base=${name:-$snap-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')} i
    if [ "$count" = 1 ]; then
        fork_one "$snap" "$base"
    else
        for ((i = 1; i <= count; i++)); do fork_one "$snap" "$base-$i"; done
    fi
}

snapshot_cmd() {
    case ${1:-} in
        ls)
            local json=0 d
            [ "${2:-}" = --json ] && json=1
            for d in "$SNAPSHOTS_DIR"/*/; do
                [ -f "$d/meta.json" ] || continue
                jq --arg name "$(basename "$d")" --argjson mem "$(( $(stat -c %b "$d/mem") * 512 ))" \
                   --argjson disk "$(( $(stat -c %b "$d"/*.ext4 | head -1) * 512 ))" --slurpfile vm "$d/vm.json" \
                   '{name: $name, source, image: $vm[0].image, type: $vm[0].type, mem_mib: $vm[0].mem_mib,
                     mem_bytes_on_disk: $mem, disk_bytes: $disk, paused_ms, created}' "$d/meta.json"
            done | if [ $json = 1 ]; then jq -s .; else
                jq -rs '(["NAME","SOURCE","IMAGE","MEM","DISK","CREATED"] | @tsv),
                    (.[] | [.name, .source, .image, "\(.mem_bytes_on_disk / 1048576 | floor)M/\(.mem_mib)M",
                            "\(.disk_bytes / 1048576 | floor)M", .created] | @tsv)' | column -t -s $'\t'; fi ;;
        rm)
            local name=${2:?usage: fcvm snapshot rm NAME}
            [ -d "$SNAPSHOTS_DIR/$name" ] || die "no snapshot '$name'"
            rm -rf "${SNAPSHOTS_DIR:?}/$name"; log "removed snapshot '$name'" ;;
        ""|-h|--help) die "usage: fcvm snapshot VM NAME | fcvm snapshot ls [--json] | fcvm snapshot rm NAME" ;;
        *) snapshot_create "$@" ;;
    esac
}

# --- live host directories: fcvm mount / umount ---------------------------------------

# Where Firecracker looks for <vsock uds>_<port> listeners: the VM dir, or the
# jail's chroot (vms/<vm>/vsock.sock is then a link into it).
vsock_prefix() {
    if [ -L "$1/vsock.sock" ]; then echo "$(dirname "$(readlink "$1/vsock.sock")")/vsock.sock"; else echo "$1/vsock.sock"; fi
}

# fcvm mount VM /host/dir:/path[:ro]: add a host directory to a VM. On a running
# VM it's mounted live (a new share9p server + the agent's mount op); on a
# stopped one it's mounted at the next start.
mount_cmd() {
    local usage="usage: fcvm mount VM /HOST/DIR:/GUEST/PATH[:ro]"
    local vm=${1:?$usage} spec=${2:?$usage} dir hdir gpath ro port pid i
    vm_exists "$vm" || die "no VM '$vm'"
    [[ $spec =~ ^([^:]+):(/[^:,]*)(:ro)?$ ]] || die "$usage"
    hdir=${BASH_REMATCH[1]/#\~/$HOME} gpath=${BASH_REMATCH[2]} ro=${BASH_REMATCH[3]:+true}
    [ -d "$hdir" ] || die "not a directory: $hdir"
    hdir=$(realpath "$hdir")
    dir=$(vm_dir "$vm")
    jq -e --arg g "$gpath" '(.shares // []) | any(.path == $g)' "$dir/vm.json" >/dev/null &&
        die "'$vm' already has something mounted at $gpath (fcvm umount $vm $gpath first)"
    if vm_running "$vm"; then
        local uds; uds=$(vsock_prefix "$dir")
        for ((port = 10000; port < 10100; port++)); do [ -e "${uds}_$port" ] || break; done
        (
            if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
            exec setsid python3 "$FCVM_ROOT/lib/share9p.py" --uds-prefix "$uds" --pidfile "$dir/pid" \
                "$port=$hdir${ro:+:ro}" </dev/null >>"$dir/share.log" 2>&1
        ) &
        pid=$!
        for ((i = 0; i < 50; i++)); do [ -S "${uds}_$port" ] && break; sleep 0.02; done
        if ! python3 "$FCVM_ROOT/lib/exec_client.py" --fileop "$dir/vsock.sock" -- mount "$port" "$gpath" "${ro:+ro}${ro:-rw}"; then
            kill "$pid" 2>/dev/null || true; rm -f "${uds}_$port"
            die "could not mount $gpath in '$vm' (is it running a current fcvm initramfs?)"
        fi
        echo "$port $pid $gpath" >> "$dir/share.live"
    fi
    jq --arg h "$hdir" --arg g "$gpath" --argjson ro "${ro:-false}" '.shares = ((.shares // []) + [{host: $h, path: $g, ro: $ro}])' \
        "$dir/vm.json" > "$dir/vm.json.tmp" && mv "$dir/vm.json.tmp" "$dir/vm.json"
    log "mounted $hdir at $gpath in '$vm'$(vm_running "$vm" && echo " (live)" || echo " (from its next start)")"
}

# fcvm umount VM /path
umount_cmd() {
    local vm=${1:?usage: fcvm umount VM /GUEST/PATH} gpath=${2:?usage: fcvm umount VM /GUEST/PATH} dir line
    vm_exists "$vm" || die "no VM '$vm'"
    dir=$(vm_dir "$vm")
    jq -e --arg g "$gpath" '(.shares // []) | any(.path == $g)' "$dir/vm.json" >/dev/null ||
        die "nothing from the host is mounted at $gpath in '$vm'"
    if vm_running "$vm"; then
        python3 "$FCVM_ROOT/lib/exec_client.py" --fileop "$dir/vsock.sock" -- umount "$gpath" ||
            warn "the guest could not unmount $gpath"
        if [ -f "$dir/share.live" ]; then   # a live mount has its own server; boot-time ones share one
            while read -r line; do
                set -- $line
                if [ "$3" = "$gpath" ]; then kill "$2" 2>/dev/null || true; rm -f "$(vsock_prefix "$dir")_$1"; fi
            done < "$dir/share.live"
            awk -v g="$gpath" '$3 != g' "$dir/share.live" > "$dir/share.live.tmp"; mv "$dir/share.live.tmp" "$dir/share.live"
        fi
    fi
    jq --arg g "$gpath" '.shares = [(.shares // [])[] | select(.path != $g)]' "$dir/vm.json" > "$dir/vm.json.tmp" &&
        mv "$dir/vm.json.tmp" "$dir/vm.json"
    log "unmounted $gpath from '$vm'"
}

# --- machine-readable state ------------------------------------------------------

# Host memory a running VM actually uses: its Firecracker process's resident
# set (guest RAM is only backed once the guest touches it), in bytes.
vm_rss() {
    awk '/^VmRSS:/ {print $2 * 1024}' "/proc/$1/status" 2>/dev/null || true
}

inspect_json() {
    local vm=$1 d state=stopped pid="" ip="" code=""
    d=$(vm_dir "$vm")
    if vm_running "$vm"; then
        state=running pid=$(vm_pid "$vm") ip=$(cat "$d/ip" 2>/dev/null || true)
    elif [ "$(jq -r .type "$d/vm.json")" = app ]; then
        code=$(exit_code "$vm"); [ -z "$code" ] || state=exited
    fi
    local rss=""; [ -z "$pid" ] || rss=$(vm_rss "$pid")
    jq --arg name "$vm" --arg state "$state" --arg pid "$pid" --arg ip "$ip" --arg code "$code" --arg rss "$rss" \
        --argjson disk "$(( $(stat -c %b "$d"/*.ext4 | head -1) * 512 ))" \
        --arg mode "$([ -f "$d/disk.ext4" ] && echo copy || echo overlay)" \
        --argjson chain "$(jq -n '$ARGS.positional' --args $(image_chain "$(jq -r .image "$d/vm.json")"))" \
        '{name: $name, state: $state, pid: ($pid | tonumber? // null), ip: ($ip | select(. != "") // null),
          mem_used_bytes: ($rss | tonumber? // null),
          exit_code: ($code | tonumber? // null), image_chain: $chain, disk_mode: $mode, disk_used_bytes: $disk,
          stopped_by_user: $stopped, last_exit: $last} + {restart: "no"} + .' \
        --argjson stopped "$([ -f "$d/stopped" ] && echo true || echo false)" \
        --argjson last "$(cat "$d/last-exit" 2>/dev/null || echo null)" "$d/vm.json"
}

inspect() {
    local vm=${1:?usage: fcvm inspect VM}
    vm_exists "$vm" || die "no VM '$vm'"
    inspect_json "$vm"
}

# fcvm egress VM [--allow HOST,...] [--deny HOST,...] [-n N | -f]: a restricted
# VM's allowlist and its allow/deny log. Changes apply live to a running VM.
egress() {
    local usage="usage: fcvm egress VM [--allow HOST,...] [--deny HOST,...] [-n LINES | -f]"
    local vm="" add=() del=() lines=20 follow=0 _a
    while [ $# -gt 0 ]; do
        case $1 in
            --allow) IFS=, read -ra _a <<<"${2:?$usage}"; add+=("${_a[@]}"); shift 2 ;;
            --deny)  IFS=, read -ra _a <<<"${2:?$usage}"; del+=("${_a[@]}"); shift 2 ;;
            -n)      lines=${2:?$usage}; shift 2 ;;
            -f)      follow=1; shift ;;
            -*)      die "unknown option $1 ($usage)" ;;
            *)       vm=$1; shift ;;
        esac
    done
    [ -n "$vm" ] || die "$usage"
    vm_exists "$vm" || die "no VM '$vm'"
    local dir; dir=$(vm_dir "$vm")
    local mode; mode=$(jq -r '.net.mode // "full"' "$dir/vm.json")
    if [ ${#add[@]} -gt 0 ] || [ ${#del[@]} -gt 0 ]; then
        [ "$mode" = restricted ] || die "VM '$vm' has network '$mode'; allowlists apply to VMs created with --allow"
        [ ${#add[@]} -eq 0 ] || expand_allow "${add[@]}" >/dev/null
        jq --argjson add "$(jq -n '$ARGS.positional' --args "${add[@]}")" \
           --argjson del "$(jq -n '$ARGS.positional' --args "${del[@]}")" \
           '.net.allow = ((.net.allow + $add | unique) - $del)' "$dir/vm.json" > "$dir/vm.json.tmp" &&
            mv "$dir/vm.json.tmp" "$dir/vm.json"
        if vm_running "$vm" && [ -f "$dir/ip" ]; then write_policy "$vm" "$(cat "$dir/ip")"; fi
    fi
    local fmt='"\(.ts)  \(.decision | ascii_upcase | .[0:5])  \(.method) \(.host):\(.port)"'
    if [ $follow = 1 ]; then
        touch "$dir/egress.log"; tail -n "$lines" -f "$dir/egress.log" | jq --unbuffered -r "$fmt"
        return
    fi
    echo "network: $mode"
    [ "$mode" != restricted ] || echo "allow:   $(jq -r '.net.allow | join(" ")' "$dir/vm.json")"
    if [ -s "$dir/egress.log" ]; then
        echo "recent requests:"; tail -n "$lines" "$dir/egress.log" | jq -r "$fmt" | sed 's/^/  /'
    fi
}

list_vms() {
    local d all=0 json=0 a
    for a; do case $a in --all|-a) all=1 ;; --json) json=1 ;; esac; done
    if [ $json = 1 ]; then
        for d in "$VMS_DIR"/*/; do
            [ -f "$d/vm.json" ] || continue
            [ $all = 1 ] || [[ $(basename "$d") != _* ]] || continue
            inspect_json "$(basename "$d")"
        done | jq -s .
        return
    fi
    local fmt='%-20s %-11s %-14s %-18s %-12s %-8s %s\n'
    printf "$fmt" NAME STATE IP IMAGE "MEM" DISK "NETWORK/PORTS/VOLUMES"
    local vm state ip code used mem alloc rss nrun=0 tot_alloc=0 tot_rss=0
    for d in "$VMS_DIR"/*/; do
        [ -f "$d/vm.json" ] || continue
        vm=$(basename "$d")
        [ $all = 1 ] || [[ $vm != _* ]] || continue   # internal (fcvm build)
        state=stopped ip=-
        alloc=$(jq -r .mem_mib "$d/vm.json"); mem="${alloc}M"
        if vm_running "$vm"; then
            state=running; ip=$(cat "$d/ip" 2>/dev/null || echo -)
            rss=$(( $(vm_rss "$(vm_pid "$vm")") / 1048576 ))
            mem="${rss}M/${alloc}M"   # used / allocated
            nrun=$((nrun + 1)); tot_alloc=$((tot_alloc + alloc)); tot_rss=$((tot_rss + rss))
        elif [ "$(jq -r .type "$d/vm.json")" = app ]; then
            code=$(exit_code "$vm"); [ -z "$code" ] || state="exited($code)"
        fi
        used=$(du -h "$d"/*.ext4 2>/dev/null | awk '{print $1; exit}')
        [ -f "$d/disk.ext4" ] && used+=" copy"
        printf "$fmt" "$vm" "$state" "$ip" "$(jq -r .image "$d/vm.json")" "$mem" "$used" \
            "$(jq -r '[(if .jail then "jail" else empty end), (if (.restart // "no") != "no" then "restart:\(.restart)" else empty end), (if .net.mode == "none" then "net:none" elif .net.mode == "restricted" then "allow:\(.net.allow | join(","))" else empty end)]
                + (.ports // []) + ((.volumes // []) | map("-v " + .))
                + ((.shares // []) | map("-v \(.host):\(.path)\(if .ro then ":ro" else "" end)")) | join(" ")' "$d/vm.json")"
    done
    if [ $nrun -gt 0 ]; then
        printf '\n%d running: %dM allocated, %dM used on the host (MEM = used/allocated)\n' "$nrun" "$tot_alloc" "$tot_rss"
    fi
}

list_images() {
    local j name all=0 json=0 a
    for a; do case $a in --all|-a) all=1 ;; --json) json=1 ;; esac; done
    if [ $json = 1 ]; then
        for j in "$IMAGES_DIR"/*.json; do
            [ -f "$j" ] || continue
            name=$(basename "$j" .json)
            [ $all = 1 ] || [[ $name != _* ]] || continue
            jq --arg name "$name" --argjson used "$(( $(stat -c %b "${j%.json}.ext4") * 512 ))" \
                --argjson users "$(jq -n '$ARGS.positional' --args $(image_users "$name"))" \
                '{name: $name, type: (.type // "app"), parent: (.parent // null), ref: (.ref // null),
                  disk_used_bytes: $used, used_by: $users} + (. | {argv, env, workdir, user, exposed_ports} | with_entries(select(.value != null)))' "$j"
        done | jq -s .
        return
    fi
    printf '%-28s %-10s %-8s %-8s %s\n' NAME TYPE SIZE USED-BY SOURCE
    for j in "$IMAGES_DIR"/*.json; do
        [ -f "$j" ] || continue
        name=$(basename "$j" .json)
        [ $all = 1 ] || [[ $name != _* ]] || continue   # build cache (fcvm build)
        printf '%-28s %-10s %-8s %-8s %s\n' "$name" "$(jq -r '.type // "app"' "$j")" \
            "$(du -h "${j%.json}.ext4" | cut -f1)" "$(image_users "$name" | wc -l)" \
            "$(jq -r 'if .parent then "layer on \(.parent)" else (.ref // "-") end' "$j")"
    done
}

# fcvm update VM --restart POLICY: change settings of an existing VM.
update() {
    local usage="usage: fcvm update VM --restart no|on-failure|unless-stopped|always" vm=${1:-} restart=""
    [ -n "$vm" ] || die "$usage"; shift
    while [ $# -gt 0 ]; do
        case $1 in
            --restart) valid_restart "${2:-}"; restart=$2; shift 2 ;;
            *)         die "unknown option $1 ($usage)" ;;
        esac
    done
    [ -n "$restart" ] || die "$usage"
    lock_vm "$vm"
    vm_exists "$vm" || die "no VM '$vm'"
    local f=$VMS_DIR/$vm/vm.json
    [ "$restart" = no ] || [ "$(jq -r .ephemeral "$f")" != true ] || die "'$vm' is a throwaway VM (fcvm run); restart policies don't apply"
    jq --arg r "$restart" '.restart = $r' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    log "'$vm': restart policy $restart$([ "$restart" = no ] || echo " (applied by fcvm serve / the fcvm service)")"
}

# fcvm _shutdown [--force]: run by fcvm.service when it stops. Only while the
# host is shutting down (or with --force), stop every running VM cleanly, in
# parallel, and mark it to resume at the next boot. Restarting the service
# alone leaves VMs running.
shutdown_all() {
    [ "${1:-}" = --force ] || [ "$(systemctl is-system-running 2>/dev/null)" = stopping ] || return 0
    local d vm n=0
    for d in "$VMS_DIR"/*/; do
        vm=$(basename "$d")
        vm_exists "$vm" && vm_running "$vm" || continue
        touch "$d/resume"
        FCVM_SYSTEM_STOP=1 QUIET_STOP=1 "$FCVM_ROOT/fcvm" stop "$vm" >/dev/null 2>&1 &
        n=$((n + 1))
    done
    wait
    log "stopped $n VM(s) for shutdown; they resume at the next boot"
}

rm_vm() {
    local vm=${1:?usage: fcvm rm VM}
    lock_vm "$vm"
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" && die "VM '$vm' is running (fcvm stop $vm first)"
    rm -rf "$(vm_dir "$vm")" "$VMS_DIR/.locks/vm-$vm"
    log "removed '$vm'"
}

case $cmd in
    create)  create "$@" ;;
    start)   start "$@" ;;
    run)     run "$@" ;;
    stop)    stop "$@" ;;
    console) console "$@" ;;
    logs)    logs "$@" ;;
    _reap)   reap "$@" ;;
    ssh)     ssh_vm "$@" ;;
    exec)    exec_vm "$@" ;;
    shell)   shell_vm "$@" ;;
    ls)      list_vms "$@" ;;
    images)  list_images "$@" ;;
    inspect) inspect "$@" ;;
    commit)  commit "$@" ;;
    rmi)     rmi "$@" ;;
    squash)  squash "$@" ;;
    prune)   prune ;;
    cp)      cp_cmd "$@" ;;
    volume)  volume_cmd "$@" ;;
    egress)  egress "$@" ;;
    snapshot) snapshot_cmd "$@" ;;
    fork)    fork_cmd "$@" ;;
    mount)   mount_cmd "$@" ;;
    umount)  umount_cmd "$@" ;;
    rm)      rm_vm "$@" ;;
    update)  update "$@" ;;
    _shutdown) shutdown_all "$@" ;;
esac
