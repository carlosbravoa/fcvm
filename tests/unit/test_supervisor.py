"""The supervisor's decisions (lib/web/supervisor.py): what counts as a
failure, and how a VM directory's pid files are read."""
import os
import tempfile
import time
import unittest

from helpers import ROOT  # noqa: F401
import supervisor as sv


class Failed(unittest.TestCase):
    def test_cases(self):
        self.assertFalse(sv.failed({"code": 0, "fc_status": 0}))       # an app exited 0
        self.assertTrue(sv.failed({"code": 3, "fc_status": 0}))        # non-zero exit
        self.assertFalse(sv.failed({"code": None, "fc_status": 0}))    # the guest shut down
        self.assertTrue(sv.failed({"code": None, "fc_status": -9}))    # killed
        self.assertTrue(sv.failed({"stale": True}))                    # the host went down under it
        self.assertFalse(sv.failed({"code": None, "fc_status": None})) # unknown: not a failure


class VmState(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-sv-")

    def write(self, name, text, age=0):
        p = os.path.join(self.d, name)
        with open(p, "w") as f:
            f.write(text)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))

    def test_no_pid_file(self):
        self.assertEqual(sv.vm_state(self.d), "stopped")

    def test_live_process_with_matching_identity(self):
        pid = os.getpid()
        self.write("pid", str(pid))
        self.write("pid.id", f"{sv.boot_id()} {sv.proc_start(pid)}")
        self.assertEqual(sv.vm_state(self.d), "running")

    def test_reused_pid_is_stale(self):
        pid = os.getpid()                       # alive, but not the process that was recorded
        self.write("pid", str(pid))
        self.write("pid.id", f"{sv.boot_id()} 1")
        self.assertEqual(sv.vm_state(self.d), "stale")

    def test_no_identity_and_not_firecracker_is_stale(self):
        self.write("pid", str(os.getpid()))    # our comm is python, not firecracker
        self.assertEqual(sv.vm_state(self.d), "stale")

    def test_dead_process_other_boot_is_stale(self):
        self.write("pid", "999999999")
        self.write("pid.id", "another-boot 1")
        self.assertEqual(sv.vm_state(self.d), "stale")

    def test_dead_process_just_exited_is_exiting(self):
        self.write("pid", "999999999")
        self.write("pid.id", f"{sv.boot_id()} 1")
        self.assertEqual(sv.vm_state(self.d), "exiting")

    def test_dead_process_long_ago_is_stale(self):
        self.write("pid", "999999999", age=sv.REAP_GRACE + 5)
        self.write("pid.id", f"{sv.boot_id()} 1")
        self.assertEqual(sv.vm_state(self.d), "stale")


if __name__ == "__main__":
    unittest.main()
