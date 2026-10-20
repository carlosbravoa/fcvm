"""Events: the lifecycle leaves a trail in vms/.events.jsonl (docs/events.md)."""
import json
import os

from fcvmtest import VMTestCase, fcvm, VMS


def last_seq():
    try:
        with open(os.path.join(VMS, ".events.seq")) as f:
            return int(f.read().strip() or 0)
    except OSError:
        return 0


class Events(VMTestCase):
    def events(self, vm, since):
        return [json.loads(l) for l in fcvm("events", "--since", since, "--json", *([vm] if vm else [])).stdout.splitlines() if l]

    def test_lifecycle_trail(self):
        before = last_seq()
        vm = self.vm("--label", "k=v", start=False)
        fcvm("start", vm)
        fcvm("stop", vm, env={"FCVM_EXPIRED": "ttl"})       # as the supervisor stops an expired VM
        fcvm("rm", vm)
        evs = self.events(vm, before)
        types = [e["type"] for e in evs]
        self.assertEqual(types[0], "vm.created")
        self.assertEqual(types[-1], "vm.deleted")
        self.assertEqual(set(types), {"vm.created", "vm.started", "vm.exited", "vm.stopped", "vm.deleted"})
        self.assertEqual([e["seq"] for e in evs], sorted(e["seq"] for e in evs))
        self.assertTrue(all(e["seq"] > before for e in evs))
        by_type = {e["type"]: e for e in evs}
        self.assertEqual(by_type["vm.stopped"]["reason"], "ttl")
        self.assertIn(by_type["vm.exited"]["reason"], ("exit", "shutdown"))   # an idle app VM's init exits 0 on Ctrl-Alt-Del
        self.assertEqual(by_type["vm.created"]["labels"], {"k": "v"})
        self.assertGreater(by_type["vm.started"]["data"]["pid"], 0)
        self.assertTrue(by_type["vm.created"]["by"].startswith("cli:") or by_type["vm.created"]["by"] == "serve")
        self.assertIn("vm.started", fcvm("events", "--since", before, vm).stdout)   # the human format

    def test_update_and_run(self):
        vm = self.vm(start=False)
        seq = last_seq()
        fcvm("update", vm, "--ttl", "1h", "--label", "a=b")
        ev = self.events(vm, seq)[-1]
        self.assertEqual(ev["type"], "vm.updated")
        self.assertTrue(any("ttl" in w for w in ev["data"]["what"]))
        fcvm("run", "alpine-latest", "--", "true")
        evs = [e for e in self.events(None, seq) if e["type"] == "vm.deleted" and e["reason"] == "ephemeral"]
        self.assertTrue(evs, "a throwaway VM's end is recorded")
