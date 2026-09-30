"""Jailed VMs: Firecracker under its own uid, in a chroot and network namespace."""
import os
import unittest

from fcvmtest import VMTestCase, fcvm, VMS, jail_usable


@unittest.skipUnless(jail_usable(), "fcvm-jaild isn't set up for this state directory")
class Jail(VMTestCase):
    def test_jailed_vm(self):
        vm = self.vm("--jail")
        pid = self.inspect(vm)["pid"]
        with open(f"/proc/{pid}/status") as f:
            uid = int(next(l for l in f if l.startswith("Uid:")).split()[1])
        self.assertGreaterEqual(uid, 900000)
        with open(f"/proc/{pid}/net/dev") as f:           # its own network namespace
            ifaces = {l.split(":")[0].strip() for l in f.readlines()[2:]}
        self.assertEqual(ifaces, {"lo", "veth0", "br0", "tap0"})
        self.assertEqual(fcvm("exec", vm, "--", "echo", "inside").stdout.strip(), "inside")
        self.assertTrue(os.path.islink(os.path.join(VMS, vm, "fc.sock")))   # links into the chroot

    def test_cleanup_after_stop(self):
        vm = self.vm("--jail")
        fcvm("stop", vm)
        self.assertFalse(os.path.exists(f"/srv/jailer/firecracker/{vm}"))


if __name__ == "__main__":
    unittest.main()
