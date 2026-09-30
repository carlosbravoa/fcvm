#!/usr/bin/env python3
"""tar2ext4.py TAR MKFS-OPTIONS... -- DEVICE [SIZE]: an ext4 filesystem with
TAR's contents, owners included, with no privileges at all.

For e2fsprogs older than 1.47.1 (which can't read tarballs: Ubuntu 24.04 has
1.47.0), and hosts where unprivileged user namespaces are restricted (Ubuntu
24.04's AppArmor default), so nothing can chown to other users:
  1. unpack TAR as the calling user, with ordinary permissions;
  2. mkfs.ext4 -d on that directory;
  3. write each entry's real owner, mode (setuid and all), mtime, device
     nodes and extended attributes into the new filesystem with debugfs.
"""
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

TYPE_BITS = {
    tarfile.REGTYPE: stat.S_IFREG, tarfile.AREGTYPE: stat.S_IFREG, tarfile.CONTTYPE: stat.S_IFREG,
    tarfile.DIRTYPE: stat.S_IFDIR, tarfile.SYMTYPE: stat.S_IFLNK, tarfile.FIFOTYPE: stat.S_IFIFO,
    tarfile.CHRTYPE: stat.S_IFCHR, tarfile.BLKTYPE: stat.S_IFBLK,
}


def safe(name):
    """A member name as a path inside the image, or None if it would escape."""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def quote(path):
    """debugfs arguments: double-quoted; names it can't express are skipped."""
    if any(c in path for c in '"\\\n'):
        return None
    return f'"/{path}"'


def unpack(tar, root):
    """Unpack as ourselves (owners ignored, permissions ordinary). Returns the
    members to fix up, in tar order, with their normalized paths."""
    entries = []
    with tarfile.open(tar) as tf:
        for m in tf:
            path = safe(m.name)
            if not path:
                continue
            dest = os.path.join(root, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                os.chmod(dest, 0o755)
            elif m.issym():
                os.symlink(m.linkname, dest)
            elif m.islnk():
                target = safe(m.linkname)
                if not target or not os.path.lexists(os.path.join(root, target)):
                    continue
                os.link(os.path.join(root, target), dest, follow_symlinks=False)
            elif m.isfile():
                with tf.extractfile(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, 0o644)
            elif m.isfifo():
                os.mkfifo(dest, 0o644)
            elif m.ischr() or m.isblk():
                pass    # created by debugfs (mknod) after mkfs
            else:
                continue
            entries.append((path, m))
    return entries


def fixups(entries, xattr_dir):
    """The debugfs commands that give every entry its real metadata."""
    cmds, skipped, n = [], 0, 0
    for path, m in entries:
        q = quote(path)
        if q is None:
            skipped += 1
            continue
        if m.islnk():
            continue                                    # same inode as its target
        if m.ischr() or m.isblk():   # debugfs mknod takes a name in its current directory
            parent, name = os.path.split(path)
            cmds += [f'cd "/{parent}"', f"mknod {name} {'c' if m.ischr() else 'b'} {m.devmajor} {m.devminor}",
                     "cd /"]
        mode = TYPE_BITS.get(m.type, stat.S_IFREG) | (m.mode & 0o7777)
        cmds += [f"sif {q} mode 0{mode:o}", f"sif {q} uid {m.uid}", f"sif {q} gid {m.gid}",
                 f"sif {q} mtime {int(m.mtime)}"]
        for key, value in (m.pax_headers or {}).items():
            if key.startswith("SCHILY.xattr."):
                n += 1
                vf = os.path.join(xattr_dir, str(n))
                with open(vf, "wb") as f:
                    f.write(value.encode("utf-8", "surrogateescape"))
                cmds.append(f"ea_set -f {vf} {q} {key[len('SCHILY.xattr.'):]}")
    return cmds, skipped


def main():
    args = sys.argv[1:]
    if len(args) < 3 or "--" not in args:
        sys.exit("usage: tar2ext4.py TAR MKFS-OPTIONS... -- DEVICE [SIZE]")
    tar, sep = args[0], args.index("--")
    opts, rest = args[1:sep], args[sep + 1:]
    work = tempfile.mkdtemp(prefix="tar2ext4-", dir=os.environ.get("TMPDIR"))
    try:
        root = os.path.join(work, "root")
        os.mkdir(root)
        entries = unpack(tar, root)
        subprocess.run(["mkfs.ext4", *opts, "-d", root, *rest], check=True)
        cmds, skipped = fixups(entries, work)
        script = os.path.join(work, "fixups")
        with open(script, "w") as f:
            f.write("\n".join(cmds) + "\n")
        r = subprocess.run(["debugfs", "-w", "-f", script, rest[0]], capture_output=True, text=True)
        errors = [l for l in r.stderr.splitlines() if l.strip() and not l.startswith("debugfs ")]
        if r.returncode != 0 or errors:
            sys.exit("tar2ext4: debugfs failed:\n" + "\n".join(errors[:20]))
        if skipped:
            print(f"==> {skipped} name(s) debugfs can't express kept default owners", file=sys.stderr)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
