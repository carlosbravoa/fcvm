#!/usr/bin/env bash
# Build fc-init (static) and the initramfs every VM boots with. The initramfs
# holds only /init (fc-init), /dev/console and a few mount points, written as a
# newc cpio by Python so device nodes and root ownership need no root.
. "$(dirname "$0")/common.sh"
need gcc strip python3

mkdir -p "$BUILD_DIR"
gcc -static -Os -Wall -Wextra -Wno-unused-result -o "$BUILD_DIR/fc-init.$$" "$FCVM_ROOT/init/fc-init.c"
strip "$BUILD_DIR/fc-init.$$"
mv -f "$BUILD_DIR/fc-init.$$" "$BUILD_DIR/fc-init"

python3 - "$BUILD_DIR/fc-init" "$BUILD_DIR/initramfs.cpio.$$" <<'EOF'
import stat, sys, time

init_path, out_path = sys.argv[1:]
now = int(time.time())
ino = 0

def entry(f, name, mode, data=b"", rdev=(0, 0)):
    global ino
    ino += 1
    name_b = name.encode() + b"\0"
    fields = [ino, mode, 0, 0, 1, now, len(data), 0, 0, rdev[0], rdev[1], len(name_b), 0]
    hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
    f.write(hdr + name_b + b"\0" * (-(len(hdr) + len(name_b)) % 4))
    f.write(data + b"\0" * (-len(data) % 4))

with open(init_path, "rb") as f:
    init = f.read()
with open(out_path, "wb") as f:
    for d in ["dev", "proc", "sys", "mnt"]:
        entry(f, d, stat.S_IFDIR | 0o755)
    entry(f, "dev/console", stat.S_IFCHR | 0o600, rdev=(5, 1))
    entry(f, "init", stat.S_IFREG | 0o755, init)
    entry(f, "TRAILER!!!", 0)
EOF
mv -f "$BUILD_DIR/initramfs.cpio.$$" "$BUILD_DIR/initramfs.cpio"   # atomic: VMs may be starting
sha256sum < "$FCVM_ROOT/init/fc-init.c" | cut -d' ' -f1 > "$BUILD_DIR/initramfs.src"   # see initramfs_current
log "built $BUILD_DIR/fc-init ($(du -h "$BUILD_DIR/fc-init" | cut -f1)) and initramfs.cpio"
