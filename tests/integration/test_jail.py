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

    def test_secrets_and_live_ports_in_a_jail(self):
        """The secrets server and the port forwarder work through the chroot's sockets."""
        import socket
        import time
        vm = self.vm("--jail", "--secret", "TOKEN=jailed-secret")
        self.assertEqual(fcvm("exec", vm, "--", "sh", "-c", "cat /run/secrets/TOKEN; echo; echo $TOKEN").stdout.split(),
                         ["jailed-secret", "jailed-secret"])
        fcvm("exec", vm, "--", "sh", "-c", 'setsid sh -c "echo from-jail | nc -l -p 8000" >/dev/null 2>&1 </dev/null &')
        spec = fcvm("publish", vm, ":8000").stdout.strip()
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", int(spec.split(":")[0])), timeout=5) as s:
            self.assertEqual(s.recv(100).strip(), b"from-jail")

    def test_cleanup_after_stop(self):
        vm = self.vm("--jail")
        fcvm("stop", vm)
        self.assertFalse(os.path.exists(f"/srv/jailer/firecracker/{vm}"))


if __name__ == "__main__":
    unittest.main()
