"""VM lifecycle: run, create/start/stop/rm, exec, cp, inspect."""
import json
import os
import tempfile
import unittest

from fcvmtest import VMTestCase, fcvm, IMAGE, unique


class Run(VMTestCase):
    def test_exit_code_and_output(self):
        r = fcvm("run", IMAGE, "--", "sh", "-c", "echo out-marker; exit 3", check=False)
        self.assertEqual(r.returncode, 3)
        self.assertIn("out-marker", r.stdout)

    def test_throwaway_vm_is_deleted(self):
        before = {v["name"] for v in json.loads(fcvm("ls", "--json").stdout)}
        fcvm("run", IMAGE, "--", "true")
        after = {v["name"] for v in json.loads(fcvm("ls", "--json").stdout)}
        self.assertEqual(before, after)


class Lifecycle(VMTestCase):
    def test_create_start_exec_stop_rm(self):
        vm = self.vm()
        info = self.inspect(vm)
        self.assertEqual(info["state"], "running")
        self.assertTrue(info["ip"])
        self.assertEqual(fcvm("exec", vm, "--", "echo", "hi").stdout.strip(), "hi")
        again = fcvm("start", vm, check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already running", again.stderr)
        fcvm("stop", vm)
        info = self.inspect(vm)
        self.assertIn(info["state"], ("stopped", "exited"))   # an app VM shows its exit, like docker stop
        self.assertTrue(info["stopped_by_user"])
        fcvm("rm", vm)
        self.assertNotEqual(fcvm("inspect", vm, check=False).returncode, 0)

    def test_restart_keeps_the_disk(self):
        vm = self.vm()
        self.sh(vm, "echo kept > /root/f")
        fcvm("stop", vm)
        fcvm("start", vm)
        self.assertEqual(self.sh(vm, "cat /root/f").stdout.strip(), "kept")


class Exec(VMTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.name = unique("exec")
        fcvm("create", cls.name, IMAGE, "--idle")
        fcvm("start", cls.name)

    @classmethod
    def tearDownClass(cls):
        fcvm("stop", cls.name, check=False)
        fcvm("rm", cls.name, check=False)

    def test_separate_streams_and_status(self):
        r = fcvm("exec", self.name, "--", "sh", "-c", "echo o; echo e >&2; exit 7", check=False)
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (7, "o", "e"))

    def test_user_workdir_env(self):
        out = fcvm("exec", "-u", "nobody", "-w", "/tmp", "-e", "X=42", self.name, "--",
                   "sh", "-c", 'echo "$(id -un) $(pwd) $X"').stdout.strip()
        self.assertEqual(out, "nobody /tmp 42")

    def test_timeout(self):
        r = fcvm("exec", "--timeout", "1", self.name, "--", "sleep", "30", check=False)
        self.assertEqual(r.returncode, 124)

    def test_stdin(self):
        r = fcvm("exec", "-i", self.name, "--", "wc", "-c", input="12345")
        self.assertEqual(r.stdout.strip(), "5")

    def test_cp_roundtrip(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-cp-")
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("payload")
        fcvm("cp", os.path.join(d, "a.txt"), f"{self.name}:/tmp/a.txt")
        out = os.path.join(d, "back.txt")
        fcvm("cp", f"{self.name}:/tmp/a.txt", out)
        with open(out) as f:
            self.assertEqual(f.read(), "payload")


class Names(VMTestCase):
    def test_too_long_for_sockets(self):
        r = fcvm("create", "fcvmtest-" + "x" * 110, IMAGE, "--idle", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("too long", r.stderr)


if __name__ == "__main__":
    unittest.main()
