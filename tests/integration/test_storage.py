"""Named volumes and live host directories."""
import os
import tempfile
import time
import unittest

from fcvmtest import VMTestCase, fcvm, IMAGE


class Volumes(VMTestCase):
    def test_volume_outlives_the_vm(self):
        vol = self.volume()
        a = self.vm("-v", f"{vol}:/data")
        self.sh(a, "echo persisted > /data/f")
        self.remove_vm(a)
        b = self.vm("-v", f"{vol}:/data:ro")
        self.assertEqual(self.sh(b, "cat /data/f").stdout.strip(), "persisted")
        self.assertNotEqual(self.sh(b, "echo x > /data/g", check=False).returncode, 0)   # :ro

    def test_rw_volume_is_exclusive(self):
        vol = self.volume()
        self.vm("-v", f"{vol}:/data")
        other = self.vm("-v", f"{vol}:/data", start=False)
        r = fcvm("start", other, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("in use", r.stderr)


class HostDirectories(VMTestCase):
    def test_live_both_ways(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-share-")
        with open(os.path.join(d, "from-host"), "w") as f:
            f.write("h")
        vm = self.vm("-v", f"{d}:/share")
        self.assertEqual(self.sh(vm, "cat /share/from-host").stdout, "h")
        self.sh(vm, "echo g > /share/from-guest")
        with open(os.path.join(d, "from-guest")) as f:
            self.assertEqual(f.read().strip(), "g")
        with open(os.path.join(d, "later"), "w") as f:     # changes show up at once
            f.write("now")
        self.assertEqual(self.sh(vm, "cat /share/later").stdout, "now")

    def test_read_only(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-share-")
        vm = self.vm("-v", f"{d}:/share:ro")
        self.assertNotEqual(self.sh(vm, "touch /share/x", check=False).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(d, "x")))

    def test_live_mount_and_umount(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-share-")
        with open(os.path.join(d, "f"), "w") as f:
            f.write("mounted")
        vm = self.vm()
        fcvm("mount", vm, f"{d}:/late")
        self.assertEqual(self.sh(vm, "cat /late/f").stdout, "mounted")
        fcvm("umount", vm, "/late")
        self.assertNotEqual(self.sh(vm, "cat /late/f", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
