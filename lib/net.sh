#!/usr/bin/env bash
# Host networking: one bridge with NAT to the outside world and a pool of
# persistent tap devices owned by the invoking user, so VMs start without root.
#
#   host  $NET_PREFIX.1/24 on $NET_BRIDGE
#   VM i  $NET_PREFIX.(10+i) via tap fctap<i>
. "$(dirname "$0")/common.sh"

owner=$(id -un)
action=${1:?up|down}

up() {
    if ! ip link show "$NET_BRIDGE" &>/dev/null; then
        log "creating bridge $NET_BRIDGE ($NET_PREFIX.1/24)"
        sudo ip link add "$NET_BRIDGE" type bridge
        sudo ip addr add "$NET_PREFIX.1/24" dev "$NET_BRIDGE"
    fi
    sudo ip link set "$NET_BRIDGE" up

    for ((i = 0; i < NET_TAPS; i++)); do
        tap=fctap$i
        ip link show "$tap" &>/dev/null || sudo ip tuntap add "$tap" mode tap user "$owner"
        sudo ip link set "$tap" master "$NET_BRIDGE" up
    done
    log "taps fctap0..fctap$((NET_TAPS - 1)) owned by $owner"

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
        iifname "$NET_BRIDGE" accept
        oifname "$NET_BRIDGE" ct state established,related accept
    }
}
EOF
    log "NAT for $NET_PREFIX.0/24 enabled (nft table ip fcvm)"

    # ufw's own forward chain drops routed traffic by default; nftables needs
    # every base chain to accept, so punch the hole there too.
    if command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; then
        sudo ufw route allow in on "$NET_BRIDGE" >/dev/null
        sudo ufw allow in on "$NET_BRIDGE" >/dev/null
        log "ufw: allowed traffic from $NET_BRIDGE"
    fi
}

down() {
    for ((i = 0; i < NET_TAPS; i++)); do
        ip link show "fctap$i" &>/dev/null && sudo ip link del "fctap$i"
    done
    ip link show "$NET_BRIDGE" &>/dev/null && sudo ip link del "$NET_BRIDGE"
    sudo nft delete table ip fcvm 2>/dev/null || true
    if command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; then
        sudo ufw route delete allow in on "$NET_BRIDGE" >/dev/null || true
        sudo ufw delete allow in on "$NET_BRIDGE" >/dev/null || true
    fi
    log "network removed"
}

"$action"
