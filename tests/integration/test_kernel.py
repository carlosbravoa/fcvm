"""fcvm kernel: kernels/vmlinux only moves to a build that passes its checks.

These run in a state directory of their own (no network, no service), with
the host's current kernel build under made-up version names, so nothing is
downloaded or built and the real kernels/vmlinux never moves.
"""
import os
import shutil
import tempfile
import unittest

from fcvmtest import IMAGE, STATE, fcvm, require_host


class KernelSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_host()
        real = os.path.realpath(os.path.join(STATE, "kernels", "vmlinux"))
        cls.real_ver = os.path.basename(real).removeprefix("vmlinux-")
        cls.home = tempfile.mkdtemp(prefix="fcvm-kernel-")
        h = cls.home
        for d in ("kernels", "images", "bin", "build"):
            os.makedirs(os.path.join(h, d))
        os.symlink(os.path.join(STATE, "bin", "firecracker"), os.path.join(h, "bin", "firecracker"))
        for f in ("fc-init", "initramfs.cpio", "initramfs.src"):
            shutil.copy(os.path.join(STATE, "build", f), os.path.join(h, "build", f))
        for f in (f"{IMAGE}.json", f"{IMAGE}.ext4"):
            shutil.copy(os.path.join(STATE, "images", f), os.path.join(h, "images", f))
        k = os.path.join(h, "kernels")
        config = os.path.join(STATE, "kernels", f"config-{cls.real_ver}")
        # the real build, under its own version and as a "previous" one
        for v in (cls.real_ver, "1.0.1"):
            os.symlink(real, os.path.join(k, f"vmlinux-{v}"))
            shutil.copy(config, os.path.join(k, f"config-{v}"))
        os.symlink("vmlinux-1.0.1", os.path.join(k, "vmlinux"))
        # a build that isn't a kernel
        with open(os.path.join(k, "vmlinux-1.0.2"), "wb") as f:
            f.write(b"\0" * 4096)
        shutil.copy(config, os.path.join(k, "config-1.0.2"))
        # a build whose config lost a fragment option
        os.symlink(real, os.path.join(k, "vmlinux-1.0.3"))
        with open(config) as src, open(os.path.join(k, "config-1.0.3"), "w") as dst:
            dst.write(src.read().replace("CONFIG_VIRTIO_BLK=y", "# CONFIG_VIRTIO_BLK is not set"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def kernel(self, *args):
        return fcvm("kernel", *args, check=False, timeout=180,
                    env={"FCVM_HOME": self.home, "FCVM_CONF": os.path.join(self.home, "none")})

    def current(self):
        return os.readlink(os.path.join(self.home, "kernels", "vmlinux"))

    def tearDown(self):
        left = os.listdir(os.path.join(self.home, "vms")) if os.path.isdir(os.path.join(self.home, "vms")) else []
        self.assertFalse([v for v in left if v.startswith("kernel-check-")], "the boot test VM was left behind")
        os.remove(os.path.join(self.home, "kernels", "vmlinux"))
        os.symlink("vmlinux-1.0.1", os.path.join(self.home, "kernels", "vmlinux"))

    def test_a_build_that_boots_becomes_the_default(self):
        r = self.kernel(self.real_ver)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"boot test passed: Linux {self.real_ver}", r.stderr)
        self.assertEqual(self.current(), f"vmlinux-{self.real_ver}")

    def test_a_build_that_does_not_boot_is_kept_out(self):
        r = self.kernel("1.0.2")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("kept kernels/vmlinux at vmlinux-1.0.1: it failed the boot test", r.stderr)
        self.assertEqual(self.current(), "vmlinux-1.0.1")

    def test_a_build_that_boots_another_version_is_kept_out(self):
        os.remove(os.path.join(self.home, "kernels", "vmlinux"))
        os.symlink(f"vmlinux-{self.real_ver}", os.path.join(self.home, "kernels", "vmlinux"))
        r = self.kernel("1.0.1")   # the real kernel under another name: uname -r gives it away
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("booted another kernel than 1.0.1", r.stderr)

    def test_a_missing_fragment_option_is_kept_out_and_no_check_overrides(self):
        r = self.kernel("1.0.3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("requested CONFIG_VIRTIO_BLK=y", r.stderr)
        self.assertIn("1 option(s) of kernel/", r.stderr)
        self.assertEqual(self.current(), "vmlinux-1.0.1")
        r = self.kernel("1.0.3", "--no-check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.current(), "vmlinux-1.0.3")


if __name__ == "__main__":
    unittest.main()
