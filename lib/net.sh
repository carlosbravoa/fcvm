#!/usr/bin/env bash
# Host networking, set up once per boot (root). Two bridges, each with a pool
# of persistent taps owned by the invoking user, so VMs start without root:
#
#   full        $NET_BRIDGE  $NET_PREFIX.1/24    NAT to everything
#               tap fctap<i>  -> VM $NET_PREFIX.(10+i)
#   restricted  $NET_R_BRIDGE $NET_R_PREFIX.1/24  no forwarding at all; VMs can
#               reach only the egress proxy ($EGRESS_PORT on the host), which
#               applies each VM's allowlist. Taps are isolated from each other.
#               tap fcrtap<i> -> VM $NET_R_PREFIX.(10+i)
. "$(dirname "$0")/common.sh"

owner=$(id -un)
action=${1:?up|down}

ufw_active() { command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; }

bridge_up() {   # bridge prefix tap-prefix isolate
    local br=$1 prefix=$2 tp=$3 isolate=$4 i tap
    if ! ip link show "$br" &>/dev/null; then
        log "creating bridge $br ($prefix.1/24)"
        sudo ip link add "$br" type bridge
        sudo ip addr add "$prefix.1/24" dev "$br"
    fi
    sudo ip link set "$br" up
    for ((i = 0; i < NET_TAPS; i++)); do
        tap=$tp$i
        ip link show "$tap" &>/dev/null || sudo ip tuntap add "$tap" mode tap user "$owner"
        sudo ip link set "$tap" master "$br" up
        sudo bridge link set dev "$tap" isolated "$([ "$isolate" = 1 ] && echo on || echo off)"
    done
    log "taps ${tp}0..${tp}$((NET_TAPS - 1)) on $br owned by $owner$([ "$isolate" = 1 ] && echo ', isolated from each other')"
}

up() {
    bridge_up "$NET_BRIDGE" "$NET_PREFIX" fctap "$NET_ISOLATE"
    bridge_up "$NET_R_BRIDGE" "$NET_R_PREFIX" fcrtap 1

    sudo sysctl -q -w net.ipv4.ip_forward=1
    sudo nft -f - <<EOF
table ip fcvm
delete table ip fcvm
table ip fcvm {
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr $NET_PREFIX.0/24 oifname != "$NET_BRIDGE" masquerade
    }
    chain forward {
        type filter hook forward priority filter; policy accept;
        iifname "$NET_R_BRIDGE" drop
        oifname "$NET_R_BRIDGE" drop
        iifname "$NET_BRIDGE" accept
        oifname "$NET_BRIDGE" ct state established,related accept
    }
    chain input {
        type filter hook input priority filter; policy accept;
        iifname "$NET_R_BRIDGE" ct state established,related accept
        iifname "$NET_R_BRIDGE" ip daddr $NET_R_PREFIX.1 tcp dport $EGRESS_PORT accept
        iifname "$NET_R_BRIDGE" reject
    }
}
EOF
    log "NAT for $NET_PREFIX.0/24; $NET_R_PREFIX.0/24 reaches only the egress proxy (nft table ip fcvm)"

    # ufw's own chains drop by default; nftables needs every base chain to accept.
    if ufw_active; then
        sudo ufw route allow in on "$NET_BRIDGE" >/dev/null
        sudo ufw allow in on "$NET_BRIDGE" >/dev/null
        sudo ufw allow in on "$NET_R_BRIDGE" to "$NET_R_PREFIX.1" port "$EGRESS_PORT" proto tcp >/dev/null
        log "ufw: allowed $NET_BRIDGE, and $NET_R_BRIDGE to the egress proxy"
    fi
}

down() {
    local i tp
    for tp in fctap fcrtap; do
        for ((i = 0; i < NET_TAPS; i++)); do
            ip link show "$tp$i" &>/dev/null && sudo ip link del "$tp$i"
        done
    done
    for br in "$NET_BRIDGE" "$NET_R_BRIDGE"; do
        ip link show "$br" &>/dev/null && sudo ip link del "$br"
    done
    sudo nft delete table ip fcvm 2>/dev/null || true
    if ufw_active; then
        sudo ufw route delete allow in on "$NET_BRIDGE" >/dev/null || true
        sudo ufw delete allow in on "$NET_BRIDGE" >/dev/null || true
        sudo ufw delete allow in on "$NET_R_BRIDGE" to "$NET_R_PREFIX.1" port "$EGRESS_PORT" proto tcp >/dev/null || true
    fi
    log "network removed"
}

"$action"
