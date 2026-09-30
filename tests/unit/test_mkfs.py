"""lib/mkfs-tar.sh: an ext4 filesystem from a tarball, with owners, modes,
links, device nodes and xattrs, both directly (e2fsprogs 1.47.1+) and by
unpacking and fixing up with debugfs (lib/tar2ext4.py, older e2fsprogs)."""
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from helpers import LIB

ENTRIES = [   # name, type, mode, uid, gid, data (link target for links)
    ("etc", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("etc/shadow", tarfile.REGTYPE, 0o640, 0, 42, b"secret"),
    ("home", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("home/app", tarfile.DIRTYPE, 0o700, 1000, 1000, b""),
    ("home/app/file", tarfile.REGTYPE, 0o644, 1000, 1000, b"mine"),
    ("home/app/with space", tarfile.REGTYPE, 0o600, 1000, 1000, b"x"),
    ("home/big", tarfile.REGTYPE, 0o644, 100000, 100000, b"high ids"),
    ("usr", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("usr/suid", tarfile.REGTYPE, 0o4755, 0, 0, b"#!/bin/sh\n"),
    ("usr/link", tarfile.SYMTYPE, 0o777, 0, 0, b"suid"),
    ("usr/hard", tarfile.LNKTYPE, 0o4755, 0, 0, b"usr/suid"),
    ("dev", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("dev/null", tarfile.CHRTYPE, 0o666, 0, 0, b""),
    ("run", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("run/fifo", tarfile.FIFOTYPE, 0o600, 0, 0, b""),
]
# a file capability (cap_net_raw), the xattr images actually carry
CAP = "\x01\x00\x00\x02\x00\x20\x00\x00" + "\x00" * 12
TYPES = {tarfile.DIRTYPE: 0o040000, tarfile.REGTYPE: 0o100000, tarfile.SYMTYPE: 0o120000,
         tarfile.LNKTYPE: 0o100000, tarfile.CHRTYPE: 0o020000, tarfile.FIFOTYPE: 0o010000}


def stat(img, path):
    out = subprocess.run(["debugfs", "-R", f"stat {path}", img], capture_output=True, text=True).stdout
    uid = re.search(r"User:\s+(\d+)", out)
    gid = re.search(r"Group:\s+(\d+)", out)
    mode = re.search(r"Mode:\s+(\d+)", out)
    kind = re.search(r"Type: (\w+)", out)
    types = {"regular": 0o100000, "directory": 0o040000, "symlink": 0o120000,
             "character": 0o020000, "FIFO": 0o010000, "block": 0o060000}
    if not (uid and gid and mode and kind):
        return None
    return int(uid.group(1)), int(gid.group(1)), types.get(kind.group(1), 0) | int(mode.group(1), 8)


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("debugfs"), "needs e2fsprogs")
class MkfsTar(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-mkfs-")
        self.tar = os.path.join(self.d, "root.tar")
        with tarfile.open(self.tar, "w", format=tarfile.PAX_FORMAT) as tf:
            for name, typ, mode, uid, gid, data in ENTRIES:
                ti = tarfile.TarInfo(name)
                ti.type, ti.mode, ti.uid, ti.gid = typ, mode, uid, gid
                if typ in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    ti.linkname = data.decode()
                    tf.addfile(ti)
                elif typ == tarfile.CHRTYPE:
                    ti.devmajor, ti.devminor = 1, 3
                    tf.addfile(ti)
                else:
                    if name == "usr/suid":
                        ti.pax_headers = {"SCHILY.xattr.security.capability": CAP}
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data) if data else None)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def build(self, unpack):
        img = os.path.join(self.d, f"img-{unpack}.ext4")
        env = {**os.environ, "FCVM_HOME": self.d, "FCVM_MKFS_UNPACK": "1" if unpack else "0"}
        r = subprocess.run([os.path.join(LIB, "mkfs-tar.sh"), self.tar, "-q", "-F", "--", img, "16M"],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0 and "subuid" in r.stdout + r.stderr:
            self.skipTest("unpacking needs a subuid range for this user (fcvm host-setup)")
        return r, img

    def check(self, img):
        for name, typ, mode, uid, gid, _ in ENTRIES:
            self.assertEqual(stat(img, f'"{name}"'), (uid, gid, TYPES[typ] | mode), name)
        dev = subprocess.run(["debugfs", "-R", "stat /dev/null", img], capture_output=True, text=True).stdout
        self.assertRegex(dev, r"Device major/minor number: 0*1:0*3")
        ea = subprocess.run(["debugfs", "-R", "ea_list /usr/suid", img], capture_output=True, text=True).stdout
        self.assertIn("security.capability (20) = 01 00 00 02 00 20", ea)
        link = subprocess.run(["debugfs", "-R", "stat /usr/link", img], capture_output=True, text=True).stdout
        self.assertIn('Fast link dest: "suid"', link)

    def test_unpack_and_fix_up(self):
        r, img = self.build(unpack=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.check(img)
        self.assertEqual([f for f in os.listdir(os.path.join(self.d, "build")) if f.startswith("tar2ext4-")], [])

    def test_default_way(self):
        r, img = self.build(unpack=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.check(img)


if __name__ == "__main__":
    unittest.main()
