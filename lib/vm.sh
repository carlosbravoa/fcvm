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
vm_running() { local p; p=$(vm_pid "$1"); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
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
    local usage="usage: fcvm create VM IMAGE [--vcpus N] [--mem MiB] [--disk SIZE] [--copy] [-p [BIND:]HOST:GUEST]... [-- CMD...]"
    local vm=${1:?$usage} image=${2:?$usage}
    shift 2
    local vcpus=$VM_VCPUS mem=$VM_MEM_MIB disk="" copy=0 ports=() argv=()
    while [ $# -gt 0 ]; do
        case $1 in
            --vcpus)        vcpus=$2; shift 2 ;;
            --mem)          mem=$2; shift 2 ;;
            --disk)         disk=$2; shift 2 ;;
            --copy)         copy=1; shift ;;
            -p|--publish)   ports+=("$2"); shift 2 ;;
            --)             shift; argv=("$@"); break ;;
            *)              die "unknown option $1 ($usage)" ;;
        esac
    done
    [[ $vm =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "invalid VM name '$vm'"
    vm_exists "$vm" && die "VM '$vm' already exists"
    local meta type
    meta=$(image_json "$image")
    type=$(jq -r '.type // "container"' "$meta")
    [ ${#argv[@]} -eq 0 ] || [ "$type" = container ] || die "-- CMD only applies to container images"
    local p
    for p in "${ports[@]}"; do
        [[ $p =~ ^(([0-9.]+):)?[0-9]+:[0-9]+(/tcp)?$ ]] || die "bad port spec '$p' (want [BIND:]HOSTPORT:GUESTPORT, TCP only)"
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
        --argjson ephemeral "${EPHEMERAL:-false}" --args \
        '{image: $image, type: $type, vcpus: $vcpus, mem_mib: $mem, ports: $ARGS.positional, ephemeral: $ephemeral}' \
        "${ports[@]}" > "$dir/vm.json"
    [ -n "${EPHEMERAL:-}" ] || log "created VM '$vm' from $image ($([ $copy = 1 ] && echo 'private copy' || echo 'shared image + writable layer'))"
}

# Claim a free tap: the flock is inherited by firecracker and held for its lifetime.
claim_tap() {
    mkdir -p "$VMS_DIR/.locks"
    local i
    for ((i = 0; i < NET_TAPS; i++)); do
        [ -e "/sys/class/net/fctap$i" ] || continue
        exec {TAP_FD}>"$VMS_DIR/.locks/fctap$i"
        if flock -n "$TAP_FD"; then TAP_INDEX=$i; return 0; fi
        exec {TAP_FD}>&-
    done
    return 1
}

# Publish ports for the VM whose firecracker runs as pid $1 (see portfwd.py).
start_portfwd() {
    [ ${#PORTS[@]} -gt 0 ] || return 0
    if [ -z "$IP" ]; then warn "no network: ports not published"; return 0; fi
    setsid python3 "$FCVM_ROOT/lib/portfwd.py" --watch "$1" --target "$IP" "${PORTS[@]}" \
        </dev/null >"$DIR/portfwd.log" 2>&1 &
    echo $! > "$DIR/portfwd.pid"
    sleep 0.3
    kill -0 $! 2>/dev/null || { cat "$DIR/portfwd.log" >&2; return 1; }
}

start() {
    local vm=${1:?usage: fcvm start VM [-d]} bg=0
    [ "${2:-}" = -d ] && bg=1
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" && die "VM '$vm' is already running (pid $(vm_pid "$vm"))"
    [ -x "$FIRECRACKER" ] || die "firecracker not installed (run: ./fcvm firecracker)"
    local kernel type vcpus mem image
    DIR=$(vm_dir "$vm")
    kernel=$(default_kernel)
    image=$(jq -r .image "$DIR/vm.json")
    type=$(jq -r .type "$DIR/vm.json")
    vcpus=$(jq -r .vcpus "$DIR/vm.json")
    mem=$(jq -r .mem_mib "$DIR/vm.json")
    mapfile -t PORTS < <(jq -r '.ports[]?' "$DIR/vm.json")

    local args="console=ttyS0 reboot=k panic=1" net='[]'
    IP=""
    if claim_tap; then
        IP=$NET_PREFIX.$((10 + TAP_INDEX))
        local mac; mac=$(printf '06:00:%02x:%02x:%02x:%02x' ${IP//./ })
        args+=" ip=$IP::$NET_PREFIX.1:255.255.255.0:$vm:eth0:off:$NET_DNS"
        net=$(jq -n --arg mac "$mac" --arg tap "fctap$TAP_INDEX" '[{iface_id: "eth0", guest_mac: $mac, host_dev_name: $tap}]')
        echo "$IP" > "$DIR/ip"
    else
        warn "no free tap device; starting without network (run: ./fcvm net-up)"
        rm -f "$DIR/ip"
    fi

    local drives
    if [ -f "$DIR/disk.ext4" ]; then
        drives=$(jq -n --arg disk "$DIR/disk.ext4" \
            '[{drive_id: "rootfs", path_on_host: $disk, is_root_device: true, is_read_only: false}]')
        [ "$type" = container ] && args+=" init=/.fcvm/init"
    else
        [ -f "$IMAGES_DIR/$image.ext4" ] || die "image '$image' is gone; VM '$vm' cannot boot"
        drives=$(jq -n --arg base "$IMAGES_DIR/$image.ext4" --arg rw "$DIR/rw.ext4" '[
            {drive_id: "rootfs", path_on_host: $base, is_root_device: true, is_read_only: true},
            {drive_id: "rw", path_on_host: $rw, is_root_device: false, is_read_only: false}]')
        args+=" init=/.fcvm/init fcvm.overlay=/dev/vdb"
        [ "$type" = systemd ] && args+=" fcvm.exec=/sbin/init"
    fi
    if [ "$type" = container ]; then
        args+=" quiet loglevel=1"   # keep the console to the app's output
    else
        args+=" systemd.hostname=$vm"
    fi
    args+=${VM_KERNEL_ARGS:+ $VM_KERNEL_ARGS}

    jq -n --arg kernel "$kernel" --arg args "$args" --argjson drives "$drives" \
        --argjson vcpus "$vcpus" --argjson mem "$mem" --argjson net "$net" --arg vsock "$DIR/vsock.sock" '{
        "boot-source": {kernel_image_path: $kernel, boot_args: $args},
        "drives": $drives,
        "machine-config": {vcpu_count: $vcpus, mem_size_mib: $mem},
        "network-interfaces": $net,
        "vsock": {guest_cid: 3, uds_path: $vsock},
        "entropy": {}
    }' > "$DIR/fc.json"

    rm -f "$DIR/fc.sock" "$DIR/vsock.sock"
    : > "$DIR/firecracker.log"
    local fc=("$FIRECRACKER" --api-sock "$DIR/fc.sock" --config-file "$DIR/fc.json"
              --log-path "$DIR/firecracker.log" --level Warning)
    local info="$type, ${vcpus} vCPU, ${mem} MiB, kernel ${kernel##*/}${IP:+, ip $IP}"
    [ ${#PORTS[@]} -eq 0 ] || info+=", ports ${PORTS[*]}"
    log "starting '$vm' ($info)"
    if [ $bg = 1 ]; then
        setsid "${fc[@]}" </dev/null >"$DIR/console.log" 2>&1 &
        local pid=$!
        echo $pid > "$DIR/pid"
        start_portfwd $pid || { kill $pid; cleanup "$vm"; die "cannot publish ports"; }
        sleep 0.3
        vm_running "$vm" || { cat "$DIR/console.log" "$DIR/firecracker.log" >&2; cleanup "$vm"; die "firecracker failed to start"; }
        log "running in background (pid $pid). Console: ./fcvm console $vm"
    else
        [ "$type" = systemd ] && log "serial console attached; 'poweroff' in the guest or './fcvm stop $vm' to exit"
        local rc=0 code
        ( echo "$BASHPID" > "$DIR/pid"; start_portfwd "$BASHPID" || exit 125; exec "${fc[@]}" ) || rc=$?
        case $rc in
            0)   ;;
            125) warn "cannot publish ports; VM not started" ;;
            *)   warn "firecracker exited with status $rc (see $DIR/firecracker.log)" ;;
        esac
        code=$([ "$type" = container ] && exit_code "$vm" || true)
        cleanup "$vm"
        return "${code:-$rc}"
    fi
}

stop() {
    local vm=${1:?usage: fcvm stop VM} dir pid i
    vm_exists "$vm" || die "no VM '$vm'"
    dir=$(vm_dir "$vm")
    if vm_running "$vm"; then
        pid=$(vm_pid "$vm")
        curl -fsS --unix-socket "$dir/fc.sock" -X PUT http://localhost/actions \
            -H 'Content-Type: application/json' -d '{"action_type": "SendCtrlAltDel"}' >/dev/null || true
        for ((i = 0; i < 100; i++)); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
        if kill -0 "$pid" 2>/dev/null; then warn "guest did not shut down; killing"; kill -9 "$pid"; fi
        log "stopped '$vm'"
    fi
    cleanup "$vm"
}

cleanup() {
    local dir; dir=$(vm_dir "$1")
    if [ -f "$dir/portfwd.pid" ]; then kill "$(cat "$dir/portfwd.pid")" 2>/dev/null || true; fi
    rm -f "$dir/pid" "$dir/fc.sock" "$dir/vsock.sock" "$dir/portfwd.pid"
    if [ "$(jq -r .ephemeral "$dir/vm.json" 2>/dev/null)" = true ]; then rm -rf "$dir"; fi
}

run() {
    local image=${1:?usage: fcvm run IMAGE [-d] [-p [BIND:]HOST:GUEST]... [--vcpus N] [--mem MiB] [--disk SIZE] [-- CMD...]}
    shift
    local bg="" opts=()
    while [ $# -gt 0 ]; do
        case $1 in
            -d) bg=-d; shift ;;
            --) break ;;
            *)  opts+=("$1"); shift ;;
        esac
    done
    local vm; vm=${image%%-*}-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')
    EPHEMERAL=true create "$vm" "$image" "${opts[@]}" "$@"
    start "$vm" $bg
}

console() {
    local f; f=$(vm_dir "${1:?usage: fcvm console VM}")/console.log
    [ -f "$f" ] || die "no console log for '$1' (only background VMs have one)"
    tail -n +1 -f "$f"
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
    local usage="usage: fcvm exec [-i] [-t] [-u USER] VM [CMD...]" flags=()
    while [[ ${1:-} == -* ]]; do
        case $1 in
            -u|--user)     flags+=(-u "${2:?$usage}"); shift 2 ;;
            -i|-t|-it|-ti) [[ $1 == *i* ]] && flags+=(-i); [[ $1 == *t* ]] && flags+=(-t); shift ;;
            *)             die "unknown option $1 ($usage)" ;;
        esac
    done
    local vm=${1:?$usage}; shift
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" || die "VM '$vm' is not running"
    exec python3 "$FCVM_ROOT/lib/exec_client.py" "${flags[@]}" "$(vm_dir "$vm")/vsock.sock" -- "$@"
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

list_vms() {
    local fmt='%-20s %-11s %-14s %-18s %-10s %s\n'
    printf "$fmt" NAME STATE IP IMAGE DISK PORTS
    local d vm state ip code used
    for d in "$VMS_DIR"/*/; do
        [ -f "$d/vm.json" ] || continue
        vm=$(basename "$d")
        state=stopped ip=-
        if vm_running "$vm"; then
            state=running; ip=$(cat "$d/ip" 2>/dev/null || echo -)
        elif [ "$(jq -r .type "$d/vm.json")" = container ]; then
            code=$(exit_code "$vm"); [ -z "$code" ] || state="exited($code)"
        fi
        used=$(du -h "$d"/*.ext4 2>/dev/null | awk '{print $1; exit}')
        [ -f "$d/disk.ext4" ] && used+=" copy"
        printf "$fmt" "$vm" "$state" "$ip" "$(jq -r .image "$d/vm.json")" "$used" \
            "$(jq -r '(.ports // []) | join(" ")' "$d/vm.json")"
    done
}

list_images() {
    printf '%-28s %-10s %-8s %-8s %s\n' NAME TYPE SIZE VMS SOURCE
    local j name
    for j in "$IMAGES_DIR"/*.json; do
        [ -f "$j" ] || continue
        name=$(basename "$j" .json)
        printf '%-28s %-10s %-8s %-8s %s\n' "$name" "$(jq -r '.type // "container"' "$j")" \
            "$(du -h "${j%.json}.ext4" | cut -f1)" "$(image_users "$name" | wc -w)" "$(jq -r '.ref // "-"' "$j")"
    done
}

rm_vm() {
    local vm=${1:?usage: fcvm rm VM}
    vm_exists "$vm" || die "no VM '$vm'"
    vm_running "$vm" && die "VM '$vm' is running (fcvm stop $vm first)"
    rm -rf "$(vm_dir "$vm")"
    log "removed '$vm'"
}

case $cmd in
    create)  create "$@" ;;
    start)   start "$@" ;;
    run)     run "$@" ;;
    stop)    stop "$@" ;;
    console) console "$@" ;;
    ssh)     ssh_vm "$@" ;;
    exec)    exec_vm "$@" ;;
    shell)   shell_vm "$@" ;;
    ls)      list_vms ;;
    images)  list_images ;;
    rm)      rm_vm "$@" ;;
esac
