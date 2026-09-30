"""lib/mkfs-tar.sh: an ext4 filesystem from a tarball, owners included, both
directly (e2fsprogs 1.47.1+) and by unpacking in a user namespace (older)."""
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from helpers import LIB

ENTRIES = [   # name, type, mode, uid, gid, data
    ("etc", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("etc/shadow", tarfile.REGTYPE, 0o640, 0, 42, b"secret"),
    ("home", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("home/app", tarfile.DIRTYPE, 0o700, 1000, 1000, b""),
    ("home/app/file", tarfile.REGTYPE, 0o644, 1000, 1000, b"mine"),
    ("usr", tarfile.DIRTYPE, 0o755, 0, 0, b""),
    ("usr/suid", tarfile.REGTYPE, 0o4755, 0, 0, b"#!/bin/sh\n"),
]


def stat(img, path):
    out = subprocess.run(["debugfs", "-R", f"stat {path}", img], capture_output=True, text=True).stdout
    uid = re.search(r"User:\s+(\d+)", out)
    gid = re.search(r"Group:\s+(\d+)", out)
    mode = re.search(r"Mode:\s+(\d+)", out)
    return (int(uid.group(1)), int(gid.group(1)), int(mode.group(1), 8)) if uid and gid and mode else None


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("debugfs"), "needs e2fsprogs")
class MkfsTar(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-mkfs-")
        self.tar = os.path.join(self.d, "root.tar")
        with tarfile.open(self.tar, "w") as tf:
            for name, typ, mode, uid, gid, data in ENTRIES:
                ti = tarfile.TarInfo(name)
                ti.type, ti.mode, ti.uid, ti.gid, ti.size = typ, mode, uid, gid, len(data)
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
        for name, _, mode, uid, gid, _ in ENTRIES:
            self.assertEqual(stat(img, name), (uid, gid, mode), name)

    def test_unpack_in_a_user_namespace(self):
        r, img = self.build(unpack=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.check(img)
        self.assertEqual([f for f in os.listdir(os.path.join(self.d, "build")) if f.startswith("unpack")], [])

    def test_default_way(self):
        r, img = self.build(unpack=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.check(img)


if __name__ == "__main__":
    unittest.main()
