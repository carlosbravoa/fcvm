"""Snapshots and fork, rootless and jailed."""
import json
import os
import shutil
import tempfile
import unittest

from fcvmtest import VMTestCase, fcvm, unique, jail_usable


class Snapshots(VMTestCase):
    def check_fork(self, *opts):
        src = self.vm(*opts)
        self.sh(src, "echo state > /root/marker; setsid sleep 1000 >/dev/null 2>&1 </dev/null & echo $! > /root/pid")
        snap = self.snapshot(src)
        fork = unique("fork")
        self.addCleanup(self.remove_vm, fork)
        fcvm("fork", snap, fork, timeout=120)
        a, b = self.inspect(src), self.inspect(fork)
        self.assertEqual(b["state"], "running")
        self.assertNotEqual(a["ip"], b["ip"])
        self.assertEqual(self.sh(fork, "cat /root/marker").stdout.strip(), "state")
        self.assertIn("alive", self.sh(fork, 'kill -0 "$(cat /root/pid)" && echo alive').stdout)   # processes carried over
        self.assertEqual(self.sh(fork, "hostname").stdout.strip(), fork)
        self.assertEqual(self.sh(src, "hostname").stdout.strip() != fork, True)
        return snap

    def test_fork(self):
        self.check_fork()

    @unittest.skipUnless(jail_usable(), "fcvm-jaild isn't set up for this state directory")
    def test_fork_jailed(self):
        self.check_fork("--jail")

    def check_export_import(self, *opts):
        src = self.vm(*opts)
        self.sh(src, "echo moved > /root/marker")
        snap = self.snapshot(src)
        d = tempfile.mkdtemp(prefix="fcvmtest-snap-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        tar = os.path.join(d, f"{snap}.tar")
        fcvm("snapshot", "export", snap, tar, timeout=300)
        self.assertTrue(os.path.exists(tar))
        back = unique("snap")
        self.addCleanup(fcvm, "snapshot", "rm", back, check=False)
        fcvm("snapshot", "import", tar, back, timeout=300)
        self.assertIn(back, [s["name"] for s in json.loads(fcvm("snapshot", "ls", "--json").stdout)])
        self.assertNotEqual(fcvm("snapshot", "import", tar, back, check=False).returncode, 0)   # exists
        fcvm("snapshot", "rm", snap)                      # the original is gone; the import stands on its own
        fork = unique("fork")
        self.addCleanup(self.remove_vm, fork)
        fcvm("fork", back, fork, timeout=120)
        self.assertEqual(self.sh(fork, "cat /root/marker").stdout.strip(), "moved")
        self.assertEqual(self.sh(fork, "hostname").stdout.strip(), fork)

    def test_export_import(self):
        self.check_export_import()

    @unittest.skipUnless(jail_usable(), "fcvm-jaild isn't set up for this state directory")
    def test_export_import_jailed(self):
        self.check_export_import("--jail")

    def test_import_needs_the_images(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-snap-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        r = fcvm("snapshot", "import", os.path.join(d, "missing.tar"), check=False)
        self.assertNotEqual(r.returncode, 0)
        with open(os.path.join(d, "junk.tar"), "wb") as f:
            f.write(b"not a tar")
        r = fcvm("snapshot", "import", os.path.join(d, "junk.tar"), check=False)
        self.assertIn("isn't an fcvm snapshot export", r.stderr)

    def test_fork_clock_is_current(self):
        # The guest clock stops at the snapshot; a fork must show the host's time.
        import time
        snap = self.snapshot(self.vm())
        time.sleep(4)
        fork = unique("fork")
        self.addCleanup(self.remove_vm, fork)
        fcvm("fork", snap, fork, timeout=120)
        guest = int(self.sh(fork, "date +%s").stdout.strip())
        self.assertLess(abs(guest - time.time()), 2)

    def test_forks_are_independent(self):
        src = self.vm()
        snap = self.snapshot(src)
        names = []
        for _ in range(2):
            n = unique("fork")
            self.addCleanup(self.remove_vm, n)
            fcvm("fork", snap, n, timeout=120)
            names.append(n)
        self.sh(names[0], "echo only-here > /root/x")
        self.assertNotEqual(self.sh(names[1], "cat /root/x", check=False).returncode, 0)

    def test_snapshot_listed(self):
        snap = self.snapshot(self.vm())
        self.assertIn(snap, [s["name"] for s in json.loads(fcvm("snapshot", "ls", "--json").stdout)])


if __name__ == "__main__":
    unittest.main()
