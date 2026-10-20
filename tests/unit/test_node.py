"""The node view (lib/node.py): what's allocated, tenants and their quotas,
admission, status."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import ROOT, bash  # noqa: F401
import node

NODE_PY = os.path.join(ROOT, "lib", "node.py")


class NodeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fcvm-unit-")
        os.environ["FCVM_HOME"] = self.home
        for k in ("NODE_ADMIT", "NODE_VCPUS", "NODE_MEM_MIB", "NODE_VMS", "NODE_CPU_OVERCOMMIT", "NODE_MEM_OVERCOMMIT", "NODE_RESERVE_MIB",
                  "FCVM_VERSION", "FIRECRACKER_VERSION"):
            os.environ.pop(k, None)
        node.STATE, node.VMS = self.home, os.path.join(self.home, "vms")
        os.makedirs(node.VMS)

    def tearDown(self):
        os.environ.pop("FCVM_HOME", None)

    def make(self, name, vcpus=2, mem=1024, tenant=None, state="stopped"):
        d = os.path.join(node.VMS, name)
        os.makedirs(d)
        with open(os.path.join(d, "vm.json"), "w") as f:
            json.dump({"vcpus": vcpus, "mem_mib": mem, "labels": {"tenant": tenant} if tenant else {}}, f)
        if state == "running":                 # this very process stands in for Firecracker
            with open(os.path.join(d, "pid"), "w") as f:
                f.write(str(os.getpid()))
            with open(os.path.join(d, "pid.id"), "w") as f:
                f.write(f"{node.boot_id()} {node.proc_start(os.getpid())}")
        elif state == "starting":
            with open(os.path.join(d, "starting"), "w"):
                pass
        elif state == "stale-start":
            with open(os.path.join(d, "starting"), "w"):
                pass
            os.utime(os.path.join(d, "starting"), (time.time() - 600, time.time() - 600))
        return d

    def run_py(self, *args, env=None):
        return subprocess.run([sys.executable, NODE_PY, *args], capture_output=True, text=True,
                              env={**os.environ, **(env or {})})


class Allocation(NodeCase):
    def test_running_and_starting_count_stopped_and_stale_dont(self):
        self.make("a", 2, 1024, "acme", "running")
        self.make("b", 4, 2048, None, "starting")
        self.make("c", 8, 8192, "acme", "stopped")
        self.make("d", 8, 8192, "acme", "stale-start")
        self.make("e", 1, 512, "beta", "running")
        os.makedirs(os.path.join(node.VMS, ".tenants"))                      # dot entries are skipped
        os.makedirs(os.path.join(node.VMS, "f"))                             # no vm.json: not a VM
        states = {v["name"]: v["state"] for v in node.vms()}
        self.assertEqual(states, {"a": "running", "b": "starting", "c": "stopped", "d": "stopped", "e": "running"})
        self.assertEqual(node.allocated(node.vms()), {"vcpus": 7, "mem_mib": 3584, "vms": 3})
        self.assertEqual(node.allocated(node.vms(), tenant="acme"), {"vcpus": 2, "mem_mib": 1024, "vms": 1})
        self.assertEqual(node.allocated(node.vms(), exclude="a"), {"vcpus": 5, "mem_mib": 2560, "vms": 2})

    def test_a_stale_pid_is_not_running(self):
        d = self.make("z", state="running")
        with open(os.path.join(d, "pid.id"), "w") as f:
            f.write("other-boot 1")
        self.assertEqual(node.vms()[0]["state"], "stopped")

    def test_capacity_from_the_host_or_settings(self):
        cap, host = node.capacity()
        self.assertEqual(cap["vcpus"], host["cpus"] * 2)
        self.assertEqual(cap["mem_mib"], max(host["mem_mib"] - 1024, 0))
        self.assertIsNone(cap["vms"])
        os.environ.update({"NODE_VCPUS": "7", "NODE_VMS": "3", "NODE_MEM_OVERCOMMIT": "1.5", "NODE_RESERVE_MIB": "0"})
        cap, host = node.capacity()
        self.assertEqual((cap["vcpus"], cap["vms"], cap["mem_mib"]), (7, 3, int(host["mem_mib"] * 1.5)))
        self.assertFalse(node.enforced())
        os.environ["NODE_ADMIT"] = "1"
        self.assertTrue(node.enforced())


class Tenants(NodeCase):
    def test_set_show_ls_rm(self):
        r = self.run_py("tenant", "set", "acme", "--vcpus", "4", "--mem", "2048")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(node.quota_of("acme"), {"vcpus": 4, "mem_mib": 2048, "vms": None})
        r = self.run_py("tenant", "set", "acme", "--vcpus", "none", "--vms", "2")   # changes merge
        self.assertEqual(node.quota_of("acme"), {"vcpus": None, "mem_mib": 2048, "vms": 2})
        self.make("a", 2, 512, "acme", "running")
        self.make("b", 2, 512, "zeta", "running")                            # a tenant with no quota file
        shown = json.loads(self.run_py("tenant", "show", "acme").stdout)
        self.assertEqual((shown["quota"]["vms"], shown["allocated"], shown["vms"]), (2, {"vcpus": 2, "mem_mib": 512, "vms": 1}, ["a"]))
        listed = json.loads(self.run_py("tenant", "ls", "--json").stdout)
        self.assertEqual([t["name"] for t in listed], ["acme", "zeta"])
        self.assertEqual(listed[1]["quota"], {"vcpus": None, "mem_mib": None, "vms": None})
        text = self.run_py("tenant", "ls").stdout
        self.assertIn("acme", text)
        self.assertIn("2/2048", text)
        with open(os.path.join(node.VMS, ".events.jsonl")) as f:
            evs = [json.loads(l) for l in f]
        self.assertEqual([e["type"] for e in evs], ["tenant.set", "tenant.set"])
        self.assertEqual(evs[-1]["data"], {"tenant": "acme", "quota": {"vcpus": None, "mem_mib": 2048, "vms": 2}})
        r = self.run_py("tenant", "rm", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(node.tenant("acme"))
        self.assertEqual(self.run_py("tenant", "rm", "acme").stderr.splitlines()[-1], "fcvm-error: tenant_not_found")
        self.assertEqual(self.run_py("tenant", "show", "nobody").stderr.splitlines()[-1], "fcvm-error: tenant_not_found")
        self.assertEqual(self.run_py("tenant", "set", "bad name").stderr.splitlines()[-1], "fcvm-error: invalid")
        self.assertEqual(self.run_py("tenant", "set", "acme", "--vcpus", "many").stderr.splitlines()[-1], "fcvm-error: invalid")
        self.assertEqual(self.run_py("tenant", "set", "acme", "--disk", "1").stderr.splitlines()[-1], "fcvm-error: invalid")


class Admission(NodeCase):
    def test_tenant_quota(self):
        self.run_py("tenant", "set", "acme", "--vcpus", "4", "--vms", "2")
        self.make("a", 2, 1024, "acme", "running")
        self.make("b", 2, 1024, "acme")
        r = self.run_py("admit", "b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(node.VMS, "b", "starting")))   # counts from now on
        self.make("c", 1, 256, "acme")
        r = self.run_py("admit", "c")
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: quota_exceeded")
        self.assertIn("vcpus quota: 4 allocated + 1 > 4", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(node.VMS, "c", "starting")))
        os.unlink(os.path.join(node.VMS, "b", "starting"))
        self.assertEqual(self.run_py("admit", "c").returncode, 0)                   # room again
        self.make("d", 1, 256, "nobody")
        self.assertEqual(self.run_py("admit", "d").returncode, 0)                   # no quota file: no limit
        self.assertEqual(self.run_py("admit", "ghost").stderr.splitlines()[-1], "fcvm-error: vm_not_found")

    def test_a_running_vm_being_admitted_again_is_not_counted_twice(self):
        self.run_py("tenant", "set", "acme", "--vms", "1")
        self.make("a", 2, 1024, "acme", "running")
        self.assertEqual(self.run_py("admit", "a").returncode, 0)                   # a restart of itself

    def test_claim_checks_the_tenant_only(self):
        self.run_py("tenant", "set", "acme", "--mem", "1024")
        self.make("pooled", 2, 1024, None, "running")
        self.make("mine", 2, 512, "acme", "running")
        r = self.run_py("admit", "--claim", "--tenant", "acme", "pooled", env={"NODE_ADMIT": "1", "NODE_VCPUS": "1"})
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: quota_exceeded")
        self.run_py("tenant", "set", "acme", "--mem", "2048")
        r = self.run_py("admit", "--claim", "--tenant", "acme", "pooled", env={"NODE_ADMIT": "1", "NODE_VCPUS": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)                                 # the node is full, but it runs already
        self.assertFalse(os.path.exists(os.path.join(node.VMS, "pooled", "starting")))

    def test_node_capacity_only_when_enforced(self):
        self.make("a", 4, 4096, None, "running")
        self.make("b", 4, 4096)
        env = {"NODE_VCPUS": "6", "NODE_MEM_MIB": "100000"}
        self.assertEqual(self.run_py("admit", "b", env=env).returncode, 0)
        os.unlink(os.path.join(node.VMS, "b", "starting"))
        r = self.run_py("admit", "b", env={**env, "NODE_ADMIT": "1"})
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: node_full")
        self.assertIn("vcpus 4 allocated + 4 > 6", r.stderr)
        r = self.run_py("admit", "b", env={**env, "NODE_ADMIT": "1", "NODE_VCPUS": "8", "NODE_VMS": "1"})
        self.assertIn("vms 1 allocated + 1 > 1", r.stderr)
        self.assertEqual(self.run_py("admit", "b", env={**env, "NODE_ADMIT": "1", "NODE_VCPUS": "8"}).returncode, 0)


class Status(NodeCase):
    def test_status_view(self):
        self.run_py("tenant", "set", "acme", "--vcpus", "8")
        self.make("a", 2, 1024, "acme", "running")
        self.make("b", 2, 1024, None, "starting")
        self.make("c", 2, 1024)
        os.environ.update({"NODE_VCPUS": "10", "NODE_MEM_MIB": "5000", "NODE_VMS": "4", "FCVM_VERSION": "9.9.9", "FIRECRACKER_VERSION": "v1.2.3"})
        s = node.status()
        self.assertEqual((s["version"], s["contract"], s["firecracker"], s["admission"], s["cordoned"]), ("9.9.9", 1, "v1.2.3", False, None))
        self.assertEqual(s["capacity"], {"vcpus": 10, "mem_mib": 5000, "vms": 4})
        self.assertEqual(s["allocated"], {"vcpus": 4, "mem_mib": 2048, "vms": 2})
        self.assertEqual(s["allocatable"], {"vcpus": 6, "mem_mib": 2952, "vms": 2})
        self.assertEqual(s["vms"], {"running": 1, "starting": 1, "stopped": 1})
        self.assertEqual([t["name"] for t in s["tenants"]], ["acme"])
        self.assertEqual(s["tenants"][0]["allocated"]["vcpus"], 2)
        self.assertEqual(set(s["slots"]), {"taps_full", "taps_restricted", "jail"})
        self.assertEqual(s["host"]["cpus"], os.cpu_count())
        os.makedirs(os.path.join(node.VMS, ".node"))
        with open(os.path.join(node.VMS, ".node", "cordoned"), "w") as f:
            f.write("drain\n")
        self.assertEqual(node.status()["cordoned"], "drain")
        out = self.run_py("status")
        self.assertEqual(json.loads(out.stdout)["allocated"]["vms"], 2)


if __name__ == "__main__":
    unittest.main()
