#!/usr/bin/env bash
# fcvm jail-setup [--remove]: install (or remove) fcvm-jaild, the root helper
# that runs VMs under the Firecracker jailer. Uses sudo. Re-run it after
# updating fcvm or `fcvm firecracker`: the service runs root-owned copies in
# /usr/local/lib/fcvm, never the files in this (user-writable) tree.
. "$(dirname "$0")/common.sh"

LIB=/usr/local/lib/fcvm
UNIT=/etc/systemd/system/fcvm-jaild.service

if [ "${1:-}" = --remove ]; then
    sudo systemctl disable --now fcvm-jaild 2>/dev/null || true
    sudo rm -f "$UNIT" /etc/fcvm/jaild.json
    sudo rm -rf "$LIB"
    sudo systemctl daemon-reload
    log "fcvm-jaild removed (jailed VMs are stopped; /srv/jailer is left in place)"
    exit 0
fi

need setfacl
[ -x "$FIRECRACKER" ] && [ -x "$BIN_DIR/jailer" ] || die "run fcvm firecracker first"
log "installing root-owned copies of jaild.py, jailer and firecracker into $LIB"
sudo install -d -m 755 -o root -g root "$LIB" /etc/fcvm
sudo install -m 755 -o root -g root "$FCVM_ROOT/lib/jaild.py" "$BIN_DIR/jailer" "$FIRECRACKER" "$LIB/"

jq -n --argjson uid "$(id -u)" --arg root "$FCVM_HOME" --argjson base "${JAIL_UID_BASE:-900000}" \
    --arg br "$NET_BRIDGE" --arg rbr "$NET_R_BRIDGE" --argjson iso "$([ "$NET_ISOLATE" = 1 ] && echo true || echo false)" \
    '{owner_uid: $uid, fcvm_root: $root, jail_base: "/srv/jailer", uid_base: $base, slots: 256,
      bridges: {fctap: $br, fcrtap: $rbr}, isolate: {fctap: $iso, fcrtap: true}}' |
    sudo tee /etc/fcvm/jaild.json >/dev/null

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=fcvm jailer helper (runs fcvm VMs under the Firecracker jailer)
After=network.target

[Service]
ExecStart=/usr/bin/python3 $LIB/jaild.py --config /etc/fcvm/jaild.json
# per-VM cgroups live under this service's cgroup
Delegate=yes
# bind mounts into the jails stay out of the host's mount table
PrivateMounts=yes
RuntimeDirectory=fcvm
RuntimeDirectoryMode=0755
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable fcvm-jaild >/dev/null 2>&1
sudo systemctl restart fcvm-jaild
for i in $(seq 1 30); do [ -S /run/fcvm/jaild.sock ] && break; sleep 0.1; done
[ -S /run/fcvm/jaild.sock ] || { sudo journalctl -u fcvm-jaild -n 20 --no-pager >&2; die "fcvm-jaild did not start"; }
log "fcvm-jaild running. Jail a VM with: fcvm create VM IMAGE --jail   (or JAIL=1 for every new VM)"
