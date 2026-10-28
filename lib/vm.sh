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
    flock -w 30 "$VM_LOCK" || die_code vm_busy "VM '$1' is busy (another fcvm command is changing it)"
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
        die "the path to VM '$1' is too long for its sockets (${#p} bytes; Linux allows 107): use a name of at most $room characters, or a shorter FCVM_HOME"
    fi
    die "the state directory's path is too long for VM sockets (${#p} bytes; Linux allows 107): set FCVM_HOME to a shorter path (now $FCVM_HOME)"
}

# With the fcvm service running, long-lived VMs (start, run -d, fork) are
# started by its daemon, fcvm serve, through the API. They then live in
# fcvm.service rather than in this terminal's login session: they survive
# logout, and at host shutdown the service stops them cleanly before sessions
# are killed. The daemon marks its own fcvm commands with FCVM_DAEMON.
use_daemon() {   # (the pid test also covers a daemon older than FCVM_DAEMON)
    [ -z "${FCVM_DAEMON:-}" ] && [ -f "$VMS_DIR/.serve.json" ] &&
        [ "$PPID" != "$(jq -r .pid "$VMS_DIR/.serve.json")" ] &&
        systemctl is-active -q fcvm.service 2>/dev/null
}
daemon_api() {   # METHOD PATH [JSON]: prints the reply; 1 = refused (message on stderr), 2 = unreachable,
    local st=$VMS_DIR/.serve.json out code msg   # 3 = a daemon too old to know the operation
    out=$(curl -sS -m 900 -X "$1" -H "Authorization: Bearer $(jq -r .token "$st")" \
        -H 'Content-Type: application/json' ${3:+-d "$3"} -w '\n%{http_code}' \
        "http://127.0.0.1:$(jq -r .port "$st")/api/$2" 2>/dev/null) || return 2
    code=${out##*$'\n'} out=${out%$'\n'*}
    if [[ $code == 2* ]]; then printf '%s' "$out"; return 0; fi
    msg=$(jq -r '.error // .' <<<"$out" 2>/dev/null || echo "$out")
    [[ $msg != "unknown action "* && $msg != "unknown API endpoint" ]] || return 3
    printf '\e[1;31merror:\e[0m %s\n' "$msg" >&2
    [ -t 2 ] || printf 'fcvm-error: %s\n' "$(jq -r '.code // "error"' <<<"$out" 2>/dev/null || echo error)" >&2   # as die_code does
    return 1
}
# After daemon_api returned 3: say the service is behind, and carry on locally.
daemon_too_old() { warn "the fcvm service runs an older fcvm (fcvm service install updates it); $1"; }

# A host directory spec, HOST[:GUEST][:ro]; sets SHARE_HOST (resolved),
# SHARE_GUEST and SHARE_RO. HOST may start with ~. Without GUEST, a directory
# in your home goes to the same place in the guest user's home (~/..., which
# the guest resolves against its own /etc/passwd), and any other directory to
# the same path. GUEST is /PATH, ~ or ~/PATH.
parse_share() {
    local spec=$1 host guest="" ro=false home re='^(/[^:,]+|~|~/[^:,]*)$'
    if [[ $spec == *:ro ]]; then ro=true; spec=${spec%:ro}; fi
    host=${spec%%:*}
    [[ $spec != *:* ]] || guest=${spec#*:}
    host=${host/#\~/$HOME}
    [ -d "$host" ] || die "not a directory: $host"
    host=$(realpath "$host") home=$(realpath "$HOME")
    host_path_ok "$host" "a host directory in a VM"
    if [ -z "$guest" ]; then
        if [ "$host" = "$home" ]; then guest="~"
        elif [[ $host == "$home"/* ]]; then guest="~/${host#"$home"/}"
        else guest=$host; fi
    fi
    [[ $guest =~ $re ]] && [ "$guest" != / ] || die_code invalid "bad guest path '$guest' (want /PATH, ~ or ~/PATH)"
    SHARE_HOST=$host SHARE_GUEST=${guest%/} SHARE_RO=$ro
}
# The guest path a user means by ARG: as given (/PATH, ~/PATH), or for a
# directory in the host home (a shell turns ~/x into /home/you/x) its ~/ form.
guest_arg() {
    local a=${1%/} home; home=$(realpath "$HOME")
    if [ "$a" = "$home" ]; then echo "~"
    elif [[ $a == "$home"/* ]]; then echo "~/${a#"$home"/}"
    else echo "$a"; fi
}

# A JSON list of strings; unlike jq --args, safe for strings that look like options.
json_list() { [ $# -eq 0 ] && echo '[]' || printf '%s\0' "$@" | jq -Rsc 'split("\u0000")[:-1]'; }

# Labels (--label KEY=VALUE): yours to tag VMs with; fcvm ignores them. Keys
# are up to 63 characters of letters, digits, . _ - and /, not starting with
# "fcvm." (reserved for annotations fcvm writes). Values are free text.
label_ok() {
    [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9_./-]{0,62}=.*$ ]] || die_code invalid "bad label '$1' (want KEY=VALUE; KEY: letters, digits, . _ - /)"
    [[ $1 != fcvm.* ]] || die_code invalid "label keys starting with fcvm. are reserved"
}
labels_json() {   # KEY=VALUE... -> {"KEY": "VALUE", ...}
    [ $# -eq 0 ] && echo '{}' || printf '%s\0' "$@" | jq -Rsc 'split("\u0000")[:-1] | map(capture("^(?<k>[^=]*)=(?<v>.*)$") | {key: .k, value: .v}) | from_entries'
}
# Admission (lib/node.py): does the VM fit its tenant's quota and, with
# NODE_ADMIT=1, the host? Fails with quota_exceeded or node_full; otherwise
# the VM counts as allocated (the `starting` marker) until it records a pid.
# `node_admit [--tenant T] --disk SIZE` asks for disk alone (a new layer,
# volume, image or snapshot), before it's taken.
node_admit() { python3 "$FCVM_ROOT/lib/node.py" admit "$@"; }

# Who owns an object: images/NAME.json, snapshots/NAME/meta.json and
# volumes/NAME.json carry a `tenant`; none means public. A tenant's objects
# are its own: own_ok refuses their use by a VM of another tenant, or of
# none (docs/vms.md, Tenants and quotas).
image_tenant()    { jq -r '.tenant // empty' "$IMAGES_DIR/$1.json" 2>/dev/null || true; }
snapshot_tenant() { jq -r '.tenant // empty' "$SNAPSHOTS_DIR/$1/meta.json" 2>/dev/null || true; }
volume_tenant()   { jq -r '.tenant // empty' "$VOLUMES_DIR/$1.json" 2>/dev/null || true; }
vm_tenant()       { jq -r '.labels.tenant // empty' "$VMS_DIR/$1/vm.json" 2>/dev/null || true; }
own_ok() {   # vm-tenant kind name owner
    [ -z "$4" ] || [ "$4" = "$1" ] ||
        die_code policy "$2 '$3' belongs to tenant '$4'$([ -n "$1" ] && echo ", not to '$1'" || echo "; the VM has no tenant (--tenant $4)")"
}
# `tenant` from --label/--tenant arguments already collected as KEY=VALUE.
labels_tenant() { labels_json "$@" | jq -r '.tenant // empty'; }

# fcvm create/run --template NAME [ARGS...]: the template's image and create
# options, then yours (later options win), then the template's process unless
# you give one (-- CMD, --idle or --entrypoint). A missing image is imported
# when the template says from where. Sets TEMPLATE_ARGS=(IMAGE ARGS...).
template_expand() {
    local t=${1:?--template wants a template name} line image ref mine=() cmd=() own=0 topts=() tproc=()
    shift
    line=$(python3 "$FCVM_ROOT/lib/templates.py" image "$t") || exit 1
    image=${line%%$'\t'*} ref=${line#*$'\t'}
    if [ ! -f "$IMAGES_DIR/$image.json" ]; then
        [ -n "$ref" ] || die "template '$t' uses image '$image', which isn't imported (fcvm import ...)"
        log "template '$t': importing $ref as $image"
        "$FCVM_ROOT/lib/import.sh" "$ref" "$image"
    fi
    while [ $# -gt 0 ]; do
        case $1 in
            --)                  cmd=("$@"); own=1; break ;;
            --idle|--entrypoint) own=1 ;;
        esac
        mine+=("$1"); shift
    done
    mapfile -d '' topts < <(python3 "$FCVM_ROOT/lib/templates.py" options "$t")
    # your --net replaces the template's network; your --allow adds to its allowlist
    local o drop=() kept=()
    for o in "${mine[@]}"; do
        case $o in --net) drop+=(--net --allow) ;; --allow) drop+=(--net) ;; esac
    done
    while [ ${#topts[@]} -gt 0 ]; do
        if [[ " ${drop[*]} " == *" ${topts[0]} "* ]]; then topts=("${topts[@]:2}"); continue; fi
        kept+=("${topts[0]}"); topts=("${topts[@]:1}")
    done
    topts=("${kept[@]}")
    [ $own = 1 ] || mapfile -d '' tproc < <(python3 "$FCVM_ROOT/lib/templates.py" process "$t")
    TEMPLATE_ARGS=("$image" "${topts[@]}" "${mine[@]}" "${tproc[@]}" "${cmd[@]}")
}

# VMs created with --no-agent have no exec agent: refuse what needs it, clearly.
need_agent() {   # vm what
    jq -e '.agent == false' "$(vm_dir "$1")/vm.json" >/dev/null 2>&1 &&
        die_code no_agent "'$1' was created with --no-agent: $2 needs the exec agent" || true
}

# create --no-agent: say what goes, and ask (FCVM_NO_AGENT_OK=1 answers yes, for scripts).
confirm_no_agent() {
    cat >&2 <<'EOF2'
[1;33mwarning:[0m --no-agent starts this VM without fcvm's exec agent. You lose:
  - fcvm exec, shell and cp (and the web console's Shell, Files and Processes tabs)
  - live fcvm mount/umount on the running VM (host directories given at create still work)
  - snapshots and forks (forks are re-addressed through the agent)
  - metrics from inside the guest (host-side stats still work)
  - agents: the MCP server works through the agent
You keep: the serial console (fcvm console), logs, published ports, stop, host-side stats.
This can't be changed later: recreate the VM to get the agent back.
EOF2
    [ "${FCVM_NO_AGENT_OK:-}" = 1 ] && return 0
    local answer=""
    { [ -t 0 ] && read -r -p "Continue without the agent? [y/N] " answer; } ||
        die "--no-agent needs confirmation: run it in a terminal (or set FCVM_NO_AGENT_OK=1)"
    [[ $answer =~ ^[Yy]([Ee][Ss])?$ ]] || die "cancelled"
}

RESTART_POLICIES="no on-failure unless-stopped always"
valid_restart() { [[ " $RESTART_POLICIES " == *" $1 "* ]] || die_code invalid "--restart wants one of: $RESTART_POLICIES"; }

# vm.json's lifetime fields from --ttl / --idle-stop values ("" = none).
lifetime_json() {   # ttl idle
    jq -n --arg t "$1" --arg i "$2" \
        '(if $t == "" then {} else {ttl_s: ($t | tonumber)} end) + (if $i == "" then {} else {idle_stop_s: ($i | tonumber)} end)'
}
# Record a sign of life (exec, shell, cp, files...): --idle-stop counts from the last one.
touch_activity() { touch "$VMS_DIR/$1/activity" 2>/dev/null || true; }

# --- secrets (create/update --secret): vms/VM/secrets.json, 0600, handed to the
# guest at boot over vsock into a tmpfs (never on its disk). A spec is
# NAME=VALUE, NAME=@FILE (the file's content) or NAME (from this environment).
SECRETS_PORT=9000
secret_name_ok() { [[ $1 =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die_code invalid "bad secret name '$1' (letters, digits and _)"; }
# Sets SECRET_NAME and SECRET_VALUE from a spec.
parse_secret() {
    local spec=$1
    SECRET_NAME=${spec%%=*}
    secret_name_ok "$SECRET_NAME"
    if [[ $spec != *=* ]]; then
        [ -n "${!SECRET_NAME+x}" ] || die "--secret $SECRET_NAME: not set in the environment (use NAME=VALUE or NAME=@FILE)"
        SECRET_VALUE=${!SECRET_NAME}
    elif [[ ${spec#*=} == @* ]]; then
        local f=${spec#*=@}
        host_path_ok "$f" "--secret NAME=@FILE"
        [ -f "$f" ] || die "--secret $SECRET_NAME: no such file: $f"
        SECRET_VALUE=$(cat "$f"; echo x); SECRET_VALUE=${SECRET_VALUE%x}   # keep a final newline as is
    else
        SECRET_VALUE=${spec#*=}
    fi
}
# Merge specs ($@) into DIR/secrets.json, removing the names in SECRETS_DEL.
write_secrets() {   # dir specs...
    local dir=$1 f=$1/secrets.json cur='{}' n; shift
    [ ! -f "$f" ] || cur=$(cat "$f")
    for n in "${SECRETS_DEL[@]}"; do secret_name_ok "$n"; cur=$(jq --arg n "$n" 'del(.[$n])' <<<"$cur"); done
    local spec
    for spec in "$@"; do
        parse_secret "$spec"
        cur=$(jq --arg n "$SECRET_NAME" --arg v "$SECRET_VALUE" '.[$n] = $v' <<<"$cur")
    done
    if [ "$cur" = '{}' ]; then rm -f "$f"; return; fi
    (umask 077; printf '%s\n' "$cur" > "$f.tmp") && mv "$f.tmp" "$f"
}
secret_names() { jq -r 'keys[]' "$1/secrets.json" 2>/dev/null || true; }

# --forward ssh-agent,git (create/update): what the host relays into the VM
# (lib/forward.py). Prints the checked, deduplicated list, one per line.
parse_forward() {   # LIST[,LIST...] ("none" clears)
    local f out=()
    IFS=, read -ra _f <<<"$1"
    for f in "${_f[@]}"; do
        case $f in
            ssh-agent|git) [[ " ${out[*]:-} " == *" $f "* ]] || out+=("$f") ;;
            none|"")       ;;
            *)             die_code invalid "--forward wants ssh-agent and/or git (comma-separated), or none" ;;
        esac
    done
    printf '%s\n' "${out[@]:-}" | sed '/^$/d'
}

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
image_json() { local f=$IMAGES_DIR/$1.json; [ -f "$f" ] || die_code image_not_found "no image '$1' (see: fcvm images)"; echo "$f"; }

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
    "$FCVM_ROOT/lib/mkfs-tar.sh" "$stage.tar" -q -F -L fcvm-rw -E lazy_itable_init=1,lazy_journal_init=1 -- "$disk"
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

# fcvm ssh's key pair (on the host only; VMs get its public half).
ssh_key() {
    local key=$SSH_DIR/id_ed25519
    [ -f "$key" ] || { mkdir -p "$SSH_DIR"; ssh-keygen -q -t ed25519 -N '' -C fcvm -f "$key"; }
    echo "$key"
}

# SSH identity for a new system VM, written into its disk (DISK, PREFIX "/upper"
# for a writable layer, "" for a --copy disk) instead of living in the image:
#  - /root/.ssh/authorized_keys: fcvm ssh's key and your own public keys;
#  - a host key made here, so fcvm ssh can check it from the first connection.
# Prints the host key's public half (for vm.json).
ssh_identity() {
    local disk=$1 pre=$2 tmp; tmp=$(mktemp -d)
    cat "$(ssh_key).pub" > "$tmp/keys"
    cat "$HOME"/.ssh/id_*.pub >> "$tmp/keys" 2>/dev/null || true
    ssh-keygen -q -t ed25519 -N '' -C fcvm-vm -f "$tmp/host"
    debugfs -w -f - "$disk" >/dev/null 2>&1 <<DEBUGFS
mkdir $pre/root
sif $pre/root mode 040700
sif $pre/root uid 0
sif $pre/root gid 0
mkdir $pre/root/.ssh
sif $pre/root/.ssh mode 040700
sif $pre/root/.ssh uid 0
sif $pre/root/.ssh gid 0
rm $pre/root/.ssh/authorized_keys
write $tmp/keys $pre/root/.ssh/authorized_keys
sif $pre/root/.ssh/authorized_keys mode 0100600
sif $pre/root/.ssh/authorized_keys uid 0
sif $pre/root/.ssh/authorized_keys gid 0
mkdir $pre/etc
sif $pre/etc mode 040755
sif $pre/etc uid 0
sif $pre/etc gid 0
mkdir $pre/etc/ssh
sif $pre/etc/ssh mode 040755
sif $pre/etc/ssh uid 0
sif $pre/etc/ssh gid 0
rm $pre/etc/ssh/ssh_host_ed25519_key
write $tmp/host $pre/etc/ssh/ssh_host_ed25519_key
sif $pre/etc/ssh/ssh_host_ed25519_key mode 0100600
sif $pre/etc/ssh/ssh_host_ed25519_key uid 0
sif $pre/etc/ssh/ssh_host_ed25519_key gid 0
rm $pre/etc/ssh/ssh_host_ed25519_key.pub
write $tmp/host.pub $pre/etc/ssh/ssh_host_ed25519_key.pub
sif $pre/etc/ssh/ssh_host_ed25519_key.pub mode 0100644
sif $pre/etc/ssh/ssh_host_ed25519_key.pub uid 0
sif $pre/etc/ssh/ssh_host_ed25519_key.pub gid 0
DEBUGFS
    cut -d' ' -f1,2 "$tmp/host.pub"
    rm -rf "$tmp"
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
    local usage="usage: fcvm create VM IMAGE [--vcpus N] [--mem MiB] [--disk SIZE] [--copy] [--idle] [--entrypoint CMD] [--net full|none] [--allow HOST,...]... [-p [BIND:]HOST:GUEST]... [-v VOLUME:/PATH[:ro]]... [--restart POLICY] [--ttl DURATION] [--idle-stop DURATION] [--label KEY=VALUE]... [--tenant NAME] [--lease UNTIL] [--if-same-spec] [-- CMD...]"
    local vm=${1:?$usage}
    if [ "${2:-}" = --template ]; then template_expand "${@:3}"; set -- "$vm" "${TEMPLATE_ARGS[@]}"; fi
    local image=${2:?$usage}
    shift 2
    local vcpus=$VM_VCPUS mem=$VM_MEM_MIB disk="" copy=0 idle=0 ports=() vols=() shares=() argv=() netmode=full net_set=0 allow=()
    local entrypoint=() set_entrypoint=0 jail=${JAIL:-0} jail_set="" vcpus_set=null mem_set=null restart=no agent=1
    local ttl="" idle_stop="" ttl_raw="" idle_raw="" secrets=() forward=() cache=0 labels=() same_ok=0 lease=""
    while [ $# -gt 0 ]; do
        case $1 in
            --cache)        cache=1; shift ;;
            --label)        label_ok "${2:?--label wants KEY=VALUE}"; labels+=("$2"); shift 2 ;;
            --tenant)       tenant_ok "${2:?--tenant wants a name}"; labels+=("tenant=$2"); shift 2 ;;
            --if-same-spec) same_ok=1; shift ;;
            --lease)        lease=$(parse_until --lease "${2:?--lease wants a time or a duration}"); shift 2 ;;
            --secret)       parse_secret "${2:?--secret wants NAME=VALUE, NAME=@FILE or NAME}"; secrets+=("$2"); shift 2 ;;
            --forward)      mapfile -t forward < <(parse_forward "$(IFS=,; echo "${forward[*]:-},${2:?--forward wants ssh-agent and/or git}")"); shift 2 ;;
            --vcpus)        vcpus=$2 vcpus_set=$2; shift 2 ;;
            --mem)          mem=$2 mem_set=$2; shift 2 ;;
            --disk)         disk=$2; shift 2 ;;
            --copy)         copy=1; shift ;;
            --idle)         idle=1; shift ;;
            --jail)         jail=1 jail_set=1; shift ;;
            --no-agent)     agent=0; shift ;;
            --no-jail)      node_refuse "every VM is jailed (--no-jail)"; jail=0 jail_set=0; shift ;;
            --restart)      valid_restart "${2:-}"; restart=$2; shift 2 ;;
            --ttl)          ttl=$(duration_s --ttl "${2:?--ttl wants a duration}") ttl_raw=$2; shift 2 ;;
            --idle-stop)    idle_stop=$(duration_s --idle-stop "${2:?--idle-stop wants a duration}") idle_raw=$2; shift 2 ;;
            --entrypoint)   set_entrypoint=1; [ -z "${2-}" ] || entrypoint=("$2"); shift 2 ;;
            -p|--publish)   ports+=("$2"); shift 2 ;;
            -v|--volume)    case $2 in
                                /*|./*|../*|~*) shares+=("$2") ;;   # a host directory, mounted live
                                *)              vols+=("$2") ;;     # a named volume
                            esac; shift 2 ;;
            --net)          [[ ${2:-} =~ ^(full|none|restricted)$ ]] || die_code invalid "--net wants full, none or restricted (--allow adds hosts to a restricted network)"
                            netmode=$2 net_set=1; shift 2 ;;
            --allow)        IFS=, read -ra _a <<<"${2:?--allow wants HOST[,HOST...]}"; allow+=("${_a[@]}"); shift 2 ;;
            --)             shift; argv=("$@"); break ;;
            *)              die_code invalid "unknown option $1 ($usage)" ;;
        esac
    done
    [[ $vm =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die_code invalid "invalid VM name '$vm'"
    [[ $vcpus =~ ^[1-9][0-9]*$ && $mem =~ ^[1-9][0-9]*$ ]] || die "--vcpus and --mem want positive numbers"
    local tenant; tenant=$(labels_tenant "${labels[@]}")   # --tenant, or a template's tenant label
    sock_room "$vm"
    lock_vm "$vm"
    local existing=0
    if vm_exists "$vm"; then
        # --if-same-spec: a retry of a create that already happened (a script, a
        # control plane) is fine if it asks for the same VM; checked below, once
        # the request is validated and normalized.
        [ $same_ok = 1 ] && [ -z "${FCVM_TEMPLATE_OUT:-}" ] || die_code vm_exists "VM '$vm' already exists"
        existing=1
    fi
    [ "$restart" = no ] || [ -z "${EPHEMERAL:-}" ] || die "--restart doesn't apply to throwaway VMs (fcvm run); use create"
    local meta type raw_cmd=("${argv[@]}") raw_ep=("${entrypoint[@]}")
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
    local layer
    for layer in $(image_chain "$image"); do own_ok "$tenant" image "$layer" "$(image_tenant "$layer")"; done
    [ ${#forward[@]} -eq 0 ] || node_refuse "the host's credentials stay on the host (--forward)"
    if [ ${#allow[@]} -gt 0 ]; then
        [ "$netmode" != none ] || die "--allow and --net none are mutually exclusive"
        netmode=restricted
        expand_allow "${allow[@]}" >/dev/null   # validate now, expand at start
    elif [ $net_set = 0 ] && node_mode; then
        netmode=restricted   # a node's default: nothing out until the allowlist says so (fcvm egress --allow)
    fi
    [[ ",$NODE_NETWORKS," == *",$netmode,"* ]] || die_code policy "network '$netmode' isn't allowed on this host (NODE_NETWORKS=$NODE_NETWORKS)"
    [ $cache = 0 ] || [ "$netmode" = restricted ] || die "--cache is the egress mirror of a restricted network: it needs --allow or --net restricted"
    [ "$netmode" != none ] || [ ${#ports[@]} -eq 0 ] || die "-p needs a network; the VM has --net none"
    local p
    for p in "${ports[@]}"; do
        [[ $p =~ ^(([0-9.]+):)?[0-9]+:[0-9]+(/tcp)?$ ]] || die_code invalid "bad port spec '$p' (want [BIND:]HOSTPORT:GUESTPORT, TCP only)"
    done
    local sharejson='[]' hdir gpath sro
    for p in "${shares[@]}"; do
        parse_share "$p"
        jq -e --arg g "$SHARE_GUEST" 'any(.path == $g)' <<<"$sharejson" >/dev/null && die "two host directories at $SHARE_GUEST"
        sharejson=$(jq --arg h "$SHARE_HOST" --arg g "$SHARE_GUEST" --argjson ro "$SHARE_RO" \
            '. + [{host: $h, path: $g, ro: $ro}]' <<<"$sharejson")
    done
    for p in "${vols[@]}"; do
        [[ $p =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*:/[^:,]*(:ro)?$ ]] || die_code invalid "bad volume spec '$p' (want NAME:/PATH[:ro] for a named volume, or HOST/DIR[:GUEST/PATH][:ro] for a host directory)"
        [ -n "${FCVM_TEMPLATE_OUT:-}" ] && continue
        if [ -f "$VOLUMES_DIR/${p%%:*}.ext4" ]; then own_ok "$tenant" volume "${p%%:*}" "$(volume_tenant "${p%%:*}")"
        else volume_create "${p%%:*}" "$VOLUME_SIZE" ${tenant:+--tenant "$tenant"}; fi   # a new one is the VM's tenant's
    done
    [ -z "${FCVM_TEMPLATE_OUT:-}" ] || [ $agent = 1 ] || die "--no-agent can't be saved in a template"
    local secret_names=() s
    for s in "${secrets[@]}"; do
        [ -z "${FCVM_TEMPLATE_OUT:-}" ] || [[ $s != *=* ]] || die "a template takes secret names only (--secret NAME, from the environment at create), not values"
        secret_names+=("${s%%=*}")
    done
    [ $agent = 1 ] || [ -z "${EPHEMERAL:-}" ] || [ "$type" = app ] ||
        die "run needs the agent for a system image's shell; use create --no-agent, then fcvm console"
    [ $agent = 1 ] || [ -n "${FCVM_TEMPLATE_OUT:-}" ] || confirm_no_agent
    if [ -n "${FCVM_TEMPLATE_OUT:-}" ]; then   # fcvm template save: the checked recipe, no VM
        recipe_json template > "$FCVM_TEMPLATE_OUT"
        unlock_vm; rm -f "$VMS_DIR/.locks/vm-$vm"
        return 0
    fi

    local dir spec; dir=$(vm_dir "$vm")
    spec=$(recipe_json spec)
    if [ $existing = 1 ]; then
        if [ "$(jq -c '.spec // null' "$dir/vm.json")" = "$(jq -c . <<<"$spec")" ]; then
            unlock_vm
            [ -n "${EPHEMERAL:-}" ] || log "VM '$vm' already exists with this spec (--if-same-spec)"
            return 0
        fi
        die_code vm_exists "VM '$vm' already exists with a different spec (fcvm inspect $vm shows it)"
    fi
    refuse_if_cordoned
    # The disk the VM takes counts from now (its tenant's quota, the host's capacity)
    local need=${disk:-$VM_DISK}; [ $copy = 0 ] || need=${disk:-$(stat -c %s "$IMAGES_DIR/$image.ext4")}
    node_admit ${tenant:+--tenant "$tenant"} --disk "$need" || exit 1
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
    local hostkey=""
    if [ "$type" = system ]; then
        if [ $copy = 1 ]; then hostkey=$(ssh_identity "$dir/disk.ext4" ""); else hostkey=$(ssh_identity "$dir/rw.ext4" /upper); fi
    fi
    jq -n --arg image "$image" --arg type "$type" --argjson vcpus "$vcpus" --argjson mem "$mem" \
        --argjson ephemeral "${EPHEMERAL:-false}" \
        --argjson ports "$(jq -n '$ARGS.positional' --args "${ports[@]}")" \
        --argjson volumes "$(jq -n '$ARGS.positional' --args "${vols[@]}")" --argjson shares "$sharejson" \
        --argjson jail "$([ "$jail" = 1 ] && echo true || echo false)" --arg restart "$restart" \
        --argjson net "$(jq -n --arg mode "$netmode" --argjson cache "$([ $cache = 1 ] && echo true || echo false)" \
            '{mode: $mode, allow: $ARGS.positional} + (if $cache then {cache: true} else {} end)' --args "${allow[@]}")" \
        '{image: $image, type: $type, vcpus: $vcpus, mem_mib: $mem, ports: $ports, volumes: $volumes,
          shares: $shares, net: $net, jail: $jail, restart: $restart, ephemeral: $ephemeral, created: (now | todate),
          labels: $labels, spec: $spec}
         + (if $lease == "" then {} else {lease_until: ($lease | tonumber)} end)
         + (if $agent then {} else {agent: false} end) + (if $hostkey == "" then {} else {ssh_host_key: $hostkey} end)
         + $lifetime + (if ($forward | length) == 0 then {} else {forward: $forward} end)
         + (if ($forward | index("ssh-agent")) then {ssh_auth_sock: $sock} else {} end)' \
        --arg hostkey "$hostkey" --argjson agent "$([ $agent = 1 ] && echo true || echo false)" \
        --argjson lifetime "$(lifetime_json "$ttl" "$idle_stop")" \
        --argjson forward "$(json_list "${forward[@]}")" --arg sock "${SSH_AUTH_SOCK:-}" \
        --argjson labels "$(labels_json "${labels[@]}")" --argjson spec "$spec" --arg lease "$lease" > "$dir/vm.json"
    if [[ " ${forward[*]:-} " == *" ssh-agent "* ]] && [ ! -S "${SSH_AUTH_SOCK:-}" ]; then
        warn "--forward ssh-agent: no agent in this environment (SSH_AUTH_SOCK); set it before fcvm start"
    fi
    local SECRETS_DEL=()
    [ ${#secrets[@]} -eq 0 ] || write_secrets "$dir" "${secrets[@]}"
    emit_event vm.created "$vm" "" "$(jq -nc --arg i "$image" --argjson e "${EPHEMERAL:-false}" '{image: $i, ephemeral: $e}')"
    [ -n "${EPHEMERAL:-}" ] || log "created VM '$vm' from $image ($([ $copy = 1 ] && echo 'private copy' || echo 'shared image + writable layer'))"
}

# The create request, checked and normalized, as JSON: what a template holds
# (fcvm template save) and what vm.json keeps as the VM's `spec`. The template
# keeps vcpus and mem_mib only when given (null means the defaults at create);
# the spec keeps the values the VM got. Reads create()'s variables.
recipe_json() {   # template | spec
    local proc=image v=$vcpus_set m=$mem_set; [ $idle = 0 ] || proc=idle; [ ${#raw_cmd[@]} -eq 0 ] || proc=command
    [ "$1" != spec ] || v=$vcpus m=$mem
    jq -n --arg image "$image" --arg ref "$(jq -r '.ref // empty' "$meta")" --argjson vcpus "$v" \
        --argjson mem "$m" --arg disk "$disk" --argjson copy "$([ $copy = 1 ] && echo true || echo false)" \
        --argjson ports "$(json_list "${ports[@]}")" \
        --argjson volumes "$(json_list "${vols[@]}" "${shares[@]}")" \
        --arg net "$netmode" --argjson allow "$(json_list "${allow[@]}")" --argjson cache "$([ $cache = 1 ] && echo true || echo false)" \
        --arg proc "$proc" --argjson command "$(json_list "${raw_cmd[@]}")" \
        --argjson ep_set "$set_entrypoint" --arg ep "${raw_ep[*]}" --arg jail "$jail_set" --arg restart "$restart" \
        --arg ttl "${ttl:+$ttl_raw}" --arg idle_stop "${idle_stop:+$idle_raw}" --argjson secrets "$(json_list "${secret_names[@]}")" \
        --argjson forward "$(json_list "${forward[@]}")" --argjson labels "$(labels_json "${labels[@]}")" --arg kind "$1" \
        '{image: $image, ref: (if $ref == "" then null else $ref end), vcpus: $vcpus, mem_mib: $mem,
          disk: (if $disk == "" then null else $disk end), copy: $copy, network: $net, allow: $allow,
          ports: $ports, volumes: $volumes, process: $proc, command: $command,
          entrypoint: (if $ep_set == 1 then $ep else null end),
          jail: (if $jail == "" then null else ($jail == "1") end), restart: $restart,
          ttl: (if $ttl == "" then null else $ttl end), idle_stop: (if $idle_stop == "" then null else $idle_stop end),
          secrets: $secrets, forward: $forward, cache: $cache}
         + (if $kind == "template" and ($labels | length) > 0 then {labels: $labels} else {} end)'
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
            [ -n "$hosts" ] || die_code invalid "unknown egress preset '$e' (see lib/egress-presets.conf)"
            echo "$hosts"
        else
            [[ $e =~ ^(\*\.)?[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die_code invalid "bad allowlist entry '$e' (want host, *.domain, host:port or @preset)"
            echo "$e"
        fi
    done
}

# Policy file the egress proxy reads for the VM at $IP (re-read per request).
# The VM's tenant picks its mirror cache (cache/egress/tenants/NAME).
write_policy() {   # vm ip
    local dir; dir=$(vm_dir "$1")
    mkdir -p "$VMS_DIR/.egress"
    jq -n --arg vm "$1" --arg log "$dir/egress.log" --arg t "$(vm_tenant "$1")" \
        --argjson allow "$(jq -n '$ARGS.positional' --args $(expand_allow $(jq -r '.net.allow[]?' "$dir/vm.json")))" \
        '{vm: $vm, allow: $allow, log: $log} + (if $t == "" then {} else {tenant: $t} end)' > "$VMS_DIR/.egress/$2.json.tmp"
    mv "$VMS_DIR/.egress/$2.json.tmp" "$VMS_DIR/.egress/$2.json"
}

# One egress proxy per user, shared by all restricted VMs, kept alive by a
# restart loop: a crash (or killing the python process to reload it) costs at
# most a second of refused connections. proxy.pid is the loop; kill it to stop.
proxy_listening() { (exec 3<>"/dev/tcp/$NET_R_PREFIX.1/$EGRESS_PORT") 2>/dev/null; }
ensure_proxy() {
    local pidf=$VMS_DIR/.egress/proxy.pid pid i
    # a proxy started with other settings, or from older code (an upgrade), is replaced
    local want="$NET_R_PREFIX.1:$EGRESS_PORT $CACHE_DIR/egress $(stat -c %Y "$FCVM_ROOT/lib/egress_proxy.py")"
    pid=$(cat "$pidf" 2>/dev/null || true)
    # The recorded pid must still be our restart loop (after a reboot it may be
    # anything), and the proxy must answer: a loop whose python just crashed
    # is back within a second.
    if [ -n "$pid" ] && tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -qF egress-proxy; then
        if [ "$(cat "$VMS_DIR/.egress/proxy.args" 2>/dev/null)" = "$want" ]; then
            for ((i = 0; i < 30; i++)); do proxy_listening && return 0; sleep 0.1; done
        fi
        kill "$pid" 2>/dev/null || true   # never answering, or from an older fcvm: replace it
        pkill -f "egress_proxy.py --listen $NET_R_PREFIX.1:$EGRESS_PORT" 2>/dev/null || true
        sleep 0.3
    fi
    mkdir -p "$VMS_DIR/.egress" "$CACHE_DIR/egress"
    (   # don't let the long-lived proxy inherit this VM's tap lock
        if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
        exec {VM_LOCK}>&-
        exec setsid bash -c 'while :; do python3 "$1" --listen "$2" --policy-dir "$3" --cache-dir "$4"; sleep 1; done' egress-proxy \
            "$FCVM_ROOT/lib/egress_proxy.py" "$NET_R_PREFIX.1:$EGRESS_PORT" "$VMS_DIR/.egress" "$CACHE_DIR/egress" \
            </dev/null >>"$VMS_DIR/.egress/proxy.log" 2>&1
    ) &
    echo $! > "$pidf"
    echo "$want" > "$VMS_DIR/.egress/proxy.args"
    for ((i = 0; i < 20; i++)); do
        proxy_listening && return 0
        sleep 0.1
    done
    cat "$VMS_DIR/.egress/proxy.log" >&2; die "egress proxy failed to start"
}


# Publish ports for the VM whose firecracker runs as pid $1 (see portfwd.py):
# one forwarder per port, so `fcvm unpublish` can end one while the VM runs.
start_portfwd() {
    [ ${#PORTS[@]} -gt 0 ] || return 0
    if [ -z "$IP" ]; then warn "no network: ports not published"; return 0; fi
    local p
    for p in "${PORTS[@]}"; do forward_port "$1" "$IP" "$p" || return 1; done
}
# A forwarder for port spec $3 to the VM at $2 (Firecracker pid $1), recorded
# in $DIR/portfwd.live as "SPEC PID"; it exits with the VM.
forward_port() {   # fcpid ip spec
    local pid
    setsid python3 "$FCVM_ROOT/lib/portfwd.py" --watch "$1" --target "$2" "$3" \
        </dev/null >>"$DIR/portfwd.log" 2>&1 {VM_LOCK}>&- &
    pid=$!
    sleep 0.3
    kill -0 $pid 2>/dev/null || { tail -n 3 "$DIR/portfwd.log" >&2; return 1; }
    echo "$3 $pid" >> "$DIR/portfwd.live"
}
# End the forwarders listed in $1/portfwd.live (all, or only spec $2).
stop_portfwd() {   # dir [spec]
    [ -f "$1/portfwd.live" ] || return 0
    local s p
    while read -r s p; do [ -n "${2:-}" ] && [ "$s" != "$2" ] || kill_ours "$p" portfwd.py; done < "$1/portfwd.live"
    if [ -n "${2:-}" ]; then awk -v s="$2" '$1 != s' "$1/portfwd.live" > "$1/portfwd.live.tmp" && mv "$1/portfwd.live.tmp" "$1/portfwd.live"
    else rm -f "$1/portfwd.live"; fi
}

# fcvm start [-a] VM: boot in the background (like docker start); -a attaches the
# console afterwards. The serial console is held by lib/console.py, which logs
# it and lets clients attach/detach; when Firecracker exits it runs `_reap`.
start() {
    local usage="usage: fcvm start [-a] [--lease UNTIL] VM" attach=0 vm="" lease=""
    while [ $# -gt 0 ]; do
        case $1 in
            -a|--attach) attach=1; shift ;;
            --lease)     lease=$(parse_until --lease "${2:?$usage}"); shift 2 ;;
            -d)          shift ;;   # background is the default now
            -*)          die_code invalid "unknown option $1 ($usage)" ;;
            *)           vm=$1; shift ;;
        esac
    done
    [ -n "$vm" ] || die_code invalid "$usage"
    if [ -n "$lease" ]; then   # set here, whoever starts the VM (the service may run an older fcvm)
        lock_vm "$vm"; vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"; set_lease "$vm" "$lease"; unlock_vm
    fi
    if [ -n "${VIA_DAEMON:-}" ] && [ -z "${RESTORE:-}" ] && use_daemon; then
        local reply rc=0
        reply=$(daemon_api POST "vms/$vm/start" "$(jq -n --arg l "$lease" '(if $l == "" then {} else {lease: ($l | tonumber)} end)')") || rc=$?
        if [ $rc = 0 ]; then
            [ -n "${QUIET_START:-}" ] || log "running (pid $(jq -r .pid <<<"$reply"), started by the fcvm service). fcvm console $vm | fcvm logs $vm | fcvm stop $vm"
            if [ $attach = 1 ]; then attach_vm "$vm"; fi
            return
        fi
        [ $rc = 2 ] || exit 1
        warn "the fcvm service isn't answering; starting '$vm' from this terminal"
    fi
    lock_vm "$vm"
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" && die_code vm_running "VM '$vm' is already running (pid $(vm_pid "$vm"))"
    vm_jailed "$vm" || node_refuse "'$vm' isn't jailed (created before node mode?); every VM runs jailed here: recreate it"
    rm -f "$VMS_DIR/$vm/stopped" "$VMS_DIR/$vm/resume" "$VMS_DIR/$vm/expired" "$VMS_DIR/$vm/claimed"
    touch_activity "$vm"   # --idle-stop counts from the boot until the first exec
    [ -x "$FIRECRACKER" ] || die "firecracker not installed (run: fcvm firecracker)"
    sock_room "$vm"
    node_admit "$vm" || exit 1
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
            claim_tap fcrtap || die_code no_network_slot "no free restricted tap (fcrtap*); run: fcvm net-up"
            IP=$NET_R_PREFIX.$((10 + TAP_INDEX))
            local proxy=http://$NET_R_PREFIX.1:$EGRESS_PORT v
            args+=" ip=$IP::$NET_R_PREFIX.1:255.255.255.0:$vm:eth0:off:$NET_R_PREFIX.1 fcvm.proxy=$proxy"
            if [ "$type" = system ]; then
                for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do args+=" systemd.setenv=$v=$proxy"; done
                args+=" systemd.setenv=no_proxy=localhost,127.0.0.1"
            fi
            if [ "$(jq -r '.net.cache // false' "$DIR/vm.json")" = true ]; then   # the mirror (--cache)
                args+=" fcvm.cache=$proxy/cache"
                if [ "$type" = system ]; then
                    args+=" systemd.setenv=FCVM_CACHE=$proxy/cache systemd.setenv=PIP_INDEX_URL=$proxy/cache/pypi.org/simple/"
                    args+=" systemd.setenv=PIP_TRUSTED_HOST=$NET_R_PREFIX.1 systemd.setenv=npm_config_registry=$proxy/cache/registry.npmjs.org/"
                fi
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
                warn "no free tap device; starting without network (run: fcvm net-up)"
            fi ;;
    esac
    [ -z "$IP" ] || echo "$IP" > "$DIR/ip"

    # Drives, in attach order: vda, vdb, ... (Firecracker keeps config order).
    # fc-init (initramfs) assembles the root from the fcvm.* arguments.
    initramfs_current || "$FCVM_ROOT/lib/build-init.sh"
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
            [ -z "$busy" ] || die_code in_use "volume '$vname' is in use by running VM(s): $busy"
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
    local secrets=0; [ ! -s "$DIR/secrets.json" ] || secrets=$(jq length "$DIR/secrets.json")
    [ "$secrets" = 0 ] || args+=" fcvm.secrets=$SECRETS_PORT"
    # Credential forwarding (lib/forward.py): the agent socket of whoever starts
    # the VM, else the one recorded at create (the service starts VMs as you).
    local fwd_args=() fwd="" agent_sock
    if jq -e '.forward | index("ssh-agent")' "$DIR/vm.json" >/dev/null 2>&1; then
        agent_sock=${SSH_AUTH_SOCK:-$(jq -r '.ssh_auth_sock // empty' "$DIR/vm.json")}
        if [ -S "$agent_sock" ]; then
            args+=" fcvm.ssh_agent=9001"; fwd_args+=(--ssh-agent "$agent_sock"); fwd+="ssh-agent "
        else
            warn "ssh-agent forwarding: no agent socket (SSH_AUTH_SOCK=${agent_sock:-unset}); '$vm' gets none this time"
        fi
    fi
    if jq -e '.forward | index("git")' "$DIR/vm.json" >/dev/null 2>&1; then
        args+=" fcvm.git_cred=9002"; fwd_args+=(--git); fwd+="git"
    fi
    [ "$type" = system ] && args+=" fcvm.exec=/sbin/init"
    [ "$(jq -r '.agent != false' "$DIR/vm.json")" = true ] || args+=" fcvm.agent=0"
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
        "balloon": {amount_mib: 0, deflate_on_oom: true, stats_polling_interval_s: 5},
        "entropy": {}
    }' > "$DIR/fc.json"

    local jailed=0
    if vm_jailed "$vm"; then
        jailed=1
        [ -S /run/fcvm/jaild.sock ] || die_code jail_unavailable "'$vm' runs jailed, but fcvm-jaild isn't running (run: fcvm jail-setup)"
        [ "$(/usr/local/lib/fcvm/firecracker --version 2>/dev/null | head -1)" = "$("$FIRECRACKER" --version | head -1)" ] ||
            warn "fcvm-jaild has a different Firecracker than bin/ (re-run fcvm jail-setup)"
        # The helper re-validates everything in this request (paths, ownership, writability).
        jq -n --arg vm "$vm" --arg kernel "$kernel" --arg initrd "$BUILD_DIR/initramfs.cpio" --arg args "$args" \
            --argjson drives "$(jq '[.[] | {drive_id, path: .path_on_host, read_only: .is_read_only}]' <<<"$drives")" \
            --argjson vcpus "$vcpus" --argjson mem "$mem" \
            --arg tap "$(jq -r '.[0].host_dev_name // empty' <<<"$net")" --arg mac "$(jq -r '.[0].guest_mac // empty' <<<"$net")" \
            '{op: "launch", vm: $vm, kernel: $kernel, initrd: $initrd, drives: $drives, boot_args: $args,
              vcpus: $vcpus, mem_mib: $mem, tap: (if $tap == "" then null else $tap end), mac: $mac}
              + (if $restore == "" then {} else {restore: $restore} end)' --arg restore "${RESTORE:-}" > "$DIR/jail-request.json"
    fi
    rm -f "$DIR/fc.sock" "$DIR/vsock.sock" "$DIR/console.sock" "$DIR/pid" "$DIR/waiter" "$DIR/firecracker.log" "$DIR/portfwd.live"
    [ $jailed = 1 ] || : > "$DIR/firecracker.log"
    : > "$DIR/console.log"; : > "$DIR/portfwd.log"
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
    # Secrets: a server at <vsock uds>_9000 (lib/secrets.py), started like the shares.
    start_secrets() {   # uds-prefix
        (
            if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
            exec {VM_LOCK}>&-
            exec setsid python3 "$FCVM_ROOT/lib/secrets.py" serve --uds "${1}_$SECRETS_PORT" --file "$DIR/secrets.json" \
                --pidfile "$DIR/pid" </dev/null >"$DIR/secrets.log" 2>&1
        ) &
        echo $! > "$DIR/secrets.pid"
        for ((i = 0; i < 50; i++)); do [ -S "${1}_$SECRETS_PORT" ] && break; sleep 0.02; done
        [ -S "${1}_$SECRETS_PORT" ] || { cat "$DIR/secrets.log" >&2; die "the secrets server failed to start"; }
    }
    if [ "$secrets" != 0 ] && [ $jailed = 0 ]; then start_secrets "$DIR/vsock.sock"; fi
    start_forward() {   # uds-prefix
        (
            if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
            exec {VM_LOCK}>&-
            exec setsid python3 "$FCVM_ROOT/lib/forward.py" serve --uds-prefix "$1" --pidfile "$DIR/pid" \
                --log "$DIR/forward.log" "${fwd_args[@]}" </dev/null >"$DIR/forward.err" 2>&1
        ) &
        echo $! > "$DIR/forward.pid"
        local want="${1}_9002"; [[ $fwd != ssh-agent* ]] || want="${1}_9001"
        for ((i = 0; i < 50; i++)); do [ -S "$want" ] && break; sleep 0.02; done
        [ -S "$want" ] || { cat "$DIR/forward.err" >&2; die "the credential forwarder failed to start"; }
    }
    if [ ${#fwd_args[@]} -gt 0 ] && [ $jailed = 0 ]; then start_forward "$DIR/vsock.sock"; fi
    local launch=(-- "${fc[@]}")
    [ $jailed = 0 ] || launch=(--jail "$DIR/jail-request.json" --link-dir "$DIR")
    [ "$secrets" = 0 ] || info+=", $secrets secret(s)"
    [ -z "$fwd" ] || info+=", forwards ${fwd% }"
    setsid python3 "$FCVM_ROOT/lib/console.py" serve --sock "$DIR/console.sock" --log "$DIR/console.log" \
        --pidfile "$DIR/pid" --on-exit "$(printf '%q _reap %q' "$FCVM_ROOT/fcvm" "$vm")" "${launch[@]}" \
        </dev/null >"$DIR/relay.log" 2>&1 {VM_LOCK}>&- &
    # The relay writes the pid once Firecracker (or the jail) is launched: well
    # under a second, but more on a cold disk (the first VM on a fresh cloud
    # host reads Python, the kernel and the image for the first time).
    local i pid="" relay=$!
    for ((i = 0; i < 750; i++)); do
        pid=$(vm_pid "$vm"); [ -n "$pid" ] && break
        kill -0 "$relay" 2>/dev/null || { pid=$(vm_pid "$vm"); break; }   # the relay is gone
        sleep 0.02
    done
    [ -n "$pid" ] || { cat "$DIR/relay.log" >&2; die "console relay failed to start$([ $i -lt 750 ] || echo " in 15 s")"; }
    record_pid_id "$vm" "$pid"; rm -f "$DIR/starting"
    if [ ${#exports[@]} -gt 0 ] && [ $jailed = 1 ]; then start_shares "$(dirname "$(readlink "$DIR/vsock.sock")")/vsock.sock"; fi
    if [ "$secrets" != 0 ] && [ $jailed = 1 ]; then start_secrets "$(dirname "$(readlink "$DIR/vsock.sock")")/vsock.sock"; fi
    if [ ${#fwd_args[@]} -gt 0 ] && [ $jailed = 1 ]; then start_forward "$(dirname "$(readlink "$DIR/vsock.sock")")/vsock.sock"; fi
    if ! start_portfwd "$pid"; then
        if [ $jailed = 1 ]; then jaild kill "$vm" >/dev/null || true; else kill "$pid" 2>/dev/null || true; fi
        die "cannot publish ports; VM not started"
    fi
    if [ -n "${RESTORE:-}" ]; then   # fork of a jailed snapshot: same chroot paths, same tap0
        if ! fc_api "$DIR/fc.sock" PUT /snapshot/load '{"snapshot_path": "/snap.vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": "/snap.mem"},
                "resume_vm": true}'; then
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
    emit_event vm.started "$vm" "${RESTORE:+fork}" "$(jq -nc --arg pid "$pid" --arg ip "$IP" '{pid: ($pid | tonumber), ip: (if $ip == "" then null else $ip end)}')"
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
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    dir=$(vm_dir "$vm")
    # Stopped by you: restart policies leave it alone until you start it again.
    # A stop for host shutdown (fcvm _shutdown) marks it to resume instead. A
    # stop because its --ttl or --idle-stop ran out (the supervisor sets
    # FCVM_EXPIRED) counts as yours, and says why in `expired`.
    [ -n "${FCVM_SYSTEM_STOP:-}" ] || touch "$dir/stopped"
    [ -z "${FCVM_EXPIRED:-}" ] || echo "$FCVM_EXPIRED" > "$dir/expired"
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
        [ -n "${QUIET_STOP:-}" ] || log "stopped '$vm'${FCVM_EXPIRED:+ (its ${FCVM_EXPIRED/idle/idle time} ran out)}"
        emit_event vm.stopped "$vm" "${FCVM_EXPIRED:-$([ -n "${FCVM_SYSTEM_STOP:-}" ] && echo system || echo user)}"
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
    emit_event vm.exited "$vm" "$(jq -r 'if .stale then "crash" elif .code != null then "exit" elif (.fc_status // 0) != 0 then "crash" else "shutdown" end' "$dir/last-exit")" "$(cat "$dir/last-exit")"
    kill_ours "$(cat "$dir/portfwd.pid" 2>/dev/null)" portfwd.py   # VMs started before 0.7: one forwarder for all ports
    stop_portfwd "$dir"
    kill_ours "$(cat "$dir/share.pid" 2>/dev/null)" share9p.py; rm -f "$dir/share.pid"
    kill_ours "$(cat "$dir/secrets.pid" 2>/dev/null)" secrets.py; rm -f "$dir/secrets.pid"
    kill_ours "$(cat "$dir/forward.pid" 2>/dev/null)" forward.py; rm -f "$dir/forward.pid"
    if [ -f "$dir/share.live" ]; then
        local sp; for sp in $(awk '{print $2}' "$dir/share.live"); do kill_ours "$sp" share9p.py; done
        rm -f "$dir/share.live"
    fi
    rm -f "$dir"/vsock.sock_*
    if [ -f "$dir/ip" ]; then rm -f "$VMS_DIR/.egress/$(cat "$dir/ip").json"; fi
    rm -f "$dir/fc.sock" "$dir/vsock.sock" "$dir/console.sock" "$dir/portfwd.pid" "$dir/pid" "$dir/pid.id"
    if [ -L "$dir/firecracker.log" ]; then rm -f "$dir/firecracker.log"; fi
    if [ "$(jq -r .ephemeral "$dir/vm.json")" = true ]; then emit_event vm.deleted "$vm" ephemeral; rm -rf "$dir" "$VMS_DIR/.locks/vm-$vm"; fi
}

# fcvm run IMAGE [-d] [opts] [-- CMD...]: throwaway VM, deleted when it stops.
#   container image: attached to its console like `docker run` (Ctrl-] detaches);
#                    exits with the container's exit code
#   systemd image:   boots, then `fcvm shell` (or CMD via exec); the VM is
#                    stopped and deleted when the shell/command ends
run() {
    local usage="usage: fcvm run IMAGE|--template NAME [-d] [-p [BIND:]HOST:GUEST]... [--vcpus N] [--mem MiB] [--disk SIZE] [-- CMD...]"
    if [ "${1:-}" = --template ]; then template_expand "${@:2}"; set -- "${TEMPLATE_ARGS[@]}"; fi
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
        if [ $bg = 1 ]; then VIA_DAEMON=1 start "$vm"; return; fi
        QUIET_START=1 start "$vm"
        attach_vm "$vm" all
        return
    fi

    EPHEMERAL=true create "$vm" "$image" "${opts[@]}"
    if [ $bg = 1 ]; then VIA_DAEMON=1 start "$vm"; return; fi
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
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running (see: fcvm logs $vm)"
    attach_vm "$vm" tail
}

# fcvm logs [-f] VM: console output of the current/last boot.
logs() {
    local follow=0 vm=""
    while [ $# -gt 0 ]; do
        case $1 in -f|--follow) follow=1 ;; *) vm=$1 ;; esac
        shift
    done
    [ -n "$vm" ] || die_code invalid "usage: fcvm logs [-f] VM"
    local f; f=$(vm_dir "$vm")/console.log
    [ -f "$f" ] || die "no console log for '$vm'"
    if [ $follow = 1 ]; then tail -n +1 -f "$f"; else cat "$f"; fi
}

ssh_vm() {
    local vm=${1:?usage: fcvm ssh VM [args]}; shift
    local ip; ip=$(cat "$(vm_dir "$vm")/ip" 2>/dev/null) || die_code no_network "VM '$vm' has no network"
    local hostkey; hostkey=$(jq -r '.ssh_host_key // empty' "$(vm_dir "$vm")/vm.json")
    if [ -z "$hostkey" ]; then   # created before fcvm 0.6.1: its host key was made at first boot, unknown here
        warn "'$vm' predates per-VM host keys, so its host key isn't checked (recreate it to get one)"
        exec ssh -i "$(ssh_key)" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR "root@$ip" "$@"
    fi
    local known; known="$(vm_dir "$vm")/known_hosts"
    echo "fcvm-$vm $hostkey" > "$known"
    exec ssh -i "$(ssh_key)" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$known" \
        -o HostKeyAlias="fcvm-$vm" -o LogLevel=ERROR "root@$ip" "$@"
}

# fcvm ssh-config [VM...] [--install]: ssh client configuration for system VMs
# (`Host fcvm-NAME`), with a ProxyCommand that finds the VM's current address,
# so an entry stays right across restarts and works for ssh, scp, rsync and
# VS Code Remote-SSH alike. --install writes ~/.ssh/fcvm_config and includes
# it from ~/.ssh/config.
ssh_config_cmd() {
    local usage="usage: fcvm ssh-config [VM...] [--install]" install=0 names=() a d vm out=""
    for a; do case $a in --install) install=1 ;; -*) die_code invalid "unknown option $a ($usage)" ;; *) names+=("$a") ;; esac; done
    if [ ${#names[@]} -eq 0 ]; then
        for d in "$VMS_DIR"/*/; do
            [ -f "$d/vm.json" ] && [[ $(basename "$d") != _* ]] && [ "$(jq -r .type "$d/vm.json")" = system ] || continue
            names+=("$(basename "$d")")
        done
        [ ${#names[@]} -gt 0 ] || die "no system VMs (app VMs have no sshd: use fcvm shell)"
    fi
    for vm in "${names[@]}"; do
        vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
        if [ "$(jq -r .type "$(vm_dir "$vm")/vm.json")" != system ]; then warn "'$vm' is an app VM without sshd (use fcvm shell); skipped"; continue; fi
        out+="$(ssh_config_entry "$vm")"$'\n\n'
    done
    if [ $install = 0 ]; then printf '%s' "$out"; return; fi
    local f=$HOME/.ssh/fcvm_config cfg=$HOME/.ssh/config
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
    printf '# written by `fcvm ssh-config --install`; re-run it after creating VMs\n\n%s' "$out" > "$f"; chmod 600 "$f"
    if ! grep -qsx "Include ~/.ssh/fcvm_config" "$cfg"; then   # first, before any Host block
        { echo "Include ~/.ssh/fcvm_config"; echo; [ ! -f "$cfg" ] || cat "$cfg"; } > "$cfg.tmp"
        mv "$cfg.tmp" "$cfg"; chmod 600 "$cfg"
    fi
    log "wrote $f (${#names[@]} VM(s)) and included it from $cfg. Try: ssh fcvm-${names[0]}"
}
ssh_config_entry() {
    local vm=$1 dir hostkey check="StrictHostKeyChecking yes" known
    dir=$(vm_dir "$vm"); known=$dir/known_hosts
    hostkey=$(jq -r '.ssh_host_key // empty' "$dir/vm.json")
    if [ -n "$hostkey" ]; then echo "fcvm-$vm $hostkey" > "$known"
    else check="StrictHostKeyChecking no" known=/dev/null; fi   # created before fcvm 0.6.1: no recorded host key
    cat <<EOF
Host fcvm-$vm
    # fcvm VM '$vm' (fcvm ssh-config): the ProxyCommand finds its current address
    User root
    IdentityFile $(ssh_key)
    IdentitiesOnly yes
    HostKeyAlias fcvm-$vm
    UserKnownHostsFile $known
    $check
    ProxyCommand $(fcvm_entry) _ssh-proxy $vm
    LogLevel ERROR
EOF
}
# fcvm _ssh-proxy VM: ssh's ProxyCommand: this stdin/stdout <-> the VM's port 22
# (retrying for a few seconds while sshd comes up after a start).
ssh_proxy() {
    local vm=${1:?usage: fcvm _ssh-proxy VM} ip hostkey dir
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running (fcvm start $vm)"
    dir=$(vm_dir "$vm")
    ip=$(cat "$dir/ip" 2>/dev/null) || die_code no_network "VM '$vm' has no network"
    hostkey=$(jq -r '.ssh_host_key // empty' "$dir/vm.json")
    [ -z "$hostkey" ] || echo "fcvm-$vm $hostkey" > "$dir/known_hosts"   # current, even if the VM was recreated
    exec python3 "$FCVM_ROOT/lib/ssh_proxy.py" "$ip"   # stdin/stdout stay ssh's
}

# fcvm exec [-i] [-t] [-u USER] VM [CMD...]: like `docker exec`, over vsock
# (no network or sshd needed). USER is name|uid[:group|gid], resolved in the guest.
exec_vm() {
    local usage="usage: fcvm exec [-i] [-t] [-d] [-u USER] [-w DIR] [-e KEY=VAL]... [--timeout SECS] VM [CMD...]" flags=() detach=0
    while [[ ${1:-} == -* ]]; do
        case $1 in
            -u|--user)     flags+=(-u "${2:?$usage}"); shift 2 ;;
            -w|--workdir)  flags+=(-w "${2:?$usage}"); shift 2 ;;
            -e|--env)      [[ ${2:-} == *=* ]] || die_code invalid "-e wants KEY=VALUE"; flags+=(-e "$2"); shift 2 ;;
            --timeout)     [[ ${2:-} =~ ^[0-9]+([.][0-9]+)?$ ]] || die_code invalid "--timeout wants seconds"; flags+=(--timeout "$2"); shift 2 ;;
            -d|--detach)   detach=1; shift ;;
            -i|-t|-it|-ti) [[ $1 == *i* ]] && flags+=(-i); [[ $1 == *t* ]] && flags+=(-t); shift ;;
            *)             die_code invalid "unknown option $1 ($usage)" ;;
        esac
    done
    local vm=${1:?$usage}; shift
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running"
    need_agent "$vm" exec
    [ "$(jq -r '.net.mode // "full"' "$(vm_dir "$vm")/vm.json")" != restricted ] || ensure_proxy
    touch_activity "$vm"
    if [ $detach = 1 ]; then
        [ $# -gt 0 ] || die "exec -d wants a command"
        start_job "$vm" "$@"
        return
    fi
    python3 "$FCVM_ROOT/lib/exec_client.py" "${flags[@]}" "$(vm_dir "$vm")/vsock.sock" -- "$@"
}

# --- background commands: fcvm exec -d, fcvm jobs ---------------------------------------
# A job is a command started with `exec -d`: it runs through the agent like any
# exec, but from a host-side process of its own, with its output in
# vms/VM/jobs/ID.out and ID.err and its exit status in ID.status once it ends.
# Killing that process drops the agent connection, and the agent kills the
# command's process group in the guest. (Uses `flags` from exec_vm.)
start_job() {   # vm cmd...
    local vm=$1 dir jd id; shift
    dir=$(vm_dir "$vm"); jd=$dir/jobs
    mkdir -p "$jd"
    id=$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')
    jq -n --arg id "$id" --argjson command "$(json_list "$@")" '{id: $id, command: $command, started: now}' > "$jd/$id.json"
    (
        exec {VM_LOCK}>&-
        if [ -n "${TAP_FD:-}" ]; then exec {TAP_FD}>&-; fi
        exec setsid bash -c '
            python3 "$1" "${@:3}" </dev/null >"$2.out" 2>"$2.err" & echo $! > "$2.pid"
            wait $!; echo $? > "$2.status"' job \
            "$FCVM_ROOT/lib/exec_client.py" "$jd/$id" "${flags[@]}" "$dir/vsock.sock" -- "$@" </dev/null >/dev/null 2>&1
    ) &
    log "job $id started in '$vm' (fcvm jobs $vm $id shows its output)"
    echo "$id"
}

# One job's state as JSON: status running | exited | lost (the connection
# dropped: the VM stopped, or `--kill` ended it), with its exit code. A command
# killed by a signal in the VM exited (128+N), like in a shell.
job_json() {   # jobs-dir id
    local jd=$1 id=$2 status=running code="" ended=null
    if [ -f "$jd/$id.status" ]; then
        code=$(cat "$jd/$id.status"); ended=$(stat -c %Y "$jd/$id.status")
        status=exited; [ "$code" != 255 ] && [ ! -f "$jd/$id.killed" ] || status=lost
    elif ! kill -0 "$(cat "$jd/$id.pid" 2>/dev/null)" 2>/dev/null; then
        sleep 0.3     # its status is being written right now, or it is gone for good
        [ -f "$jd/$id.status" ] && job_json "$jd" "$id" && return
        status=lost
    fi
    jq --arg s "$status" --arg c "$code" --argjson ended "$ended" \
        '. + {status: $s, exit_code: ($c | tonumber? // null), ended: $ended}' "$jd/$id.json"
}

# fcvm jobs VM [--json] | VM ID [-f] | VM ID --kill | VM --clear
jobs_cmd() {
    local usage="usage: fcvm jobs VM [--json] | fcvm jobs VM ID [-f] | fcvm jobs VM ID --kill | fcvm jobs VM --clear"
    local vm=${1:?$usage} jd f id; shift
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    jd=$(vm_dir "$vm")/jobs
    case ${1:-} in
        ""|--json)
            for f in "$jd"/*.json; do [ -f "$f" ] || continue; job_json "$jd" "$(basename "$f" .json)"; done |
            if [ "${1:-}" = --json ]; then jq -s 'sort_by(.started)'; else
                jq -rs 'sort_by(.started)[] | [.id, (if .status == "exited" then "exited(\(.exit_code))" else .status end),
                    (.started | todate), (.command | join(" ") | .[0:60])] | @tsv' |
                { printf 'ID\tSTATUS\tSTARTED\tCOMMAND\n'; cat; } | column -t -s $'\t'
            fi ;;
        --clear)
            local n=0
            for f in "$jd"/*.status; do [ -f "$f" ] || continue; id=$(basename "$f" .status); rm -f "$jd/$id".*; n=$((n + 1)); done
            log "removed $n finished job(s) of '$vm'" ;;
        *)
            id=$1; shift
            [ -f "$jd/$id.json" ] || die_code not_found "no job '$id' in '$vm' (see: fcvm jobs $vm)"
            case ${1:-} in
                --kill)
                    [ ! -f "$jd/$id.status" ] || die "job $id has already ended"
                    : > "$jd/$id.killed"
                    kill_ours "$(cat "$jd/$id.pid" 2>/dev/null)" exec_client.py
                    log "killed job $id" ;;
                -f) tail -n +1 -f --pid="$(cat "$jd/$id.pid")" "$jd/$id.out" 2>/dev/null
                    cat "$jd/$id.err" >&2 ;;
                "") cat "$jd/$id.out"; cat "$jd/$id.err" >&2
                    [ ! -f "$jd/$id.status" ] || exit "$(cat "$jd/$id.status")" ;;
                *)  die_code invalid "$usage" ;;
            esac ;;
    esac
}

# fcvm shell [-u USER] VM: interactive shell (bash, else sh), as the image's user by default.
shell_vm() {
    local usage="usage: fcvm shell [-u USER] VM" user=() vm=""
    while [ $# -gt 0 ]; do
        case $1 in
            -u|--user) user=(-u "${2:?$usage}"); shift 2 ;;
            -*)        die_code invalid "unknown option $1 ($usage)" ;;
            *)         vm=$1; shift ;;
        esac
    done
    [ -n "$vm" ] || die_code invalid "$usage"
    if [ -t 0 ]; then exec_vm -it "${user[@]}" "$vm"; else exec_vm -i "${user[@]}" "$vm"; fi
}

# --- volumes: persistent ext4 disks, attached with -v NAME:/PATH[:ro] ---------

# fcvm volume create NAME [SIZE] [--tenant T]: an empty ext4 disk, sparse.
# volumes/NAME.json records who owns it (and when it was made); without a
# tenant the volume is public.
volume_create() {
    local usage="usage: fcvm volume create NAME [SIZE] [--tenant NAME]" name="" size="" tenant=""
    while [ $# -gt 0 ]; do
        case $1 in
            --tenant) tenant_ok "${2:?$usage}"; tenant=$2; shift 2 ;;
            -*)       die_code invalid "unknown option $1 ($usage)" ;;
            *)        if [ -z "$name" ]; then name=$1; elif [ -z "$size" ]; then size=$1; else die_code invalid "$usage"; fi; shift ;;
        esac
    done
    [ -n "$name" ] || die_code invalid "$usage"
    size=${size:-$VOLUME_SIZE}
    [[ $name =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die_code invalid "invalid volume name '$name'"
    local f=$VOLUMES_DIR/$name.ext4
    [ ! -e "$f" ] || die_code volume_exists "volume '$name' already exists"
    node_admit ${tenant:+--tenant "$tenant"} --disk "$size" || exit 1
    mkdir -p "$VOLUMES_DIR"
    truncate -s "$size" "$f"
    mkfs.ext4 -q -F -L "vol-${name:0:12}" -E lazy_itable_init=1,lazy_journal_init=1,root_owner=0:0 "$f"
    debugfs -w -R "rmdir /lost+found" "$f" >/dev/null 2>&1   # an empty root, like a docker volume
    jq -n --arg name "$name" --arg size "$size" --arg t "$tenant" \
        '{name: $name, size: $size, created: (now | todate)} + (if $t == "" then {} else {tenant: $t} end)' > "$VOLUMES_DIR/$name.json"
    log "created volume '$name' ($size, sparse${tenant:+, tenant $tenant})"
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
            local json=0 f name tenant="" owner
            while [ $# -gt 0 ]; do
                case $1 in
                    --json)   json=1; shift ;;
                    --tenant) tenant_ok "${2:?--tenant wants a name}"; tenant=$2; shift 2 ;;
                    *)        die_code invalid "unknown option $1 (usage: fcvm volume ls [--json] [--tenant NAME])" ;;
                esac
            done
            for f in "$VOLUMES_DIR"/*.ext4; do
                [ -f "$f" ] || continue
                name=$(basename "$f" .ext4); owner=$(volume_tenant "$name")
                [ -z "$tenant" ] || [ "$owner" = "$tenant" ] || continue
                jq -n --arg name "$name" --argjson size "$(stat -c %s "$f")" --arg t "$owner" \
                    --argjson used "$(( $(stat -c %b "$f") * 512 ))" \
                    --argjson vms "$(jq -n '$ARGS.positional' --args $(volume_users "$name"))" \
                    '{name: $name, size_bytes: $size, used_bytes: $used, vms: $vms, tenant: (if $t == "" then null else $t end)}'
            done | if [ $json = 1 ]; then jq -s .; else
                jq -rs '(["NAME","SIZE","USED","VMS","TENANT"] | @tsv), (.[] | [.name, "\(.size_bytes / 1073741824 | floor)G",
                    "\(.used_bytes / 1048576 | floor)M", (.vms | join(",")), (.tenant // "-")] | @tsv)' | column -t -s $'\t'; fi ;;
        rm)
            local name=${1:?usage: fcvm volume rm NAME} users
            [ -f "$VOLUMES_DIR/$name.ext4" ] || die_code volume_not_found "no volume '$name'"
            users=$(volume_users "$name" | tr '\n' ' ')
            [ -z "$users" ] || die_code in_use "volume '$name' is used by VM(s): $users"
            rm -f "$VOLUMES_DIR/$name.ext4" "$VOLUMES_DIR/$name.json"
            log "removed volume '$name'" ;;
        *) die_code invalid "usage: fcvm volume create NAME [SIZE] [--tenant NAME] | ls [--json] [--tenant NAME] | rm NAME" ;;
    esac
}

# --- images: commit and remove --------------------------------------------------

# fcvm commit VM IMAGE: the VM's writable layer becomes a read-only layer on top
# of the VM's image (docker commit). Instant: the layer disk is copied, not
# merged. A --copy VM's private disk becomes a standalone base image instead.
commit() {
    local vm=${1:?usage: fcvm commit VM IMAGE} image=${2:?usage: fcvm commit VM IMAGE}
    lock_vm "$vm"
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" && die_code vm_running "VM '$vm' is running; stop it first (fcvm stop $vm)"
    [[ $image =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die_code invalid "invalid image name '$image'"
    [ ! -e "$IMAGES_DIR/$image.json" ] || die_code image_exists "image '$image' already exists"
    local dir parent src out rc=0 tenant
    dir=$(vm_dir "$vm")
    parent=$(jq -r .image "$dir/vm.json")
    tenant=$(vm_tenant "$vm")   # the image is the VM's tenant's
    out=$IMAGES_DIR/$image.ext4
    if [ -f "$dir/disk.ext4" ]; then src=$dir/disk.ext4; else src=$dir/rw.ext4; fi
    node_admit ${tenant:+--tenant "$tenant"} --disk "$(stat -c %s "$src")" || exit 1
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
        jq --arg vm "$vm" --arg parent "$parent" --arg t "$tenant" 'del(.parent, .tenant) + {ref: "commit of \($vm) (from \($parent))"}
            + (if $t == "" then {} else {tenant: $t} end)' "$IMAGES_DIR/$parent.json" > "$IMAGES_DIR/$image.json"
    else
        jq --arg vm "$vm" --arg parent "$parent" --arg t "$tenant" 'del(.tenant) + {parent: $parent, ref: "commit of \($vm) (from \($parent))"}
            + (if $t == "" then {} else {tenant: $t} end)' "$IMAGES_DIR/$parent.json" > "$IMAGES_DIR/$image.json"
    fi
    log "committed '$vm' as image '$image' ($(du -h "$out" | cut -f1) on disk$([ -f "$dir/disk.ext4" ] || echo ", layer on $parent")${tenant:+, tenant $tenant})"
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

# An OCI image layout of IMAGE (every level flattened into one layer) in
# directory $2, made in a helper VM that boots only the initramfs (fc-init
# export mode) and writes a tar of the merged tree with owners and all.
export_layout() {   # image out-dir
    local image=$1 out=$2 chain=() drives='[]' devs=() n=0 letters=abcdefghijklmnopqrstuvwxyz img bytes=0 tmp rc=0
    mapfile -t chain < <(image_chain "$image")
    for img in "${chain[@]}"; do
        [ -f "$IMAGES_DIR/$img.ext4" ] || die "image '$img' is gone"
        drives=$(jq --arg id "l$n" --arg p "$IMAGES_DIR/$img.ext4" \
            '. + [{drive_id: $id, path_on_host: $p, is_root_device: false, is_read_only: true}]' <<<"$drives")
        devs+=("/dev/vd${letters:n:1}"); n=$((n + 1))
        bytes=$((bytes + $(stat -c %b "$IMAGES_DIR/$img.ext4") * 512))
    done
    tmp=$(mktemp -d -p "$BUILD_DIR" export.XXXXXX)
    truncate -s "$(( bytes * 12 / 10 / 1048576 + 128 ))M" "$tmp/out.ext4"
    mkfs.ext4 -q -F -E lazy_itable_init=1,lazy_journal_init=1 "$tmp/out.ext4"
    drives=$(jq --arg p "$tmp/out.ext4" '. + [{drive_id: "out", path_on_host: $p, is_root_device: false, is_read_only: false}]' <<<"$drives")
    initramfs_current || "$FCVM_ROOT/lib/build-init.sh"
    jq -n --arg kernel "$(default_kernel)" --arg initrd "$BUILD_DIR/initramfs.cpio" --argjson drives "$drives" \
        --arg args "console=ttyS0 reboot=k panic=1 quiet fcvm.export=$(IFS=,; echo "${devs[*]}") fcvm.export_out=/dev/vd${letters:n:1}" '{
        "boot-source": {kernel_image_path: $kernel, initrd_path: $initrd, boot_args: $args},
        "drives": $drives, "machine-config": {vcpu_count: 2, mem_size_mib: 512}}' > "$tmp/fc.json"
    log "exporting '$image' (${#chain[@]} level(s)) from a helper VM"
    timeout 1800 "$FIRECRACKER" --no-api --config-file "$tmp/fc.json" --log-path "$tmp/fc.log" \
        </dev/null >"$tmp/console.log" 2>&1 || rc=$?
    if [ "$(debugfs -R "cat /export-ok" "$tmp/out.ext4" 2>/dev/null)" != ok ]; then
        grep -a 'fc-init' "$tmp/console.log" >&2 || tail -20 "$tmp/console.log" >&2
        rm -rf "$tmp"; die "export failed (firecracker status $rc)"
    fi
    debugfs -R "dump /rootfs.tar $tmp/rootfs.tar" "$tmp/out.ext4" >/dev/null 2>&1
    rm -f "$tmp/out.ext4"
    python3 "$FCVM_ROOT/lib/oci_export.py" build --rootfs "$tmp/rootfs.tar" --meta "$IMAGES_DIR/$image.json" \
        --name "$image" --out "$out" --arch "$ARCH" || { rm -rf "$tmp"; exit 1; }
    rm -rf "$tmp"
}

# fcvm export IMAGE FILE.tar: the image as an OCI image archive (one layer):
# `docker load`, `podman load`, `fcvm import oci-archive:FILE` read it.
export_cmd() {
    local usage="usage: fcvm export IMAGE FILE.tar" image=${1:?$usage} file=${2:?$usage} dir
    image_json "$image" >/dev/null
    host_path_ok "$file" "export"
    [ ! -e "$file" ] || die "$file exists"
    dir=$(mktemp -d -p "$BUILD_DIR" oci.XXXXXX)
    export_layout "$image" "$dir"
    tar -C "$dir" -cf "$file" . || { rm -rf "$dir" "$file"; die "cannot write $file"; }
    rm -rf "$dir"
    log "exported '$image' to $file ($(du -h "$file" | cut -f1)). Load it: docker load < $file, or fcvm import oci-archive:$file"
}

# fcvm push IMAGE REF: the image to a registry (REF as for import: ghcr.io/org/app:tag).
push_cmd() {
    local usage="usage: fcvm push IMAGE REGISTRY/REPO[:TAG]" image=${1:?$usage} ref=${2:?$usage} dir
    image_json "$image" >/dev/null
    dir=$(mktemp -d -p "$BUILD_DIR" oci.XXXXXX)
    export_layout "$image" "$dir"
    python3 "$FCVM_ROOT/lib/oci_export.py" push "$dir" "$ref" || { rm -rf "$dir"; exit 1; }
    rm -rf "$dir"
}

# fcvm squash IMAGE NEW: IMAGE's committed layers merged into one layer on the
# same base image (keeps layer chains, and so drive counts, small).
squash() {
    local image=${1:?usage: fcvm squash IMAGE NEW} new=${2:?usage: fcvm squash IMAGE NEW}
    image_json "$image" >/dev/null
    [[ $new =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die_code invalid "invalid image name '$new'"
    [ ! -e "$IMAGES_DIR/$new.json" ] || die_code image_exists "image '$new' already exists"
    local chain=() layers=() i tenant
    mapfile -t chain < <(image_chain "$image")
    [ ${#chain[@]} -gt 2 ] || die "'$image' has ${#chain[@]} level(s); nothing to squash"
    for ((i = ${#chain[@]} - 2; i >= 0; i--)); do layers+=("${chain[i]}"); done
    tenant=$(image_tenant "$image")   # the result stays the image's tenant's (its json is copied below)
    node_admit ${tenant:+--tenant "$tenant"} --disk "$(stat -c %s "$IMAGES_DIR/$image.ext4")" || exit 1
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
    local egress=0
    if [ -d "$CACHE_DIR/egress" ]; then egress=$(du -sm "$CACHE_DIR/egress" | cut -f1); rm -rf "$CACHE_DIR/egress"; fi
    log "removed $removed build cache image(s), $((freed / 1048576)) MiB freed; the egress mirror's $egress MiB too"
}

# Per-machine identity a booted systemd VM leaves on its disk. Removed from a
# committed layer (the base image's blank versions show through again) so every
# VM from the image generates its own at first boot.
scrub_identity() {   # disk path-prefix ("/upper" for a layer, "" for a --copy disk)
    local disk=$1 pre=$2 f d sub cmds=()
    for f in /etc/machine-id /var/lib/dbus/machine-id /var/lib/systemd/random-seed \
             /etc/ssh/ssh_host_{rsa,ecdsa,ed25519}_key{,.pub} /root/.ssh/authorized_keys; do
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
    [ -z "$users" ] || die_code in_use "image '$image' is used by: $users"
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
        need_agent "$vm" cp
        host_path_ok "$src" "cp"
        [ -e "$src" ] || die "no such file: $src"
        vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running"
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
        host_path_ok "$dst" "cp"
        vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running"
        need_agent "$vm" cp
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
        die_code invalid "$usage"
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
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running (snapshots capture a running VM's memory)"
    need_agent "$vm" "a snapshot (its forks)"
    [[ $name =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die_code invalid "invalid snapshot name '$name'"
    local dir=$SNAPSHOTS_DIR/$name vdir disk drive t0 ms
    [ ! -e "$dir" ] || die_code snapshot_exists "snapshot '$name' already exists"
    vdir=$(vm_dir "$vm")
    if jq -e '[.volumes[]? | select(endswith(":ro") | not)] | length > 0' "$vdir/vm.json" >/dev/null; then
        die "VM '$vm' has read-write volumes; a fork can't share them (use :ro volumes, or copy the data in)"
    fi
    [ "$(jq '.shares // [] | length' "$vdir/vm.json")" = 0 ] ||
        die "VM '$vm' has live host directories mounted; their connections can't be cloned into a fork"
    local jailed=false vmstate=$dir/vmstate memf=$dir/mem tenant
    if vm_jailed "$vm"; then   # Firecracker writes inside its chroot; the helper moves the files out
        jailed=true vmstate=/snap.vmstate memf=/snap.mem
    fi
    if [ -f "$vdir/disk.ext4" ]; then disk=disk.ext4 drive=root; else disk=rw.ext4 drive=rw; fi
    tenant=$(vm_tenant "$vm")   # the snapshot, and its forks by default, are the VM's tenant's
    node_admit ${tenant:+--tenant "$tenant"} --disk "$(( $(jq -r .mem_mib "$vdir/vm.json") + ($(stat -c %s "$vdir/$disk") + 1048575) / 1048576 ))M" || exit 1
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
        --argjson jail "$jailed" --arg t "$tenant" \
        '{source: $src, disk: $disk, drive_id: $drive, source_disk: $srcdisk, paused_ms: $ms,
          firecracker: $fc, jail: $jail, created: (now | todate)} + (if $t == "" then {} else {tenant: $t} end)' > "$dir/meta.json"
    log "snapshot '$name' of '$vm' (paused ${ms} ms; memory $(du -h "$dir/mem" | cut -f1) on disk${tenant:+; tenant $tenant}). Fork it: fcvm fork $name"
}

# Boot VM $2 from snapshot $1: its own copy of the disk, a free tap, its own
# vsock socket, then a new address, MAC and hostname via the agent.
fork_one() {
    local snap=$1 vm=$2 sdir=$SNAPSHOTS_DIR/$1 dir disk drive mode mac="" gw="" tap="" t0 i pid
    dir=$(vm_dir "$vm")
    sock_room "$vm"
    lock_vm "$vm"
    vm_exists "$vm" && die_code vm_exists "VM '$vm' already exists"
    if [ "$(jq -r '.jail // false' "$sdir/meta.json")" = true ]; then
        # Inside a jail every path is the same (/driveN.ext4, /vsock.sock, tap0 in
        # its own netns), so a fresh jail with this VM's own disk restores as is.
        t0=$(date +%s%N)
        disk=$(jq -r .disk "$sdir/meta.json")
        mkdir -p "$dir"
        jq --arg snap "$snap" --argjson labels "${FORK_LABELS:-{\}}" --arg lease "${FORK_LEASE:-}" \
            '. + {ephemeral: false, ports: [], restored_from: $snap, created: (now | todate), labels: $labels}
             | del(.lease_until) + (if $lease == "" then {} else {lease_until: ($lease | tonumber)} end)' \
            "$sdir/vm.json" > "$dir/vm.json"
        node_admit --disk "$(stat -c %s "$sdir/$disk")" "$vm" || { rm -rf "$dir"; exit 1; }   # start checks again, as it must
        emit_event vm.created "$vm" fork "$(jq -nc --arg s "$snap" '{restored_from: $s}')"
        cp --sparse=always "$sdir/$disk" "$dir/$disk"
        RESTORE=$snap QUIET_START=1 start "$vm"
        log "forked '$vm' from '$snap' in $(( ($(date +%s%N) - t0) / 1000000 )) ms (jailed$([ -f "$dir/ip" ] && echo ", ip $(cat "$dir/ip")"))"
        return
    fi
    disk=$(jq -r .disk "$sdir/meta.json"); drive=$(jq -r .drive_id "$sdir/meta.json")
    t0=$(date +%s%N)
    mkdir -p "$dir"
    jq --arg snap "$snap" --argjson labels "${FORK_LABELS:-{\}}" --arg lease "${FORK_LEASE:-}" \
        '. + {ephemeral: false, ports: [], restored_from: $snap, created: (now | todate), labels: $labels}
         | del(.lease_until) + (if $lease == "" then {} else {lease_until: ($lease | tonumber)} end)' \
        "$sdir/vm.json" > "$dir/vm.json"
    node_admit --disk "$(stat -c %s "$sdir/$disk")" "$vm" || { rm -rf "$dir"; exit 1; }
    emit_event vm.created "$vm" fork "$(jq -nc --arg s "$snap" '{restored_from: $s}')"
    cp --sparse=always "$sdir/$disk" "$dir/$disk"
    DIR=$dir IP=""
    mode=$(jq -r '.net.mode // "full"' "$dir/vm.json")
    case $mode in
        restricted)
            claim_tap fcrtap || { rm -rf "$dir"; die_code no_network_slot "no free restricted tap; run: fcvm net-up"; }
            IP=$NET_R_PREFIX.$((10 + TAP_INDEX)) gw=$NET_R_PREFIX.1 tap=fcrtap$TAP_INDEX
            mac=$(printf '06:01:%02x:%02x:%02x:%02x' ${IP//./ })
            write_policy "$vm" "$IP"; ensure_proxy ;;
        none) ;;
        *)
            claim_tap fctap || { rm -rf "$dir"; die_code no_network_slot "no free tap; run: fcvm net-up"; }
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
    [ -z "$pid" ] || record_pid_id "$vm" "$pid"; rm -f "$dir/starting"
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
         vsock_override: {uds_path: $vsock}, resume_vm: false}
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
    emit_event vm.started "$vm" fork "$(jq -nc --arg pid "$pid" --arg ip "$IP" '{pid: ($pid | tonumber), ip: (if $ip == "" then null else $ip end)}')"
    log "forked '$vm' from '$snap' in ${ms} ms${IP:+ (ip $IP)}"
}

fork_cmd() {
    local usage="usage: fcvm fork SNAPSHOT [NAME] [-n COUNT] [--label KEY=VALUE]... [--tenant NAME] [--lease UNTIL]" snap="" name="" count=1 labels=() lease=""
    while [ $# -gt 0 ]; do
        case $1 in
            -n|--count) [[ ${2:-} =~ ^[1-9][0-9]*$ ]] || die_code invalid "$usage"; count=$2; shift 2 ;;
            --label)    label_ok "${2:?$usage}"; labels+=("$2"); shift 2 ;;
            --tenant)   tenant_ok "${2:?$usage}"; labels+=("tenant=$2"); shift 2 ;;
            --lease)    lease=$(parse_until --lease "${2:?$usage}"); shift 2 ;;
            -*)         die_code invalid "unknown option $1 ($usage)" ;;
            *)          if [ -z "$snap" ]; then snap=$1; else name=$1; fi; shift ;;
        esac
    done
    [ -n "$snap" ] || die_code invalid "$usage"
    [ -f "$SNAPSHOTS_DIR/$snap/meta.json" ] || die_code snapshot_not_found "no snapshot '$snap' (see: fcvm snapshot ls)"
    # A tenant's snapshot forks only for that tenant: the forks take it unless
    # --tenant names it; another tenant's request is refused.
    local owner tenant; owner=$(snapshot_tenant "$snap") tenant=$(labels_tenant "${labels[@]}")
    if [ -n "$owner" ] && [ -z "$tenant" ]; then labels+=("tenant=$owner")
    else own_ok "$tenant" snapshot "$snap" "$owner"; fi
    [ "$(jq -r '.jail // false' "$SNAPSHOTS_DIR/$snap/meta.json")" = true ] ||
        node_refuse "snapshot '$snap' is of an unjailed VM, and its forks would run unjailed"
    [ "$(jq '.ports | length' "$SNAPSHOTS_DIR/$snap/vm.json")" = 0 ] || use_daemon ||   # (the service's log says it)
        warn "published ports aren't carried over to forks (they'd conflict with the source)"
    if use_daemon; then
        local reply rc=0
        reply=$(daemon_api POST "snapshots/$snap/fork" "$(jq -n --arg n "$name" --argjson c "$count" --argjson l "$(labels_json "${labels[@]}")" --arg lease "$lease" \
            '{count: $c, labels: $l} + (if $n == "" then {} else {name: $n} end) + (if $lease == "" then {} else {lease: ($lease | tonumber)} end)')") || rc=$?
        if [ $rc = 0 ]; then jq -r .log <<<"$reply" | sed "/^$/d" >&2; return; fi
        [ $rc = 2 ] || exit 1
        warn "the fcvm service isn't answering; forking from this terminal"
    fi
    local base=${name:-$snap-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')} i
    FORK_LABELS=$(labels_json "${labels[@]}") FORK_LEASE=$lease
    refuse_if_cordoned
    if [ "$count" = 1 ]; then
        fork_one "$snap" "$base"
    else
        for ((i = 1; i <= count; i++)); do fork_one "$snap" "$base-$i"; done
    fi
}

# fcvm snapshot export NAME FILE.tar: a snapshot (memory, device state, disk,
# settings) as a sparse tar, to fork it on another fcvm host that has the same
# images and a compatible Firecracker. fcvm snapshot import FILE.tar [NAME]
# brings it back.
snapshot_export() {
    local usage="usage: fcvm snapshot export NAME FILE.tar" name=${1:?$usage} file=${2:?$usage} dir
    dir=$SNAPSHOTS_DIR/$name
    [ -f "$dir/meta.json" ] || die_code snapshot_not_found "no snapshot '$name' (see: fcvm snapshot ls)"
    host_path_ok "$file" "snapshot export"
    [ ! -e "$file" ] || die "$file exists"
    local chain=() i
    mapfile -t chain < <(image_chain "$(jq -r .image "$dir/vm.json")")
    jq -n --arg fcvm "$(fcvm_version)" --arg kernel "$(basename "$(default_kernel)")" --argjson images "$(json_list "${chain[@]}")" \
        --arg disk "$(jq -r .disk "$dir/meta.json")" \
        '{fcvm: $fcvm, kernel: $kernel, images: $images, disk: $disk, exported: (now | todate)}' > "$dir/export.json"
    # -S keeps the memory file sparse (untouched guest pages take no space)
    tar -S -C "$dir" -cf "$file" vmstate mem "$(jq -r .disk "$dir/meta.json")" vm.json meta.json export.json ||
        { rm -f "$file" "$dir/export.json"; die "cannot write $file"; }
    rm -f "$dir/export.json"
    log "exported snapshot '$name' to $file ($(du -h "$file" | cut -f1)); it needs image(s) $(IFS=,; echo "${chain[*]}") where it's imported"
}

snapshot_import() {
    local usage="usage: fcvm snapshot import FILE.tar [NAME] [--tenant NAME]" file="" name="" tenant="" dir tmp
    while [ $# -gt 0 ]; do
        case $1 in
            --tenant) tenant_ok "${2:?$usage}"; tenant=$2; shift 2 ;;
            -*)       die_code invalid "unknown option $1 ($usage)" ;;
            *)        if [ -z "$file" ]; then file=$1; elif [ -z "$name" ]; then name=$1; else die_code invalid "$usage"; fi; shift ;;
        esac
    done
    [ -n "$file" ] || die_code invalid "$usage"
    host_path_ok "$file" "snapshot import"
    [ -f "$file" ] || die "no such file: $file"
    tmp=$(mktemp -d -p "$SNAPSHOTS_DIR" .import.XXXXXX) || { mkdir -p "$SNAPSHOTS_DIR"; tmp=$(mktemp -d -p "$SNAPSHOTS_DIR" .import.XXXXXX); }
    tar -S -C "$tmp" -xf "$file" 2>/dev/null && [ -f "$tmp/export.json" ] && [ -f "$tmp/meta.json" ] ||
        { rm -rf "$tmp"; die "$file isn't an fcvm snapshot export"; }
    [ -n "$name" ] || name=$(basename "${file%.tar}")
    [[ $name =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || { rm -rf "$tmp"; die_code invalid "invalid snapshot name '$name'"; }
    [ ! -e "$SNAPSHOTS_DIR/$name" ] || { rm -rf "$tmp"; die_code snapshot_exists "snapshot '$name' already exists"; }
    local img missing=()
    for img in $(jq -r '.images[]' "$tmp/export.json"); do [ -f "$IMAGES_DIR/$img.json" ] || missing+=("$img"); done
    [ ${#missing[@]} -eq 0 ] || { rm -rf "$tmp"; die "the snapshot's VM boots from image(s) not present here: ${missing[*]} (fcvm import them under those names first)"; }
    local fc theirs; fc=$("$FIRECRACKER" --version | head -1); theirs=$(jq -r .firecracker "$tmp/meta.json")
    [ "$fc" = "$theirs" ] || warn "the snapshot was taken with $theirs; this host runs $fc. Forks may fail to restore"
    # An unjailed snapshot's vmstate names the source VM's disk path; a fork
    # stands in for it with a link, which must be possible here.
    local srcdisk; srcdisk=$(jq -r '.source_disk // empty' "$tmp/meta.json")
    if [ "$(jq -r '.jail // false' "$tmp/meta.json")" != true ] && [ -n "$srcdisk" ] && [ ! -e "$srcdisk" ]; then
        if ! { mkdir -p "${srcdisk%/*}" && rmdir "${srcdisk%/*}" 2>/dev/null || [ -d "${srcdisk%/*}" ]; } 2>/dev/null; then
            warn "forks of this snapshot need a stand-in for its source disk at $srcdisk, which can't be created here (snapshots of --jail VMs don't have this limitation)"
        fi
    fi
    # The snapshot is --tenant's (its VM's tenant label follows), else public
    node_admit ${tenant:+--tenant "$tenant"} --disk "$(( ($(stat -c %s "$tmp/mem") + $(stat -c %s "$tmp/$(jq -r .disk "$tmp/meta.json")") + 1048575) / 1048576 ))M" ||
        { rm -rf "$tmp"; exit 1; }
    jq --arg file "$(basename "$file")" --arg t "$tenant" 'del(.tenant) + {imported_from: $file} + (if $t == "" then {} else {tenant: $t} end)' \
        "$tmp/meta.json" > "$tmp/meta.json.tmp" && mv "$tmp/meta.json.tmp" "$tmp/meta.json"
    jq --arg t "$tenant" '.labels = ((.labels // {}) | del(.tenant)) + (if $t == "" then {} else {tenant: $t} end)' \
        "$tmp/vm.json" > "$tmp/vm.json.tmp" && mv "$tmp/vm.json.tmp" "$tmp/vm.json"
    rm -f "$tmp/export.json"
    mv "$tmp" "$SNAPSHOTS_DIR/$name"
    log "imported snapshot '$name' ($(jq -r .image "$SNAPSHOTS_DIR/$name/vm.json")${tenant:+, tenant $tenant}). Fork it: fcvm fork $name"
}

snapshot_cmd() {
    case ${1:-} in
        export) shift; snapshot_export "$@" ;;
        import) shift; snapshot_import "$@" ;;
        ls)
            local json=0 d tenant=""; shift
            while [ $# -gt 0 ]; do
                case $1 in
                    --json)   json=1; shift ;;
                    --tenant) tenant_ok "${2:?--tenant wants a name}"; tenant=$2; shift 2 ;;
                    *)        die_code invalid "unknown option $1 (usage: fcvm snapshot ls [--json] [--tenant NAME])" ;;
                esac
            done
            for d in "$SNAPSHOTS_DIR"/*/; do
                [ -f "$d/meta.json" ] || continue
                [ -z "$tenant" ] || [ "$(snapshot_tenant "$(basename "$d")")" = "$tenant" ] || continue
                jq --arg name "$(basename "$d")" --argjson mem "$(( $(stat -c %b "$d/mem") * 512 ))" \
                   --argjson disk "$(( $(stat -c %b "$d"/*.ext4 | head -1) * 512 ))" --slurpfile vm "$d/vm.json" \
                   '{name: $name, source, image: $vm[0].image, type: $vm[0].type, mem_mib: $vm[0].mem_mib,
                     mem_bytes_on_disk: $mem, disk_bytes: $disk, paused_ms, created, tenant: (.tenant // null)}' "$d/meta.json"
            done | if [ $json = 1 ]; then jq -s .; else
                jq -rs '(["NAME","SOURCE","IMAGE","MEM","DISK","CREATED","TENANT"] | @tsv),
                    (.[] | [.name, .source, .image, "\(.mem_bytes_on_disk / 1048576 | floor)M/\(.mem_mib)M",
                            "\(.disk_bytes / 1048576 | floor)M", .created, (.tenant // "-")] | @tsv)' | column -t -s $'\t'; fi ;;
        rm)
            local name=${2:?usage: fcvm snapshot rm NAME}
            [ -d "$SNAPSHOTS_DIR/$name" ] || die_code snapshot_not_found "no snapshot '$name'"
            rm -rf "${SNAPSHOTS_DIR:?}/$name"; log "removed snapshot '$name'" ;;
        ""|-h|--help) die_code invalid "usage: fcvm snapshot VM NAME | fcvm snapshot ls [--json] [--tenant NAME] | fcvm snapshot rm NAME | fcvm snapshot export NAME FILE.tar | fcvm snapshot import FILE.tar [NAME] [--tenant NAME]" ;;
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
    local usage="usage: fcvm mount VM HOST/DIR[:GUEST/PATH][:ro]"
    local vm=${1:?$usage} spec=${2:?$usage} dir hdir gpath ro port pid i
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    parse_share "$spec"
    hdir=$SHARE_HOST gpath=$SHARE_GUEST ro=""
    [ "$SHARE_RO" = false ] || ro=true
    dir=$(vm_dir "$vm")
    jq -e --arg g "$gpath" '(.shares // []) | any(.path == $g)' "$dir/vm.json" >/dev/null &&
        die "'$vm' already has something mounted at $gpath (fcvm umount $vm $gpath first)"
    if vm_running "$vm"; then
        need_agent "$vm" "mounting on a running VM (stop it first; it's mounted from the next start)"
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
    local usage="usage: fcvm umount VM GUEST/PATH|HOST/DIR"
    local vm=${1:?$usage} arg=${2:?$usage} dir line gpath
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    dir=$(vm_dir "$vm")
    # by guest path (as given, or a home directory in its ~/ form), or by host directory
    gpath=$(jq -r --arg g "$arg" --arg gh "$(guest_arg "$arg")" --arg h "$(realpath -m "${arg/#\~/$HOME}")" \
        '[(.shares // [])[] | select(.path == $g or .path == $gh or .host == $h)][0].path // empty' "$dir/vm.json")
    [ -n "$gpath" ] || die "nothing from the host is mounted at $arg in '$vm'"
    if vm_running "$vm"; then
        need_agent "$vm" "unmounting on a running VM (stop it first)"
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

# --- published ports: fcvm publish / unpublish ----------------------------------------

# fcvm publish VM [BIND:][HOSTPORT]:GUESTPORT: publish a port of a VM, running
# or not. On a running VM the forwarder starts at once; either way the port is
# saved for the next start. Without HOSTPORT a free one is picked. Prints the
# spec (for scripts: `fcvm publish box :8000` -> 43127:8000).
publish_cmd() {
    local usage="usage: fcvm publish VM [BIND:][HOSTPORT]:GUESTPORT   (no HOSTPORT: a free one is picked)"
    local vm=${1:?$usage} spec=${2:?$usage} dir bind hport gport
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    [[ $spec =~ ^(([0-9.]+):)?([0-9]*):([0-9]+)(/tcp)?$ ]] || die_code invalid "bad port spec '$spec' ($usage)"
    bind=${BASH_REMATCH[2]} hport=${BASH_REMATCH[3]} gport=${BASH_REMATCH[4]}
    if [ -z "${FCVM_DAEMON:-}" ] && vm_running "$vm" && use_daemon; then   # its forwarder must live where the VM does
        local reply rc=0
        reply=$(daemon_api POST "vms/$vm/ports" "$(jq -n --arg p "$spec" '{port: $p}')") || rc=$?
        if [ $rc = 0 ]; then jq -r '.ports[-1]' <<<"$reply"; return; fi
        [ $rc = 2 ] || [ $rc = 3 ] || exit 1
        [ $rc = 2 ] || daemon_too_old "the forwarder runs from this terminal and ends with it"
    fi
    lock_vm "$vm"
    dir=$(vm_dir "$vm")
    [ "$(jq -r '.net.mode // "full"' "$dir/vm.json")" != none ] || die_code no_network "'$vm' has no network (--net none); nothing to publish"
    if [ -z "$hport" ]; then
        hport=$(python3 -c 'import socket, sys; s = socket.socket(); s.bind((sys.argv[1], 0)); print(s.getsockname()[1])' "${bind:-127.0.0.1}") ||
            die "cannot pick a free port on ${bind:-127.0.0.1}"
    fi
    spec="${bind:+$bind:}$hport:$gport"
    jq -e --arg p "$spec" '.ports | index($p)' "$dir/vm.json" >/dev/null && die "'$vm' already publishes $spec"
    local live=""
    if vm_running "$vm"; then
        [ -s "$dir/ip" ] || die_code no_network "'$vm' is running without a network; nothing to publish"
        DIR=$dir forward_port "$(vm_pid "$vm")" "$(cat "$dir/ip")" "$spec" || die "cannot publish $spec"
        live=" (live)"
    fi
    jq --arg p "$spec" '.ports = ((.ports // []) + [$p])' "$dir/vm.json" > "$dir/vm.json.tmp" && mv "$dir/vm.json.tmp" "$dir/vm.json"
    log "published $spec -> '$vm' port $gport${live:-" (from its next start)"}"
    echo "$spec"
}

# fcvm unpublish VM HOSTPORT|SPEC: the opposite, by host port or the full spec.
unpublish_cmd() {
    local usage="usage: fcvm unpublish VM HOSTPORT|[BIND:]HOSTPORT:GUESTPORT"
    local vm=${1:?$usage} arg=${2:?$usage} dir spec
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    if [ -z "${FCVM_DAEMON:-}" ] && vm_running "$vm" && use_daemon; then
        local rc=0
        daemon_api DELETE "vms/$vm/ports?port=$arg" >/dev/null || rc=$?
        if [ $rc = 0 ]; then log "unpublished $arg from '$vm'"; return; fi
        [ $rc = 2 ] || [ $rc = 3 ] || exit 1
    fi
    lock_vm "$vm"
    dir=$(vm_dir "$vm")
    spec=$(jq -r --arg a "$arg" '[(.ports // [])[] | select(. == $a or (split(":") | .[-2]) == $a)][0] // empty' "$dir/vm.json")
    [ -n "$spec" ] || die "'$vm' doesn't publish $arg (fcvm ls shows its ports)"
    ! vm_running "$vm" || stop_portfwd "$dir" "$spec"
    jq --arg p "$spec" '.ports -= [$p]' "$dir/vm.json" > "$dir/vm.json.tmp" && mv "$dir/vm.json.tmp" "$dir/vm.json"
    log "unpublished $spec from '$vm'"
}

# --- warm pools: fcvm pool ----------------------------------------------------------------
# A pool (vms/.pools/NAME.json: template or snapshot, size, jail) is a set of
# VMs booted ahead of time, each marked with vms/VM/pooled = NAME, that the
# supervisor in fcvm serve keeps at size. `pool claim` hands one out: the
# marker goes, the VM keeps its name, and a sandbox is ready in milliseconds
# instead of a boot. The MCP server claims from a template's pool on its own.

pool_file() { echo "$VMS_DIR/.pools/$1.json"; }
pool_members() {   # name [running]: pooled VMs, unclaimed
    local d vm
    for d in "$VMS_DIR"/*/; do
        vm=$(basename "$d")
        [ "$(cat "$d/pooled" 2>/dev/null)" = "$1" ] || continue
        [ -z "${2:-}" ] || vm_running "$vm" || continue
        echo "$vm"
    done
}

pool_cmd() {
    local usage="usage: fcvm pool set NAME (--template T | --snapshot S) --size N [--jail] | fcvm pool ls [--json] | fcvm pool rm NAME | fcvm pool claim NAME [--ttl D] [--idle-stop D] [--label KEY=VALUE]... [--tenant NAME] [--lease UNTIL]"
    local sub=${1:-ls}; [ $# -eq 0 ] || shift
    case $sub in
        set)
            local name=${1:?$usage} tpl="" snap="" size="" jail=false f; shift
            [[ $name =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || die_code invalid "invalid pool name '$name'"
            while [ $# -gt 0 ]; do
                case $1 in
                    --template) tpl=${2:?$usage}; shift 2 ;;
                    --snapshot) snap=${2:?$usage}; shift 2 ;;
                    --size)     [[ ${2:-} =~ ^[0-9]+$ ]] || die_code invalid "--size wants a number"; size=$2; shift 2 ;;
                    --jail)     jail=true; shift ;;
                    *)          die_code invalid "unknown option $1 ($usage)" ;;
                esac
            done
            f=$(pool_file "$name")
            [ -n "$tpl$snap" ] || tpl=$(jq -r '.template // empty' "$f" 2>/dev/null) snap=$(jq -r '.snapshot // empty' "$f" 2>/dev/null)
            [ -n "$size" ] || size=$(jq -r '.size // empty' "$f" 2>/dev/null)
            [ -n "$tpl$snap" ] && [ -n "$size" ] || die_code invalid "$usage"
            [ -z "$tpl" ] || [ -z "$snap" ] || die "a pool is from a template or a snapshot, not both"
            [ -z "$tpl" ] || python3 "$FCVM_ROOT/lib/templates.py" show "$tpl" >/dev/null || exit 1
            [ -z "$snap" ] || [ -f "$SNAPSHOTS_DIR/$snap/meta.json" ] || die_code snapshot_not_found "no snapshot '$snap'"
            mkdir -p "$VMS_DIR/.pools"
            jq -n --arg name "$name" --arg tpl "$tpl" --arg snap "$snap" --argjson size "$size" --argjson jail "$jail" \
                '{name: $name, template: (if $tpl == "" then null else $tpl end), snapshot: (if $snap == "" then null else $snap end),
                  size: $size, jail: $jail, created: (now | todate)}' > "$f.tmp" && mv "$f.tmp" "$f"
            emit_event pool.set "" "" "$(jq -c '{pool: .name, template, snapshot, size, jail}' "$f")"
            log "pool '$name': $size VM(s) from ${tpl:+template $tpl}${snap:+snapshot $snap}$([ "$jail" = true ] && echo ", jailed") (filled by fcvm serve / the fcvm service)" ;;
        ls)
            local f json=0; [ "${1:-}" = --json ] && json=1
            for f in "$VMS_DIR"/.pools/*.json; do
                [ -f "$f" ] || continue
                jq --argjson ready "$(json_list $(pool_members "$(basename "$f" .json)" running))" \
                   --argjson all "$(json_list $(pool_members "$(basename "$f" .json)"))" \
                   '. + {ready: ($ready | length), vms: $all}' "$f"
            done | if [ $json = 1 ]; then jq -s .; else
                jq -rs '(["NAME","SOURCE","SIZE","READY","JAIL"] | @tsv),
                    (.[] | [.name, (if .template then "template \(.template)" else "snapshot \(.snapshot)" end), .size, .ready, .jail] | @tsv)' |
                column -t -s $'\t'; fi ;;
        rm)
            local name=${1:?$usage} vm
            [ -f "$(pool_file "$name")" ] || die_code pool_not_found "no pool '$name'"
            rm -f "$(pool_file "$name")"
            for vm in $(pool_members "$name"); do
                QUIET_STOP=1 "$FCVM_ROOT/fcvm" stop "$vm" >/dev/null 2>&1 || true
                emit_event vm.deleted "$vm" pool
                rm -rf "$(vm_dir "$vm")" "$VMS_DIR/.locks/vm-$vm"
            done
            emit_event pool.removed "" "" "$(jq -nc --arg p "$name" '{pool: $p}')"
            log "removed pool '$name' and its unclaimed VMs (claimed ones are ordinary VMs now)" ;;
        claim)
            local name=${1:?$usage} ttl="" idle="" vm="" d plock labels=() tenant="" owner; shift
            while [ $# -gt 0 ]; do
                case $1 in
                    --ttl)       ttl=${2:?$usage}; shift 2 ;;
                    --idle-stop) idle=${2:?$usage}; shift 2 ;;
                    --label)     label_ok "${2:?$usage}"; labels+=("--label" "$2"); shift 2 ;;
                    --tenant)    tenant_ok "${2:?$usage}"; tenant=$2; shift 2 ;;
                    --lease)     parse_until --lease "${2:?$usage}" >/dev/null; labels+=("--lease" "$2"); shift 2 ;;
                    *)           die_code invalid "unknown option $1 ($usage)" ;;
                esac
            done
            [ -f "$(pool_file "$name")" ] || die_code pool_not_found "no pool '$name' (see: fcvm pool ls)"
            refuse_if_cordoned
            mkdir -p "$VMS_DIR/.locks"
            exec {plock}>"$VMS_DIR/.locks/pool-$name"; flock "$plock"
            for d in $(pool_members "$name" running); do vm=$d; break; done
            [ -n "$vm" ] || die_code pool_empty "pool '$name' has no VM ready right now (fcvm serve fills it to $(jq .size "$(pool_file "$name")")); create one the usual way meanwhile"
            d=$(vm_dir "$vm")
            # settings first: if they fail, the VM stays in the pool. A pooled VM
            # without a tenant is public: the claim gives it one, within that
            # tenant's quota; one with a tenant goes only to that tenant.
            if [ -n "$tenant" ]; then
                owner=$(jq -r '.labels.tenant // empty' "$d/vm.json")
                [ -z "$owner" ] || [ "$owner" = "$tenant" ] || die_code invalid "pool '$name' belongs to tenant '$owner', not '$tenant'"
                node_admit --claim --tenant "$tenant" "$vm" || exit 1
            fi
            [ -z "$ttl$idle" ] && [ ${#labels[@]} -eq 0 ] || QUIET_UPDATE=1 update "$vm" ${ttl:+--ttl "$ttl"} ${idle:+--idle-stop "$idle"} "${labels[@]}"
            [ -z "$tenant" ] || { jq --arg t "$tenant" '.labels = ((.labels // {}) + {tenant: $t})' "$d/vm.json" > "$d/vm.json.tmp" && mv "$d/vm.json.tmp" "$d/vm.json"; }
            rm -f "$d/pooled"; date +%s > "$d/claimed"; touch_activity "$vm"
            exec {plock}>&-
            emit_event pool.claimed "$vm" "" "$(jq -nc --arg p "$name" --arg t "$tenant" '{pool: $p} + (if $t == "" then {} else {tenant: $t} end)')"
            log "claimed '$vm' from pool '$name'"
            echo "$vm" ;;
        _fill)   # one more VM for the pool (the supervisor's job)
            local name=${1:?$usage} f tpl snap jail vm opts=()
            f=$(pool_file "$name"); [ -f "$f" ] || die_code pool_not_found "no pool '$name'"
            refuse_if_cordoned
            tpl=$(jq -r '.template // empty' "$f"); snap=$(jq -r '.snapshot // empty' "$f"); jail=$(jq -r '.jail // false' "$f")
            vm="$name-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
            [ "$jail" != true ] || opts+=(--jail)
            if [ -n "$snap" ]; then
                "$FCVM_ROOT/fcvm" fork "$snap" "$vm" >/dev/null
                echo "$name" > "$(vm_dir "$vm")/pooled"
            else
                create "$vm" --template "$tpl" "${opts[@]}" >/dev/null
                echo "$name" > "$(vm_dir "$vm")/pooled"
                QUIET_START=1 start "$vm" >/dev/null
            fi
            echo "$vm" ;;
        *) die_code invalid "$usage" ;;
    esac
}

# --- memory balloon: fcvm balloon ----------------------------------------------------

# fcvm balloon VM [MiB]: take memory back from a running VM (the guest gives
# up MiB of its RAM to the balloon, and the host frees those pages), or
# return it with 0. Without a size: the balloon and the guest's memory view.
balloon_cmd() {
    local usage="usage: fcvm balloon VM [MiB|0]" vm=${1:?$usage} mib=${2:-} dir mem cur
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" || die_code vm_not_running "VM '$vm' is not running"
    dir=$(vm_dir "$vm")
    if ! cur=$(fc_api_get "$dir/fc.sock" /balloon 2>/dev/null); then
        die "'$vm' has no balloon device (started by an fcvm before 0.7); restart it to get one"
    fi
    mem=$(jq -r .mem_mib "$dir/vm.json")
    if [ -z "$mib" ]; then
        local stats; stats=$(fc_api_get "$dir/fc.sock" /balloon/statistics 2>/dev/null || echo '{}')
        jq -n --argjson b "$cur" --argjson s "$stats" --argjson mem "$mem" '
            {balloon_mib: $b.amount_mib, mem_mib: $mem,
             guest: ($s | if .total_memory then {total_mib: (.total_memory / 1048576 | floor), free_mib: (.free_memory / 1048576 | floor),
                           available_mib: (.available_memory / 1048576 | floor)} else null end)}'
        return
    fi
    [[ $mib =~ ^[0-9]+$ ]] && [ "$mib" -lt "$mem" ] || die_code invalid "balloon size wants MiB below the VM's memory ($mem)"
    fc_api "$dir/fc.sock" PATCH /balloon "$(jq -n --argjson m "$mib" '{amount_mib: $m}')" || die "cannot resize the balloon"
    if [ "$mib" = 0 ]; then log "'$vm': balloon deflated; it has its full $mem MiB again"
    else log "'$vm': balloon at $mib MiB ($((mem - mib)) MiB left to the guest; the host frees the rest as the guest gives it up)"; fi
}
# Firecracker API GET; prints the JSON body.
fc_api_get() {   # socket path
    local out code
    out=$(curl -sS --unix-socket "$1" "http://localhost$2" -w '\n%{http_code}') || return 1
    code=${out##*$'\n'}
    [[ $code == 2* ]] || return 1
    printf '%s' "${out%$'\n'*}"
}

# --- machine-readable state ------------------------------------------------------

# Host memory a running VM actually uses: its Firecracker process's resident
# set (guest RAM is only backed once the guest touches it), in bytes.
vm_rss() {
    awk '/^VmRSS:/ {print $2 * 1024}' "/proc/$1/status" 2>/dev/null || true
}
set_lease() {   # vm until-or-empty
    local f=$VMS_DIR/$1/vm.json
    jq --arg l "$2" 'if $l == "" then del(.lease_until) else .lease_until = ($l | tonumber) end' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
}

# fcvm cordon [REASON] / uncordon: refuse new VMs (create, fork, pool claim and
# refills) until uncordoned; running VMs and restart policies aren't affected.
cordon_cmd() {
    mkdir -p "$VMS_DIR/.node"
    echo "${*:-cordoned $(date '+%Y-%m-%dT%H:%M:%S%z')}" > "$VMS_DIR/.node/cordoned"
    emit_event node.cordoned "" "" "$(jq -nc --arg r "$*" '{reason: (if $r == "" then null else $r end)}')"
    log "cordoned: no new VMs until fcvm uncordon (running VMs stay as they are)"
}
uncordon_cmd() {
    cordoned || { log "not cordoned"; return 0; }
    rm -f "$VMS_DIR/.node/cordoned"
    emit_event node.uncordoned "" "" '{}'
    log "uncordoned: new VMs are accepted again"
}

has_label() {   # vm.json "" | KEY=VALUE
    [ -z "$2" ] || jq -e --arg k "${2%%=*}" --arg v "${2#*=}" '(.labels // {})[$k] == $v' "$1" >/dev/null
}

# A VM's state, the reason for it and since when (Unix time), from what the
# commands leave behind: `pid` while it runs; `expired` (ttl, idle) or the
# `stopped` marker after a stop; `last-exit` after the guest ended on its own
# or Firecracker died; nothing yet after create.
state_reason() {   # vm state -> "reason since"
    local d=$VMS_DIR/$1 why since=""
    if [ "$2" = running ]; then echo "start $(stat -c %Y "$d/pid" 2>/dev/null)"; return; fi
    if [ -s "$d/expired" ]; then why=$(cat "$d/expired"); since=$(stat -c %Y "$d/expired")
    elif [ -f "$d/stopped" ]; then why=user; since=$(stat -c %Y "$d/stopped")
    elif [ -f "$d/last-exit" ]; then
        why=$(jq -r 'if .stale then "crash" elif .code != null then "exit" elif (.fc_status // 0) != 0 then "crash" else "shutdown" end' "$d/last-exit")
        since=$(jq -r '.at | floor' "$d/last-exit")
    else why=created; since=$(date -d "$(jq -r .created "$d/vm.json")" +%s 2>/dev/null || true); fi
    echo "$why $since"
}

inspect_json() {
    local vm=$1 d state=stopped pid="" ip="" code=""
    d=$(vm_dir "$vm")
    if vm_running "$vm"; then
        state=running pid=$(vm_pid "$vm") ip=$(cat "$d/ip" 2>/dev/null || true)
    elif [ "$(jq -r .type "$d/vm.json")" = app ]; then
        code=$(exit_code "$vm"); [ -z "$code" ] || state=exited
    fi
    local rss="" balloon=null; [ -z "$pid" ] || rss=$(vm_rss "$pid")
    [ "$state" != running ] || balloon=$(fc_api_get "$d/fc.sock" /balloon 2>/dev/null | jq '.amount_mib' 2>/dev/null || echo null)
    # Lifetime: when --ttl and --idle-stop would stop a running VM (Unix time).
    local booted="" active="" reason
    if [ "$state" = running ]; then booted=$(stat -c %Y "$d/pid" 2>/dev/null || true); active=$(stat -c %Y "$d/activity" 2>/dev/null || echo "$booted"); fi
    read -r reason since < <(state_reason "$vm" "$state")
    jq --arg name "$vm" --arg state "$state" --arg pid "$pid" --arg ip "$ip" --arg code "$code" --arg rss "$rss" \
        --arg reason "$reason" --arg since "${since:-}" \
        --argjson disk "$(( $(stat -c %b "$d"/*.ext4 | head -1) * 512 ))" \
        --arg mode "$([ -f "$d/disk.ext4" ] && echo copy || echo overlay)" \
        --argjson chain "$(jq -n '$ARGS.positional' --args $(image_chain "$(jq -r .image "$d/vm.json")"))" \
        --arg booted "$booted" --arg active "$active" --arg expired "$(cat "$d/expired" 2>/dev/null || true)" \
        --argjson secrets "$(jq -c 'keys' "$d/secrets.json" 2>/dev/null || echo '[]')" --argjson balloon "${balloon:-null}" \
        --arg pooled "$(cat "$d/pooled" 2>/dev/null || true)" --arg claimed "$(cat "$d/claimed" 2>/dev/null || true)" \
        '{name: $name, state: $state, pid: ($pid | tonumber? // null), ip: ($ip | select(. != "") // null), secrets: $secrets,
          balloon_mib: $balloon, pooled: (if $pooled == "" then null else $pooled end),
          claimed_at: ($claimed | tonumber? // null),
          mem_used_bytes: ($rss | tonumber? // null),
          exit_code: ($code | tonumber? // null), image_chain: $chain, disk_mode: $mode, disk_used_bytes: $disk,
          stopped_by_user: $stopped, last_exit: $last,
          lease_until: (.lease_until // null),
          expires_at: (if .ttl_s and $booted != "" then ([($booted | tonumber), ($claimed | tonumber? // 0)] | max) + .ttl_s else null end),
          idle_expires_at: (if .idle_stop_s and $active != "" then ($active | tonumber) + .idle_stop_s else null end),
          expired: (if $expired == "" then null else $expired end),
          status: {state: $state, reason: $reason, since: ($since | tonumber? // null)}}
         + {restart: "no", labels: {}, spec: null} + .' \
        --argjson stopped "$([ -f "$d/stopped" ] && echo true || echo false)" \
        --argjson last "$(cat "$d/last-exit" 2>/dev/null || echo null)" "$d/vm.json"
}

inspect() {
    local vm=${1:?usage: fcvm inspect VM}
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
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
            -*)      die_code invalid "unknown option $1 ($usage)" ;;
            *)       vm=$1; shift ;;
        esac
    done
    [ -n "$vm" ] || die_code invalid "$usage"
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
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
    local fmt='"\(.ts)  \(.decision | ascii_upcase | .[0:10] | . + " " * (10 - length))  \(.method) \(.host):\(.port)\(.path // "")"'
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
    local d all=0 json=0 a label=""
    while [ $# -gt 0 ]; do
        case $1 in
            --all|-a) all=1; shift ;;
            --json)   json=1; shift ;;
            --label)  label_ok "${2:?--label wants KEY=VALUE}"; label=$2; shift 2 ;;
            --tenant) tenant_ok "${2:?--tenant wants a name}"; label=tenant=$2; shift 2 ;;
            *)        die_code invalid "unknown option $1 (usage: fcvm ls [--all] [--json] [--label KEY=VALUE] [--tenant NAME])" ;;
        esac
    done
    if [ $json = 1 ]; then
        for d in "$VMS_DIR"/*/; do
            [ -f "$d/vm.json" ] || continue
            [ $all = 1 ] || [[ $(basename "$d") != _* ]] || continue
            has_label "$d/vm.json" "$label" || continue
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
        has_label "$d/vm.json" "$label" || continue
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
            "$(jq -r --arg pooled "$(cat "$d/pooled" 2>/dev/null || true)" --argjson now "$(date +%s)" 'def dur: if . % 86400 == 0 then "\(. / 86400 | floor)d" elif . % 3600 == 0 then "\(. / 3600 | floor)h" elif . % 60 == 0 then "\(. / 60 | floor)m" else "\(.)s" end;
                [(if $pooled != "" then "pool:\($pooled)" else empty end),
                 (if .jail then "jail" else empty end), (if .agent == false then "no-agent" else empty end), (if (.restart // "no") != "no" then "restart:\(.restart)" else empty end),
                 (if .ttl_s then "ttl:\(.ttl_s | dur)" else empty end), (if .idle_stop_s then "idle-stop:\(.idle_stop_s | dur)" else empty end),
                 (if .lease_until then "lease:\(((.lease_until - $now) / 60 | floor) | if . < 0 then "ended" else "\(.)m" end)" else empty end),
                 (if (.forward // []) != [] then "forward:\(.forward | join(","))" else empty end),
                 (if .net.mode == "none" then "net:none" elif .net.mode == "restricted" then "allow:\(.net.allow | join(","))\(if .net.cache then " cache" else "" end)" else empty end)]
                + (.ports // []) + ((.volumes // []) | map("-v " + .))
                + ((.shares // []) | map("-v \(.host):\(.path)\(if .ro then ":ro" else "" end)")) | join(" ")' "$d/vm.json")"
    done
    if [ $nrun -gt 0 ]; then
        printf '\n%d running: %dM allocated, %dM used on the host (MEM = used/allocated)\n' "$nrun" "$tot_alloc" "$tot_rss"
    fi
}

list_images() {
    local j name all=0 json=0 tenant=""
    while [ $# -gt 0 ]; do
        case $1 in
            --all|-a) all=1; shift ;;
            --json)   json=1; shift ;;
            --tenant) tenant_ok "${2:?--tenant wants a name}"; tenant=$2; shift 2 ;;
            *)        die_code invalid "unknown option $1 (usage: fcvm images [--all] [--json] [--tenant NAME])" ;;
        esac
    done
    if [ $json = 1 ]; then
        for j in "$IMAGES_DIR"/*.json; do
            [ -f "$j" ] || continue
            name=$(basename "$j" .json)
            [ $all = 1 ] || [[ $name != _* ]] || continue
            [ -z "$tenant" ] || [ "$(image_tenant "$name")" = "$tenant" ] || continue
            jq --arg name "$name" --argjson used "$(( $(stat -c %b "${j%.json}.ext4") * 512 ))" \
                --argjson users "$(jq -n '$ARGS.positional' --args $(image_users "$name"))" \
                '{name: $name, type: (.type // "app"), parent: (.parent // null), ref: (.ref // null), tenant: (.tenant // null),
                  disk_used_bytes: $used, used_by: $users} + (. | {argv, env, workdir, user, exposed_ports} | with_entries(select(.value != null)))' "$j"
        done | jq -s .
        return
    fi
    printf '%-28s %-10s %-8s %-8s %s\n' NAME TYPE SIZE USED-BY SOURCE
    for j in "$IMAGES_DIR"/*.json; do
        [ -f "$j" ] || continue
        name=$(basename "$j" .json)
        [ $all = 1 ] || [[ $name != _* ]] || continue   # build cache (fcvm build)
        [ -z "$tenant" ] || [ "$(image_tenant "$name")" = "$tenant" ] || continue
        printf '%-28s %-10s %-8s %-8s %s\n' "$name" "$(jq -r '.type // "app"' "$j")" \
            "$(du -h "${j%.json}.ext4" | cut -f1)" "$(image_users "$name" | wc -l)" \
            "$(jq -r 'if .parent then "layer on \(.parent)" else (.ref // "-") end + (if .tenant then " (tenant \(.tenant))" else "" end)' "$j")"
    done
}

# fcvm update VM [--restart POLICY] [--ttl D] [--idle-stop D]: change settings of an existing VM.
update() {
    local usage="usage: fcvm update VM [--restart no|on-failure|unless-stopped|always] [--ttl DURATION|none] [--idle-stop DURATION|none] [--secret SPEC]... [--unset-secret NAME]... [--forward ssh-agent,git|none] [--label KEY=VALUE]... [--unset-label KEY]... [--lease UNTIL|none]"
    local vm=${1:-} restart="" ttl="" idle="" set_ttl=0 set_idle=0 what=() secrets=() SECRETS_DEL=() forward=() set_fwd=0 labels=() unlabel=() lease="" set_lease=0
    [ -n "$vm" ] || die_code invalid "$usage"; shift
    while [ $# -gt 0 ]; do
        case $1 in
            --restart)   valid_restart "${2:-}"; restart=$2; shift 2 ;;
            --ttl)       ttl=$(duration_s --ttl "${2:?$usage}") set_ttl=1; shift 2 ;;
            --idle-stop) idle=$(duration_s --idle-stop "${2:?$usage}") set_idle=1; shift 2 ;;
            --secret)    parse_secret "${2:?$usage}"; secrets+=("$2"); shift 2 ;;
            --unset-secret) SECRETS_DEL+=("${2:?$usage}"); shift 2 ;;
            --forward)   mapfile -t forward < <(parse_forward "${2:?$usage}"); set_fwd=1; shift 2 ;;
            --label)     label_ok "${2:?$usage}"; [[ $2 != tenant=* ]] || die_code invalid "a VM's tenant is set at create, fork or pool claim (--tenant) and can't be changed"
                         labels+=("$2"); shift 2 ;;
            --unset-label) [ "${2:?$usage}" != tenant ] || die_code invalid "a VM's tenant can't be unset"; unlabel+=("$2"); shift 2 ;;
            --lease)     lease=$(parse_until --lease "${2:?$usage}") set_lease=1; shift 2 ;;
            *)           die_code invalid "unknown option $1 ($usage)" ;;
        esac
    done
    [ -n "$restart" ] || [ $set_ttl = 1 ] || [ $set_idle = 1 ] || [ ${#secrets[@]} -gt 0 ] || [ ${#SECRETS_DEL[@]} -gt 0 ] || [ $set_fwd = 1 ] ||
        [ ${#labels[@]} -gt 0 ] || [ ${#unlabel[@]} -gt 0 ] || [ $set_lease = 1 ] || die_code invalid "$usage"
    lock_vm "$vm"
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    local f=$VMS_DIR/$vm/vm.json
    if [ $set_fwd = 1 ]; then
        [ ${#forward[@]} -eq 0 ] || node_refuse "the host's credentials stay on the host (--forward)"
        jq --argjson fwd "$(json_list "${forward[@]}")" --arg sock "${SSH_AUTH_SOCK:-}" \
            'del(.forward, .ssh_auth_sock) + (if ($fwd | length) == 0 then {} else {forward: $fwd} end)
             + (if ($fwd | index("ssh-agent")) then {ssh_auth_sock: $sock} else {} end)' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        what+=("forwarding ${forward[*]:-none} (from its next start)")
    fi
    if [ ${#secrets[@]} -gt 0 ] || [ ${#SECRETS_DEL[@]} -gt 0 ]; then
        write_secrets "$VMS_DIR/$vm" "${secrets[@]}"
        what+=("secrets: $(secret_names "$VMS_DIR/$vm" | tr '\n' ' ')(from its next start)")
    fi
    if [ -n "$restart" ]; then
        [ "$restart" = no ] || [ "$(jq -r .ephemeral "$f")" != true ] || die "'$vm' is a throwaway VM (fcvm run); restart policies don't apply"
        jq --arg r "$restart" '.restart = $r' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        what+=("restart policy $restart")
    fi
    if [ $set_ttl = 1 ]; then
        jq --arg t "$ttl" 'if $t == "" then del(.ttl_s) else .ttl_s = ($t | tonumber) end' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        what+=("ttl $([ -n "$ttl" ] && echo "${ttl}s" || echo none)")
    fi
    if [ $set_idle = 1 ]; then
        jq --arg t "$idle" 'if $t == "" then del(.idle_stop_s) else .idle_stop_s = ($t | tonumber) end' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        what+=("idle-stop $([ -n "$idle" ] && echo "${idle}s" || echo none)")
    fi
    if [ $set_lease = 1 ]; then
        set_lease "$vm" "$lease"
        what+=("lease $([ -n "$lease" ] && date -d "@$lease" '+%Y-%m-%dT%H:%M:%S%z' || echo none)")
    fi
    if [ ${#labels[@]} -gt 0 ] || [ ${#unlabel[@]} -gt 0 ]; then
        jq --argjson set "$(labels_json "${labels[@]}")" --argjson del "$(json_list "${unlabel[@]}")" \
            '.labels = ((.labels // {}) + $set | with_entries(select(.key as $k | $del | index($k) | not)))' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        what+=("labels $(jq -r '.labels | to_entries | map("\(.key)=\(.value)") | join(" ")' "$f")")
        [ -n "$restart" ] || [ $set_ttl = 1 ] || [ $set_idle = 1 ] || [ ${#secrets[@]} -gt 0 ] || [ ${#SECRETS_DEL[@]} -gt 0 ] || [ $set_fwd = 1 ] ||
            { emit_event vm.updated "$vm" "" "$(json_list "${what[@]}" | jq -c '{what: .}')"; [ -n "${QUIET_UPDATE:-}" ] || log "'$vm': ${what[*]}"; return 0; }
    fi
    emit_event vm.updated "$vm" "" "$(json_list "${what[@]}" | jq -c '{what: .}')"
    [ -n "${QUIET_UPDATE:-}" ] || log "'$vm': $(printf '%s, ' "${what[@]}" | sed 's/, $//') (applied by fcvm serve / the fcvm service)"
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

# fcvm template ls|show|rm|save: launch templates (lib/templates.py).
template_cmd() {
    local usage="usage: fcvm template ls [--json] | show NAME | rm NAME | save NAME (--from VM | IMAGE [create options] [-- CMD]) [-d DESCRIPTION]"
    local sub=${1:-ls}
    [ $# -eq 0 ] || shift
    case $sub in
        ls|show)  python3 "$FCVM_ROOT/lib/templates.py" "$sub" "$@" ;;
        rm)       python3 "$FCVM_ROOT/lib/templates.py" rm "${1:?$usage}" && log "removed template '$1'" ;;
        save)
            local name=${1:?$usage} desc="" from="" args=() json out
            shift
            while [ $# -gt 0 ]; do
                case $1 in
                    -d|--description) desc=${2?$usage}; shift 2 ;;
                    --from)           from=${2:?$usage}; shift 2 ;;
                    --)               args+=("$@"); break ;;
                    *)                args+=("$1"); shift ;;
                esac
            done
            if [ -n "$from" ]; then
                json=$(python3 "$FCVM_ROOT/lib/templates.py" from-vm "$name" "$from" "$desc") || exit 1
            else
                [ ${#args[@]} -gt 0 ] || die_code invalid "$usage"
                out=$(mktemp)
                (FCVM_TEMPLATE_OUT=$out create "_template-$$" "${args[@]}") || { rm -f "$out"; exit 1; }
                json=$(jq --arg d "$desc" '.description = $d' "$out"); rm -f "$out"
            fi
            python3 "$FCVM_ROOT/lib/templates.py" save "$name" <<<"$json" >/dev/null
            log "saved template '$name'. Use it: fcvm create VM --template $name" ;;
        *) die_code invalid "$usage" ;;
    esac
}

rm_vm() {
    local vm=${1:?usage: fcvm rm VM}
    lock_vm "$vm"
    vm_exists "$vm" || die_code vm_not_found "no VM '$vm'"
    vm_running "$vm" && die_code vm_running "VM '$vm' is running (fcvm stop $vm first)"
    emit_event vm.deleted "$vm" "${FCVM_EXPIRED:-}"
    rm -rf "$(vm_dir "$vm")" "$VMS_DIR/.locks/vm-$vm"
    log "removed '$vm'"
}

case $cmd in
    create)  create "$@" ;;
    start)   VIA_DAEMON=1 start "$@" ;;
    run)     run "$@" ;;
    stop)    stop "$@" ;;
    console) console "$@" ;;
    logs)    logs "$@" ;;
    _reap)   reap "$@" ;;
    ssh)     ssh_vm "$@" ;;
    ssh-config) ssh_config_cmd "$@" ;;
    _ssh-proxy) ssh_proxy "$@" ;;
    exec)    exec_vm "$@" ;;
    shell)   shell_vm "$@" ;;
    ls)      list_vms "$@" ;;
    images)  list_images "$@" ;;
    inspect) inspect "$@" ;;
    commit)  commit "$@" ;;
    rmi)     rmi "$@" ;;
    squash)  squash "$@" ;;
    export)  export_cmd "$@" ;;
    push)    push_cmd "$@" ;;
    prune)   prune ;;
    cp)      cp_cmd "$@" ;;
    volume)  volume_cmd "$@" ;;
    egress)  egress "$@" ;;
    snapshot) snapshot_cmd "$@" ;;
    fork)    fork_cmd "$@" ;;
    mount)   mount_cmd "$@" ;;
    umount)  umount_cmd "$@" ;;
    publish) publish_cmd "$@" ;;
    unpublish) unpublish_cmd "$@" ;;
    jobs)    jobs_cmd "$@" ;;
    balloon) balloon_cmd "$@" ;;
    pool)    pool_cmd "$@" ;;
    rm)      rm_vm "$@" ;;
    update)  update "$@" ;;
    template) template_cmd "$@" ;;
    _shutdown) shutdown_all "$@" ;;
    _event)  emit_event "$@" ;;   # for the service and the supervisor (lib/web)
    cordon)  cordon_cmd "$@" ;;
    uncordon) uncordon_cmd "$@" ;;
esac
