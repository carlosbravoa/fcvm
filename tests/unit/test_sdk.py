"""The Python client (sdk/python/fcvm.py) against a fake server: discovery,
request shapes, error mapping."""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

from helpers import ROOT
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
import fcvm as sdk  # noqa: E402


class Fake(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Fake.calls.append(("GET", self.path, None))
        if self.headers.get("Authorization") != "Bearer tok":
            return self.reply(401, {"error": "wrong token"})
        if self.path == "/api/templates":
            return self.reply(200, [{"name": "py", "builtin": True, "description": "", "image": "python-3.13-slim",
                                     "process": "idle", "command": [], "secrets": ["MY_SECRET"], "network": "restricted",
                                     "allow": ["@pypi"], "vcpus": 2, "mem_mib": 2048}])
        if self.path.startswith("/api/vms/box/file?"):
            return self.reply(200, b"file-bytes")
        if self.path == "/api/vms/box/jobs/abc?tail=200":
            return self.reply(200, {"id": "abc", "status": "exited", "exit_code": 0, "stdout": "done"})
        if self.path == "/api/vms/nope":
            return self.reply(404, {"error": "no VM 'nope'", "code": "vm_not_found"})
        return self.reply(200, {"name": "box", "state": "running"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Fake.calls.append(("POST", self.path, body))
        if self.path == "/api/vms/box/exec":
            return self.reply(200, {"job": "abc"} if body.get("background") else {"exit_code": 3, "stdout": "o", "stderr": "e"})
        if self.path == "/api/vms/box/ports":
            return self.reply(200, {"ports": ["8080:80", "43127:8000"]})
        return self.reply(200, {"name": body.get("name", "box"), "state": "running"})

    def do_DELETE(self):
        Fake.calls.append(("DELETE", self.path, None))
        self.reply(200, {"removed": "x"})

    def do_PUT(self):
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Fake.calls.append(("PUT", self.path, data))
        self.reply(200, {"written": "x", "bytes": len(data)})


class SDK(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.calls.clear()
        self.c = sdk.Client(self.url, "tok")

    def test_discovery(self):
        d = tempfile.mkdtemp(prefix="fcvm-sdk-")
        os.makedirs(os.path.join(d, "vms"))
        with open(os.path.join(d, "vms", ".serve.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:8686/?token=abc", "token": "abc", "port": 8686, "pid": 1}, f)
        saved = dict(os.environ)
        try:
            os.environ.pop("FCVM_URL", None)
            os.environ["FCVM_HOME"] = d
            self.assertEqual(sdk.discover(), ("http://127.0.0.1:8686", "abc"))
            del os.environ["FCVM_HOME"]                       # a checkout: only `fcvm _home` knows
            os.environ["XDG_DATA_HOME"] = os.path.join(d, "nowhere")
            with open(os.path.join(d, "fcvm"), "w") as f:
                f.write(f'#!/bin/sh\n[ "$1" = _home ] && echo {d}\n')
            os.chmod(os.path.join(d, "fcvm"), 0o755)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            self.assertEqual(sdk.discover(), ("http://127.0.0.1:8686", "abc"))
            os.environ["FCVM_URL"], os.environ["FCVM_TOKEN"] = "http://h:1/", "t"
            self.assertEqual(sdk.discover(), ("http://h:1", "t"))
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_create_from_template_merges_fields_and_secrets(self):
        os.environ["MY_SECRET"] = "s3"
        try:
            sb = self.c.create("box", template="py", mem_mib=512, ttl="1h", command=["python", "a b.py"])
        finally:
            del os.environ["MY_SECRET"]
        self.assertEqual(sb.name, "box")
        body = next(b for m, p, b in Fake.calls if (m, p) == ("POST", "/api/vms"))
        self.assertEqual((body["image"], body["mem_mib"], body["vcpus"], body["ttl"]), ("python-3.13-slim", 512, 2, "1h"))
        self.assertEqual((body["network"], body["allow"], body["secrets"]), ("restricted", ["@pypi"], {"MY_SECRET": "s3"}))
        self.assertEqual(body["command"], "python 'a b.py'")
        self.assertTrue(body["idle"])
        self.assertNotIn("process", body)
        with self.assertRaises(sdk.FcvmError):
            self.c.create("box", template="nope")

    def test_exec_files_ports_jobs(self):
        sb = sdk.Sandbox(self.c, "box")
        r = sb.exec("echo hi", env={"X": "1"}, timeout=10)
        self.assertEqual((r.exit_code, r.stdout, r.stderr, r.ok), (3, "o", "e", False))
        with self.assertRaises(sdk.FcvmError):
            r.check()
        body = Fake.calls[-1][2]
        self.assertEqual((body["command"], body["env"], body["timeout"]), ("echo hi", {"X": "1"}, 10))
        self.assertNotIn("background", body)
        job = sb.exec("sleep 9", background=True)
        self.assertEqual(job.id, "abc")
        self.assertEqual(job.wait(timeout=5)["status"], "exited")
        self.assertEqual(sb.read_file("/etc/x"), b"file-bytes")
        sb.write_file("/tmp/y", "data", mode="600")
        self.assertEqual(Fake.calls[-1][:2], ("PUT", "/api/vms/box/file?path=%2Ftmp%2Fy&mode=600"))
        self.assertEqual(sb.publish(8000), 43127)
        sb.unpublish(43127)
        self.assertEqual(Fake.calls[-1][:2], ("DELETE", "/api/vms/box/ports?port=43127"))

    def test_errors_and_context_manager(self):
        with self.assertRaises(sdk.FcvmError) as cm:
            self.c.sandbox("nope")
        self.assertEqual((cm.exception.status, cm.exception.code, str(cm.exception)), (404, "vm_not_found", "no VM 'nope'"))
        with self.assertRaises(sdk.FcvmError) as cm:
            sdk.Client(self.url, "wrong").vms()
        self.assertEqual(cm.exception.status, 401)
        with sdk.Sandbox(self.c, "box"):
            pass
        self.assertEqual(Fake.calls[-1][:2], ("DELETE", "/api/vms/box"))


if __name__ == "__main__":
    unittest.main()
