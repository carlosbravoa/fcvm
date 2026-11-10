"""Named volumes and live host directories."""
import os
import tempfile
import time
import unittest

from fcvmtest import VMTestCase, fcvm, IMAGE, VMS, allocated_mib, wait_for


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

    def test_home_directory_shorthand(self):
        # just a host directory in your home: the same place in the guest user's home
        d = tempfile.mkdtemp(prefix="fcvmtest-", dir=os.path.expanduser("~"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        with open(os.path.join(d, "f"), "w") as f:
            f.write("home")
        vm = self.vm("-v", d)
        rel = os.path.relpath(d, os.path.realpath(os.path.expanduser("~")))
        self.assertEqual(self.inspect(vm)["shares"][0]["path"], f"~/{rel}")
        self.assertEqual(self.sh(vm, f"cat /root/{rel}/f").stdout, "home")     # alpine runs as root
        fcvm("umount", vm, d)                                                   # by host directory
        self.assertEqual(self.inspect(vm)["shares"], [])

    def test_same_path_outside_home(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-abs-")
        with open(os.path.join(d, "f"), "w") as f:
            f.write("abs")
        vm = self.vm("-v", d)
        self.assertEqual(self.sh(vm, f"cat {d}/f").stdout, "abs")

    def test_guest_home_path(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-share-")
        vm = self.vm("-v", f"{d}:~/data:ro")
        self.assertIn("data", self.sh(vm, "ls /root").stdout)

    def test_live_mount_and_umount(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-share-")
        with open(os.path.join(d, "f"), "w") as f:
            f.write("mounted")
        vm = self.vm()
        fcvm("mount", vm, f"{d}:/late")
        self.assertEqual(self.sh(vm, "cat /late/f").stdout, "mounted")
        fcvm("umount", vm, "/late")
        self.assertNotEqual(self.sh(vm, "cat /late/f", check=False).returncode, 0)


class Discard(VMTestCase):
    """virtio-blk discard: deleting files in the guest reclaims host disk space."""

    def test_overlay_backing_file_shrinks_after_delete(self):
        vm = self.vm()
        backing = os.path.join(VMS, vm, "rw.ext4")
        base = allocated_mib(backing)
        self.sh(vm, "dd if=/dev/zero of=/root/big bs=1M count=200 2>/dev/null; sync")
        self.assertGreater(allocated_mib(backing), base + 150,
                           "writing 200 MiB should grow the writable layer's backing file")
        self.sh(vm, "rm -f /root/big; sync")
        # The overlay's rw layer is mounted -o discard, so ext4 issues TRIM as it
        # frees blocks and Firecracker punches holes in the backing file; no
        # fstrim needed. It returns close to its original size.
        wait_for(lambda: allocated_mib(backing) < base + 50, timeout=10,
                 what="the backing file to shrink after delete")


if __name__ == "__main__":
    unittest.main()
