"""Operations: slow commands detached with --op, followed with fcvm ops."""
import json
import os
import tempfile

from fcvmtest import VMTestCase, Server, fcvm, IMAGE, wait_for


class Ops(VMTestCase):
    def test_export_as_an_operation(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-ops-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        tar = os.path.join(d, "img.tar")
        op = fcvm("export", IMAGE, tar, "--op").stdout.strip()
        self.assertRegex(op, r"^op-[a-z0-9]{10}$")
        r = fcvm("ops", op, "-f", timeout=300)
        self.assertIn("exported", r.stdout + r.stderr)
        j = json.loads(fcvm("ops", op, "--json").stdout)
        self.assertEqual((j["status"], j["exit"], j["kind"]), ("done", 0, "export"))
        self.assertTrue(os.path.exists(tar))
        self.assertIn(op, fcvm("ops").stdout)
        r = fcvm("export", IMAGE, tar, "--op")                      # the file exists: fails inside the operation
        failed = wait_for(lambda: (lambda s: s if s["status"] != "running" else None)(
            json.loads(fcvm("ops", r.stdout.strip(), "--json").stdout)), 30, what="the operation")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("exists", failed["error"])
        self.assertEqual(fcvm("ops", r.stdout.strip(), "-f", check=False).returncode, 1)
        self.assertEqual(fcvm("ops", "op-nonexistent", check=False).stderr.splitlines()[-1], "fcvm-error: not_found")

    def test_jobs_api_reads_operations(self):
        server = Server()
        self.addCleanup(server.close)
        code, body = server.api("GET", "jobs/op-zzzzzzzzzz")
        if code == 400:
            self.skipTest("the running fcvm service predates operations: fcvm service install")
        self.assertEqual(code, 404)
        d = tempfile.mkdtemp(prefix="fcvmtest-ops-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        op = fcvm("export", IMAGE, os.path.join(d, "a.tar"), "--op").stdout.strip()
        code, body = server.api("GET", f"jobs/{op}?wait=120")
        self.assertEqual((code, body["status"], body["id"]), (200, "done", op), body)
        self.assertIn(op, [j["id"] for j in server.api("GET", "jobs")[1]])
