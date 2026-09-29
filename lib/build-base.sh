#!/usr/bin/env bash
# Ubuntu 26.04 base image: minbase + systemd + ssh, built rootless with
# mmdebstrap (user namespaces) and packed with `mkfs.ext4 -d <tar>`.
#
# Networking comes from the kernel command line (ip=), so there is no network
# manager in the guest; /etc/resolv.conf points at /proc/net/pnp.
. "$(dirname "$0")/common.sh"
need mmdebstrap newuidmap mkfs.ext4 ssh-keygen

name=${1:-ubuntu-26.04}
img=$IMAGES_DIR/$name.ext4
mkdir -p "$IMAGES_DIR" "$BUILD_DIR" "$FCVM_ROOT/ssh"
[ -x "$BUILD_DIR/fc-init" ] || "$FCVM_ROOT/lib/build-init.sh"
users=$(image_users "$name")
[ -z "$users" ] || die "image '$name' is the shared base of VMs: $users (remove them, or build under another name)"

# Project SSH key (used by `fcvm ssh`) plus the user's own public keys.
key=$FCVM_ROOT/ssh/id_ed25519
[ -f "$key" ] || ssh-keygen -q -t ed25519 -N '' -C fcvm -f "$key"
keys=$BUILD_DIR/authorized_keys
cat "$key.pub" > "$keys"
cat ~/.ssh/id_*.pub >> "$keys" 2>/dev/null || true
chmod 644 "$keys"

tar=$BUILD_DIR/$name.rootfs.tar
trap 'rm -f "$tar"' EXIT
log "bootstrapping Ubuntu $UBUNTU_SUITE from $UBUNTU_MIRROR"
mmdebstrap \
    --mode=unshare --variant=minbase --format=tar \
    --components=main,universe \
    --include="$BASE_PACKAGES" \
    --customize-hook='echo ubuntu > "$1/etc/hostname"' \
    --customize-hook='printf "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n" > "$1/etc/hosts"' \
    --customize-hook='echo "# root is set up by the kernel / fc-init (overlay)" > "$1/etc/fstab"' \
    --customize-hook='mkdir -p "$1/.fcvm/rw" "$1/.fcvm/newroot"' \
    --customize-hook="upload $BUILD_DIR/fc-init /.fcvm/init" \
    --customize-hook='chmod 755 "$1/.fcvm/init"' \
    --customize-hook='ln -sf /proc/net/pnp "$1/etc/resolv.conf"' \
    --customize-hook=': > "$1/etc/machine-id"; rm -f "$1/var/lib/dbus/machine-id"' \
    --customize-hook='rm -f "$1"/etc/ssh/ssh_host_*' \
    --customize-hook='mkdir -p -m 700 "$1/root/.ssh"' \
    --customize-hook="upload $keys /root/.ssh/authorized_keys" \
    --customize-hook='chmod 600 "$1/root/.ssh/authorized_keys"' \
    --customize-hook='mkdir -p "$1/etc/systemd/system/serial-getty@ttyS0.service.d"
cat > "$1/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud 115200,57600,38400,9600 - \$TERM
EOF' \
    --customize-hook='cat > "$1/etc/systemd/system/ssh-hostkeys.service" <<EOF
[Unit]
Description=Generate SSH host keys on first boot
DefaultDependencies=no
After=local-fs.target
Before=sysinit.target ssh.socket ssh.service
ConditionPathExistsGlob=!/etc/ssh/ssh_host_*_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=sysinit.target
EOF
mkdir -p "$1/etc/systemd/system/sysinit.target.wants"
ln -sf /etc/systemd/system/ssh-hostkeys.service "$1/etc/systemd/system/sysinit.target.wants/ssh-hostkeys.service"' \
    --customize-hook='cat > "$1/etc/systemd/system/fcvm-agent.service" <<EOF
[Unit]
Description=fcvm exec agent (vsock), for fcvm exec / fcvm shell

[Service]
ExecStart=/.fcvm/init --agent
Restart=always
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/fcvm-agent.service "$1/etc/systemd/system/multi-user.target.wants/fcvm-agent.service"' \
    --customize-hook='sed -i "s/^AcceptEnv/#AcceptEnv/" "$1/etc/ssh/sshd_config"' \
    --customize-hook='echo LANG=C.UTF-8 > "$1/etc/default/locale"' \
    --customize-hook='rm -rf "$1"/var/lib/apt/lists/* "$1"/var/cache/apt/*.bin' \
    "$UBUNTU_SUITE" "$tar" "$UBUNTU_MIRROR"

log "creating $img ($BASE_SIZE)"
rm -f "$img"
mkfs.ext4 -q -F -L rootfs -d "$tar" "$img" "$BASE_SIZE"
chmod a-w "$img"   # shared read-only by every VM created from it
jq -n --arg suite "$UBUNTU_SUITE" --arg size "$BASE_SIZE" \
    '{type: "systemd", ref: ("ubuntu:" + $suite), disk_size: $size}' > "$IMAGES_DIR/$name.json"
log "image ready: $img. Try: ./fcvm create dev $name && ./fcvm start dev"
