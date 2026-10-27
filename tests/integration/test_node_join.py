"""fcvm node join with a real `fcvm serve`: after joining, the service connects
to the control plane, and a work connection reaches the HTTP API. Needs no VM,
so it runs in a throwaway state directory of its own, with its own serve."""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from fcvmtest import FCVM, ROOT, free_port, wait_for

if os.path.join(ROOT, "lib") not in sys.path:
    sys.path.append(os.path.join(ROOT, "lib"))
# the fake control plane of the unit tests, loaded by path (tests/unit must stay off sys.path:
# both directories have a test_node_mode)
_spec = importlib.util.spec_from_file_location("unit_test_node_join", os.path.join(ROOT, "tests", "unit", "test_node_join.py"))
_mod = importlib.util.module_from_spec(_spec)
sys.path.append(os.path.join(ROOT, "tests", "unit"))   # for its `from helpers import ROOT`
_spec.loader.exec_module(_mod)
sys.path.remove(os.path.join(ROOT, "tests", "unit"))
FakeControlPlane = _mod.FakeControlPlane


class NodeJoin(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fcvmtest-node-")
        self.env = {**os.environ, "FCVM_HOME": self.home, "FCVM_CONF": os.path.join(self.home, "none.conf")}
        self.port = free_port()
        self.serve = subprocess.Popen([FCVM, "serve", "--port", str(self.port)], env=self.env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: os.path.exists(os.path.join(self.home, "vms", ".serve.json")), 20, what="fcvm serve")
        with open(os.path.join(self.home, "vms", ".serve.json")) as f:
            self.token = json.load(f)["token"]
        # the fake control plane, on its own loop in a thread
        self.loop = asyncio.new_event_loop()
        self.cp = FakeControlPlane()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self.cp.start(), self.loop).result(10)

    def tearDown(self):
        self.serve.terminate()
        try:
            self.serve.wait(10)
        except subprocess.TimeoutExpired:
            self.serve.kill()
        asyncio.run_coroutine_threadsafe(self.cp.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        shutil.rmtree(self.home, ignore_errors=True)

    def fcvm(self, *args):
        return subprocess.run([FCVM, *args], env=self.env, capture_output=True, text=True, timeout=60)

    def on_cp(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def test_join_session_request_leave(self):
        r = self.fcvm("node", "status")
        self.assertIn("not joined", r.stdout)
        r = self.fcvm("node", "join", self.cp.url, "--token", "good", "--name", "it")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("connects within a few seconds", r.stdout)
        self.assertEqual(self.cp.joins[-1]["name"], "it")
        self.assertIn("contract", self.cp.joins[-1]["status"])       # fcvm status --json travelled with the join

        hello = self.on_cp(asyncio.wait_for(self.cp.hellos.get(), 15))
        self.assertEqual((hello["node_id"], hello["api_port"], hello["api_token"]), ("n-test", self.port, self.token))
        self.assertEqual(hello["status"]["contract"], 1)
        wait_for(lambda: json.loads(self.fcvm("node", "status", "--json").stdout)["connected"], 10, what="connected")
        r = self.fcvm("node", "status")
        self.assertIn("connected since", r.stdout)
        st = json.loads(self.fcvm("status", "--json").stdout)
        self.assertEqual(st["control_plane"], {"url": self.cp.url, "cluster": "unit", "node_id": "n-test", "connected": True})

        # an HTTP request through a work connection, with the Host and token the hello gave us
        async def request():
            ws = await self.cp.open_stream()
            await ws.send((f"GET /api/host HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                           f"Authorization: Bearer {self.token}\r\nConnection: close\r\n\r\n").encode(), 0x2)
            data = b""
            while True:
                op, chunk = await asyncio.wait_for(ws.recv(), 20)
                if op is None:
                    break
                data += chunk
            return data
        raw = self.on_cp(request(), 30)
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        self.assertIn("node", json.loads(body))

        r = self.fcvm("node", "leave")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cp.leaves[-1][1], {"node_id": "n-test"})
        wait_for(lambda: self.cp.session is None or self.cp.session.closed, 30, what="the session to close")
        self.assertIn("not joined", self.fcvm("node", "status").stdout)


if __name__ == "__main__":
    unittest.main()
