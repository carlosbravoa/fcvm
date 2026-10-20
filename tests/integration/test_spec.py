"""spec, status and labels in inspect; create --if-same-spec."""
import json

from fcvmtest import VMTestCase, fcvm, IMAGE, unique, wait_for


class SpecStatusLabels(VMTestCase):
    def test_labels_on_create_update_ls(self):
        vm = self.vm("--label", "team=a", "--label", "env=ci", start=False)
        self.assertEqual(self.inspect(vm)["labels"], {"team": "a", "env": "ci"})
        fcvm("update", vm, "--label", "env=prod", "--unset-label", "team")
        self.assertEqual(self.inspect(vm)["labels"], {"env": "prod"})
        names = [v["name"] for v in json.loads(fcvm("ls", "--json", "--label", "env=prod").stdout)]
        self.assertIn(vm, names)
        self.assertNotIn(vm, [v["name"] for v in json.loads(fcvm("ls", "--json", "--label", "env=ci").stdout)])
        self.assertIn(vm, fcvm("ls", "--label", "env=prod").stdout)
        r = fcvm("update", vm, "--label", "fcvm.x=1", check=False)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: invalid")

    def test_spec_and_if_same_spec(self):
        vm = self.vm("--mem", "256", "--label", "x=1", start=False)
        spec = self.inspect(vm)["spec"]
        self.assertEqual((spec["image"], spec["mem_mib"], spec["process"], spec["network"]), (IMAGE, 256, "idle", "full"))
        self.assertNotIn("labels", spec)                                    # labels aren't part of the spec
        r = fcvm("create", vm, IMAGE, "--idle", "--mem", "256", "--if-same-spec")   # a retry: same spec, other labels
        self.assertIn("already exists with this spec", r.stderr)
        self.assertEqual(self.inspect(vm)["labels"], {"x": "1"})          # untouched
        r = fcvm("create", vm, IMAGE, "--idle", "--mem", "512", "--if-same-spec", check=False)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: vm_exists")
        r = fcvm("create", vm, IMAGE, "--idle", "--mem", "256", check=False)   # without the flag: as always
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: vm_exists")

    def test_status_reason(self):
        vm = self.vm(start=False)
        st = self.inspect(vm)["status"]
        self.assertEqual((st["state"], st["reason"]), ("stopped", "created"))
        self.assertGreater(st["since"], 0)
        fcvm("start", vm)
        st = self.inspect(vm)["status"]
        self.assertEqual((st["state"], st["reason"]), ("running", "start"))
        fcvm("stop", vm)
        st = self.inspect(vm)["status"]
        self.assertEqual(st["reason"], "user")
        exited = self.vm(cmd=["sh", "-c", "exit 0"], kind="exit")
        wait_for(lambda: self.inspect(exited)["status"]["reason"] == "exit", 20, what="the app's exit")
        self.assertEqual(self.inspect(exited)["status"]["state"], "exited")

    def test_fork_and_claim_take_labels(self):
        vm = self.vm()
        snap = self.snapshot(vm)
        fork = unique("fork")
        self.addCleanup(self.remove_vm, fork)
        fcvm("fork", snap, fork, "--label", "attempt=1", env={"FCVM_DAEMON": "1"})
        self.assertEqual(self.inspect(fork)["labels"], {"attempt": "1"})
