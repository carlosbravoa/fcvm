#!/usr/bin/env bash
# fcvm service install [--port N] | remove | status: run fcvm at boot.
#
#   fcvm-net.service  root, oneshot: bridges, taps and firewall rules at boot,
#                     so `fcvm net-up` is no longer needed after a reboot. It
#                     runs a root-owned copy of net.sh with root-owned settings
#                     (/etc/fcvm/net.env), never files from this tree.
#   fcvm.service      `fcvm serve` as you, after the network and fcvm-jaild:
#                     the web console, its API, and the supervisor that brings
#                     VMs back after a reboot and applies restart policies. At
#                     host shutdown it stops VMs cleanly first (fcvm _shutdown).
#
# install and remove use sudo. Re-run install after updating fcvm or changing
# network settings (NET_* in fcvm.conf).
. "$(dirname "$0")/common.sh"

LIB=/usr/local/lib/fcvm
NET_UNIT=/etc/systemd/system/fcvm-net.service
UNIT=/etc/systemd/system/fcvm.service
STATE=$VMS_DIR/.serve.json   # the running console's URL (written by fcvm serve)


install_units() {
    local port=8686
    while [ $# -gt 0 ]; do
        case $1 in
            --port) [[ ${2:-} =~ ^[0-9]+$ ]] || die "--port wants a number"; port=$2; shift 2 ;;
            *)      die "unknown option $1 (usage: fcvm service install [--port N])" ;;
        esac
    done
    need systemctl
    [ "$(id -u)" != 0 ] || die "run as your user (it uses sudo); the service runs fcvm as whoever installs it"
    log "installing fcvm-net.service (network at boot) and fcvm.service (fcvm serve as $(id -un))"
    sudo install -d -m 755 -o root -g root "$LIB" /etc/fcvm
    sudo install -m 755 -o root -g root "$FCVM_ROOT/lib/net.sh" "$LIB/net.sh"
    {
        printf '# written by `fcvm service install`; re-run it to change these\n'
        local v
        for v in NET_BRIDGE NET_PREFIX NET_TAPS NET_ISOLATE NET_HOST_ACCESS NET_R_BRIDGE NET_R_PREFIX EGRESS_PORT; do
            printf '%s=%q\n' "$v" "${!v}"
        done
        printf 'FCVM_OWNER=%q\n' "$(id -un)"
    } | sudo tee /etc/fcvm/net.env >/dev/null
    sudo chmod 644 /etc/fcvm/net.env

    sudo tee "$NET_UNIT" >/dev/null <<EOF
[Unit]
Description=fcvm host network (bridges, taps, isolation rules)
After=network.target ufw.service docker.service
Before=fcvm.service fcvm-jaild.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=FCVM_NET_ENV=/etc/fcvm/net.env
ExecStart=/bin/bash $LIB/net.sh up

[Install]
WantedBy=multi-user.target
EOF

    sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=fcvm: web console, API and VM supervisor
After=network-online.target fcvm-net.service fcvm-jaild.service
Wants=network-online.target fcvm-net.service

[Service]
User=$(id -un)
Environment=FCVM_HOME=$FCVM_HOME
ExecStart=$(fcvm_entry) serve --service --port $port
# At host shutdown: stop VMs cleanly and mark them to resume at boot.
# Restarting the service alone leaves them running.
ExecStop=$(fcvm_entry) _shutdown
KillMode=process
TimeoutStopSec=150
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable fcvm-net.service fcvm.service >/dev/null 2>&1
    sudo systemctl restart fcvm-net.service
    sudo systemctl restart fcvm.service
    [ -S /run/fcvm/jaild.sock ] || warn "fcvm-jaild isn't installed; jailed VMs need it (fcvm jail-setup)"
    local i
    for ((i = 0; i < 50; i++)); do [ -f "$STATE" ] && break; sleep 0.1; done
    status
}

remove_units() {
    # Stopping the service outside a shutdown leaves running VMs alone.
    sudo systemctl disable --now fcvm.service fcvm-net.service >/dev/null 2>&1 || true
    sudo rm -f "$UNIT" "$NET_UNIT" "$LIB/net.sh" /etc/fcvm/net.env
    sudo systemctl daemon-reload
    rm -f "$STATE"
    log "fcvm service removed. Running VMs keep running; after a reboot, run fcvm net-up again"
}

status() {
    local u
    for u in fcvm-net fcvm-jaild fcvm; do
        printf '%-12s %s\n' "$u" "$(systemctl is-active "$u.service" 2>/dev/null || true)"
    done
    if systemctl is-active -q fcvm.service && [ -f "$STATE" ]; then
        printf '\nweb console: %s\n' "$(jq -r .url "$STATE")"
        printf 'API token:   %s  (header: Authorization: Bearer <token>)\n' "$(jq -r .token "$STATE")"
    fi
}

case ${1:-status} in
    install) shift; install_units "$@" ;;
    remove)  remove_units ;;
    status)  status ;;
    *)       die "usage: fcvm service install [--port N] | remove | status" ;;
esac
