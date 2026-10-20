"""Tenants and admission (docs/vms.md, Tenants and quotas): the tenant label,
quotas, node capacity, and the node view (status --json, config --json)."""
import json
import os

from fcvmtest import VMTestCase, fcvm, IMAGE, unique, VMS, wait_for

LOCAL = {"FCVM_DAEMON": "1"}   # start here, so the environment (NODE_*) is this test's


def events(n=30):
    return [json.loads(l) for l in fcvm("events", "--since", f"-{n}", "--json").stdout.splitlines() if l]


class Tenants(VMTestCase):
    def tenant(self, *args):
        name = unique("tenant")
        fcvm("tenant", "set", name, *args)
        self.addCleanup(fcvm, "tenant", "rm", name, check=False)
        return name

    def test_tenant_label_is_set_once(self):
        t = self.tenant()
        vm = self.vm("--tenant", t, "--label", "team=web", start=False)
        info = self.inspect(vm)
        self.assertEqual(info["labels"], {"tenant": t, "team": "web"})
        self.assertIn(vm, [v["name"] for v in json.loads(fcvm("ls", "--json", "--tenant", t).stdout)])
        self.assertNotIn(vm, [v["name"] for v in json.loads(fcvm("ls", "--json", "--tenant", "nobody").stdout)])
        for args in (("--label", "tenant=other"), ("--unset-label", "tenant")):
            r = fcvm("update", vm, *args, check=False)
            self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: invalid", r.stderr)
        fcvm("update", vm, "--label", "team=api")                          # other labels still change
        self.assertEqual(self.inspect(vm)["labels"], {"tenant": t, "team": "api"})
        r = fcvm("create", unique("bad"), IMAGE, "--idle", "--tenant", "no way", check=False)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: invalid")
        shown = json.loads(fcvm("tenant", "show", t).stdout)
        self.assertEqual((shown["vms"], shown["allocated"]["vms"]), ([vm], 0))   # stopped: nothing allocated

    def test_quota_refuses_a_start_that_would_exceed_it(self):
        t = self.tenant("--vcpus", "3", "--vms", "2")
        a = self.vm("--tenant", t, "--vcpus", "2", "--mem", "256")
        b = self.vm("--tenant", t, "--vcpus", "2", "--mem", "256", start=False)
        for env in (LOCAL, {}):                                             # from this terminal, and through the service
            r = fcvm("start", b, check=False, env=env)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: quota_exceeded", r.stderr)
            self.assertIn("vcpus quota: 2 allocated + 2 > 3", r.stderr)
        self.assertEqual(self.inspect(b)["state"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(VMS, b, "starting")))
        fcvm("tenant", "set", t, "--vcpus", "none")                         # room: it starts
        fcvm("start", b)
        self.assertEqual(self.inspect(b)["state"], "running")
        shown = json.loads(fcvm("tenant", "show", t).stdout)
        self.assertEqual(shown["allocated"], {"vcpus": 4, "mem_mib": 512, "vms": 2})
        c = self.vm("--tenant", t, "--vcpus", "1", "--mem", "256", start=False)
        r = fcvm("start", c, check=False, env=LOCAL)
        self.assertIn("vms quota: 2 allocated + 1 > 2", r.stderr)           # the count, now
        fcvm("stop", a)
        fcvm("start", c, env=LOCAL)                                        # a stopped VM frees its share
        self.assertEqual(self.inspect(c)["state"], "running")

    def test_fork_and_claim_take_a_tenant(self):
        t = self.tenant("--vms", "1")
        src = self.vm("--mem", "256")
        snap = self.snapshot(src)
        name = unique("fork")
        fcvm("fork", snap, name, "--tenant", t, env=LOCAL)
        self.addCleanup(self.remove_vm, name)
        self.assertEqual(self.inspect(name)["labels"].get("tenant"), t)
        second = unique("fork")
        r = fcvm("fork", snap, second, "--tenant", t, check=False, env=LOCAL)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: quota_exceeded", r.stderr)
        self.assertFalse(os.path.isdir(os.path.join(VMS, second)))        # nothing left behind
        # a pooled VM (no tenant) goes to the tenant that claims it, within its quota
        tpl, pool = unique("tpl"), unique("pool")
        self.addCleanup(fcvm, "template", "rm", tpl, check=False)
        self.addCleanup(fcvm, "pool", "rm", pool, check=False)
        fcvm("template", "save", tpl, IMAGE, "--mem", "256", "--idle")
        fcvm("pool", "set", pool, "--template", tpl, "--size", "1")

        def ready():
            p = next((p for p in json.loads(fcvm("pool", "ls", "--json").stdout) if p["name"] == pool), None)
            return p and p["ready"] == 1
        try:
            wait_for(ready, 60, what="the pool to fill")
        except AssertionError:
            self.skipTest("no fcvm service filling pools")
        r = fcvm("pool", "claim", pool, "--tenant", t, check=False)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: quota_exceeded", r.stderr)   # the fork holds the slot
        pooled = [p for p in json.loads(fcvm("pool", "ls", "--json").stdout) if p["name"] == pool][0]
        self.assertEqual(pooled["ready"], 1)                                # still in the pool
        fcvm("tenant", "set", t, "--vms", "2")
        vm = fcvm("pool", "claim", pool, "--tenant", t, "--label", "job=1").stdout.strip()
        self.addCleanup(self.remove_vm, vm)
        self.assertEqual(self.inspect(vm)["labels"], {"tenant": t, "job": "1"})
        ev = next(e for e in reversed(events()) if e["type"] == "pool.claimed")
        self.assertEqual((ev["vm"], ev["data"]["tenant"]), (vm, t))
        self.assertIn("pool.set", [e["type"] for e in events(60)])

    def test_node_capacity_when_enforced(self):
        vm = self.vm("--vcpus", "1", "--mem", "256", start=False)
        env = {**LOCAL, "NODE_ADMIT": "1", "NODE_VCPUS": "1", "NODE_MEM_MIB": "100000"}
        running = json.loads(fcvm("status", "--json").stdout)["allocated"]["vcpus"]
        r = fcvm("start", vm, check=False, env=env)
        if running == 0:
            self.assertEqual(r.returncode, 0, r.stderr)                    # exactly fits
            fcvm("stop", vm)
            env["NODE_VCPUS"] = "0"
            r = fcvm("start", vm, check=False, env=env)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: node_full", r.stderr)
        fcvm("start", vm, env={**LOCAL, "NODE_ADMIT": "0", "NODE_VCPUS": "0"})   # not enforced: starts
        self.assertEqual(self.inspect(vm)["state"], "running")

    def test_status_and_config_json(self):
        vm = self.vm("--vcpus", "1", "--mem", "256")
        s = json.loads(fcvm("status", "--json").stdout)
        self.assertEqual(s["contract"], 1)
        self.assertGreaterEqual(s["allocated"]["vms"], 1)
        self.assertEqual(s["allocatable"]["vcpus"], s["capacity"]["vcpus"] - s["allocated"]["vcpus"])
        self.assertGreaterEqual(s["vms"]["running"], 1)
        self.assertIn("taps_full", s["slots"])
        self.assertTrue(s["kernel"].startswith("vmlinux-"))
        self.assertTrue(s["firecracker"].startswith("v"))
        self.assertIsInstance(s["admission"], bool)
        c = json.loads(fcvm("config", "--json").stdout)
        self.assertEqual((c["contract"], c["paths"]["vms"]), (1, VMS))
        self.assertEqual(c["settings"]["NODE_ADMIT"], "0" if not os.environ.get("NODE_ADMIT") else os.environ["NODE_ADMIT"])
        self.assertIn("NET_TAPS", c["settings"])
        self.assertNotIn("REGISTRY_PASSWORD", c["settings"])
        c2 = json.loads(fcvm("config", "--json", env={"VM_MEM_MIB": "777"}).stdout)
        self.assertEqual(c2["settings"]["VM_MEM_MIB"], "777")
        text = fcvm("config").stdout
        self.assertIn("VM_VCPUS=", text)
        self.assertIn(f"vms={VMS}", text)
        self.assertEqual(fcvm("config", "--nope", check=False).stderr.splitlines()[-1], "fcvm-error: invalid")
        self.assertIn(vm, fcvm("ls").stdout)
