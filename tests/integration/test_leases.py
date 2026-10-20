"""Leases (--lease) and cordon: the CLI side. The supervisor's removal of a
VM whose lease ended is unit-tested (its decision) and needs the service to
run this code to be seen end to end."""
import json
import os
import time

from fcvmtest import VMTestCase, fcvm, IMAGE, unique, VMS


class Leases(VMTestCase):
    def test_lease_on_create_update_and_ls(self):
        vm = self.vm("--lease", "2h", start=False)
        until = self.inspect(vm)["lease_until"]
        self.assertAlmostEqual(until, time.time() + 7200, delta=30)
        self.assertRegex(fcvm("ls").stdout, rf"{vm}.*lease:1(19|20)m")
        fcvm("update", vm, "--lease", "2026-10-20T12:00:00Z")
        self.assertEqual(self.inspect(vm)["lease_until"], 1792497600)
        fcvm("update", vm, "--lease", str(int(time.time()) + 60))
        fcvm("update", vm, "--lease", "none")
        self.assertIsNone(self.inspect(vm)["lease_until"])
        r = fcvm("update", vm, "--lease", "soon", check=False)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: invalid")
        fcvm("start", vm, "--lease", "1h")
        self.assertAlmostEqual(self.inspect(vm)["lease_until"], time.time() + 3600, delta=30)

    def test_cordon_refuses_new_vms_only(self):
        if os.path.exists(os.path.join(VMS, ".node", "cordoned")):
            self.skipTest("the host is cordoned already")
        running = self.vm()
        fcvm("cordon", "test drain")
        self.addCleanup(fcvm, "uncordon", check=False)
        self.assertEqual(open(os.path.join(VMS, ".node", "cordoned")).read().strip(), "test drain")
        r = fcvm("create", unique("cord"), IMAGE, "--idle", check=False)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: cordoned")
        self.assertIn("test drain", r.stderr)
        snap = self.snapshot(running)
        r = fcvm("fork", snap, unique("cord"), check=False, env={"FCVM_DAEMON": "1"})
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: cordoned")
        fcvm("stop", running)
        fcvm("start", running)                                   # existing VMs: not new
        self.assertEqual(self.inspect(running)["state"], "running")
        fcvm("uncordon")
        self.assertFalse(os.path.exists(os.path.join(VMS, ".node", "cordoned")))
        fresh = self.vm(start=False)
        self.assertTrue(fresh)
        evs = [json.loads(l) for l in fcvm("events", "--since", "-30", "--json").stdout.splitlines() if l]
        self.assertIn("node.cordoned", [e["type"] for e in evs])
        self.assertIn("node.uncordoned", [e["type"] for e in evs])
        self.assertEqual(fcvm("uncordon").returncode, 0)        # idempotent
