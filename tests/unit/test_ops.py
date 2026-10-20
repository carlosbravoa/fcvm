"""Operations (lib/ops.py): detached commands with their state in vms/.ops/."""
import json
import os
import tempfile
import time
import unittest

from helpers import ROOT  # noqa: F401
import ops


def wait_done(op, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        m = ops.meta(op)
        if m and m["status"] != "running":
            return m
        time.sleep(0.1)
    raise AssertionError(f"{op} still running")


class Operations(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fcvm-unit-")
        os.environ["FCVM_HOME"] = self.home          # inherited by the detached fcvm
        ops.STATE, ops.OPS = self.home, os.path.join(self.home, "vms", ".ops")

    def tearDown(self):
        os.environ.pop("FCVM_HOME", None)

    def test_start_runs_detached_and_records_the_outcome(self):
        op = ops.start(["version"], "version", "fcvm version")
        self.assertRegex(op, r"^op-[a-z0-9]{10}$")
        m = wait_done(op)
        self.assertEqual((m["status"], m["exit"], m["error"], m["kind"], m["title"]), ("done", 0, None, "version", "fcvm version"))
        j = ops.get(op)
        self.assertTrue(any(l.startswith("fcvm ") for l in j["lines"]), j["lines"])
        self.assertEqual(j["next"], len(j["lines"]))
        self.assertEqual(ops.get(op, j["next"])["lines"], [])
        listed = ops.list_ops()
        self.assertEqual([l["id"] for l in listed], [op])
        self.assertEqual(listed[0]["log"], j["lines"][-3:])
        with open(os.path.join(self.home, "vms", ".events.jsonl")) as f:
            ev = json.loads(f.readlines()[-1])
        self.assertEqual((ev["type"], ev["vm"], ev["reason"], ev["data"]["op"]), ("operation.done", None, "done", op))

    def test_failure_keeps_the_error_and_code(self):
        op = ops.start(["inspect", "no-such-vm"], "inspect")
        m = wait_done(op)
        self.assertEqual((m["status"], m["error"], m["code"]), ("failed", "no VM 'no-such-vm'", "vm_not_found"))
        self.assertEqual(m["title"], "inspect no-such-vm")

    def test_interrupted_and_old_operations(self):
        os.makedirs(ops.OPS)
        gone = {"id": "op-aaaaaaaaaa", "kind": "import", "title": "x", "command": [], "status": "running",
                "started": time.time(), "ended": None, "exit": None, "error": None, "pid": 2 ** 22 - 1, "boot_id": "other"}
        ops._write(gone["id"], gone)
        old = {**gone, "id": "op-bbbbbbbbbb", "status": "done", "ended": time.time() - 8 * 86400}
        ops._write(old["id"], old)
        listed = ops.list_ops()
        self.assertEqual([l["id"] for l in listed], ["op-aaaaaaaaaa"])
        self.assertEqual((listed[0]["status"], listed[0]["error"]), ("failed", "interrupted"))
        self.assertIsNone(ops.meta("op-bbbbbbbbbb"))
        self.assertIsNone(ops.meta("../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
