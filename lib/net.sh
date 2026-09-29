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
#
# Guests can't reach each other (NET_ISOLATE=0 allows it on the full bridge),
# nor host services (NET_HOST_ACCESS=1 allows it); each tap (or a jailed VM's
# veth, fcv<i> / fcrv<i>) only passes IPv4 and ARP from its own MAC and address.
if [ -n "${FCVM_NET_ENV:-}" ]; then
    # Boot-time mode (fcvm-net.service, installed by `fcvm service install`): a
    # root-owned copy of this script, run by root, reading root-owned settings.
    # Nothing from the user-writable fcvm tree is sourced as root.
    set -euo pipefail
    . "$FCVM_NET_ENV"
    log()  { printf '==> %s\n' "$*" >&2; }
    die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
    sudo() { "$@"; }
    owner=$FCVM_OWNER
else
    . "$(dirname "$0")/common.sh"
    owner=$(id -un)
fi
action=${1:?up|down}

ufw_active() { command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; }

bridge_up() {   # bridge prefix tap-prefix isolate
    local br=$1 prefix=$2 tp=$3 isolate=$4 i tap
    if ! ip link show "$br" &>/dev/null; then
        log "creating bridge $br ($prefix.1/24)"
        sudo ip link add "$br" type bridge
        sudo ip addr add "$prefix.1/24" dev "$br"
    fi
    sudo sysctl -q -w "net.ipv6.conf.$br.disable_ipv6=1"
    sudo ip link set "$br" up
    for ((i = 0; i < NET_TAPS; i++)); do
        tap=$tp$i
        ip link show "$tap" &>/dev/null || sudo ip tuntap add "$tap" mode tap user "$owner"
        sudo sysctl -q -w "net.ipv6.conf.$tap.disable_ipv6=1"
        sudo ip link set "$tap" master "$br" up
        sudo bridge link set dev "$tap" isolated "$([ "$isolate" = 1 ] && echo on || echo off)"
    done
    log "taps ${tp}0..${tp}$((NET_TAPS - 1)) on $br owned by $owner$([ "$isolate" = 1 ] && echo ', isolated from each other')"
}

up() {
    bridge_up "$NET_BRIDGE" "$NET_PREFIX" fctap "$NET_ISOLATE"
    bridge_up "$NET_R_BRIDGE" "$NET_R_PREFIX" fcrtap 1

    sudo sysctl -q -w net.ipv4.ip_forward=1
    # Anti-spoofing: every guest port's allowed MAC and IPv4 address.
    local i p ip macs=() ips=()
    for ((i = 0; i < NET_TAPS; i++)); do
        ip=$NET_PREFIX.$((10 + i))
        for p in fctap fcv; do macs+=("\"$p$i\" . $(printf '06:00:%02x:%02x:%02x:%02x' ${ip//./ })"); ips+=("\"$p$i\" . $ip"); done
        ip=$NET_R_PREFIX.$((10 + i))
        for p in fcrtap fcrv; do macs+=("\"$p$i\" . $(printf '06:01:%02x:%02x:%02x:%02x' ${ip//./ })"); ips+=("\"$p$i\" . $ip"); done
    done
    local hairpin="" host="iifname \"$NET_BRIDGE\" accept"
    [ "$NET_ISOLATE" != 1 ] || hairpin="iifname \"$NET_BRIDGE\" oifname \"$NET_BRIDGE\" drop"
    [ "$NET_HOST_ACCESS" = 1 ] || host="iifname \"$NET_BRIDGE\" icmp type echo-request accept
        iifname \"$NET_BRIDGE\" reject"
    sudo nft -f - <<EOF
table bridge fcvm
delete table bridge fcvm
table bridge fcvm {
    set guest_mac { type ifname . ether_addr; elements = { $(IFS=,; echo "${macs[*]}") } }
    set guest_ip { type ifname . ipv4_addr; elements = { $(IFS=,; echo "${ips[*]}") } }
    chain prerouting {
        type filter hook prerouting priority filter; policy accept;
        iifname "fctap*" jump guest
        iifname "fcrtap*" jump guest
        iifname "fcv*" jump guest
        iifname "fcrv*" jump guest
    }
    chain guest {
        iifname . ether saddr != @guest_mac drop
        ether type != { ip, arp } drop
        ether type ip iifname . ip saddr != @guest_ip drop
        ether type arp iifname . arp saddr ether != @guest_mac drop
        ether type arp iifname . arp saddr ip != @guest_ip drop
    }
}
EOF
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
        $hairpin
        iifname "$NET_BRIDGE" accept
        oifname "$NET_BRIDGE" ct state established,related accept
    }
    chain input {
        type filter hook input priority filter; policy accept;
        iifname "$NET_R_BRIDGE" ct state established,related accept
        iifname "$NET_R_BRIDGE" ip daddr $NET_R_PREFIX.1 tcp dport $EGRESS_PORT accept
        iifname "$NET_R_BRIDGE" reject
        iifname "$NET_BRIDGE" ct state established,related accept
        $host
    }
}
EOF
    log "NAT for $NET_PREFIX.0/24; $NET_R_PREFIX.0/24 reaches only the egress proxy (nft table ip fcvm)"
    log "guests: $([ "$NET_ISOLATE" = 1 ] && echo isolated from each other || echo may reach each other), $([ "$NET_HOST_ACCESS" = 1 ] && echo may reach || echo cut off from) host services; anti-spoofing and no IPv6 (nft table bridge fcvm)"

    # ufw's own chains drop by default; nftables needs every base chain to accept.
    if ufw_active; then
        sudo ufw route allow in on "$NET_BRIDGE" >/dev/null
        if [ "$NET_HOST_ACCESS" = 1 ]; then
            sudo ufw allow in on "$NET_BRIDGE" >/dev/null
        else
            sudo ufw delete allow in on "$NET_BRIDGE" >/dev/null 2>&1 || true
        fi
        sudo ufw allow in on "$NET_R_BRIDGE" to "$NET_R_PREFIX.1" port "$EGRESS_PORT" proto tcp >/dev/null
        log "ufw: allowed forwarding from $NET_BRIDGE$([ "$NET_HOST_ACCESS" = 1 ] && echo " and input from it"), and $NET_R_BRIDGE to the egress proxy"
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
    sudo nft delete table bridge fcvm 2>/dev/null || true
    if ufw_active; then
        sudo ufw route delete allow in on "$NET_BRIDGE" >/dev/null || true
        sudo ufw delete allow in on "$NET_BRIDGE" >/dev/null || true
        sudo ufw delete allow in on "$NET_R_BRIDGE" to "$NET_R_PREFIX.1" port "$EGRESS_PORT" proto tcp >/dev/null || true
    fi
    log "network removed"
}

"$action"
