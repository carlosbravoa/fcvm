"""fcvm node join (lib/node_agent.py): the commands, the session and the work
connections, against a fake control plane in this process. No KVM, no root."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import time
import unittest

from helpers import ROOT  # noqa: F401
import node_agent as na


class FakeControlPlane:
    """What a control plane serves (docs/node-contract.md, Joining a control
    plane): join, the session, streams, leave. Plain HTTP on 127.0.0.1."""

    def __init__(self, token="good", credential="c-secret"):
        self.token, self.credential = token, credential
        self.joins, self.leaves, self.hellos, self.messages = [], [], asyncio.Queue(), asyncio.Queue()
        self.session = None
        self.streams = {}
        self.welcome = {"type": "welcome", "cluster": "unit", "name": "fake", "ping_every": 15, "orphan_after": None}

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def stop(self):
        self.server.close()
        if hasattr(self.server, "close_clients"):   # 3.13+: wait_closed would wait for the handlers otherwise
            self.server.close_clients()
        else:
            for ws in [self.session, *(f.result() for f in self.streams.values() if f.done())]:
                if ws:
                    await ws.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 5)
        except asyncio.TimeoutError:
            pass

    async def handle(self, r, w):
        try:
            head = await r.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            w.close()
            return
        lines = head.decode().split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        body = await r.readexactly(int(headers.get("content-length") or 0))
        bearer = headers.get("authorization", "").removeprefix("Bearer ")

        def reply(status, obj):
            data = json.dumps(obj).encode()
            w.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                    f"Connection: close\r\n\r\n".encode() + data)

        if method == "POST" and path == "/node/v1/join":
            d = json.loads(body)
            self.joins.append(d)
            if d.get("token") == self.token:
                reply(200, {"node_id": "n-test", "credential": self.credential, "cluster": "unit", "name": "fake"})
            else:
                reply(403, {"error": "bad token", "code": "token_invalid"})
            await w.drain()
            w.close()
            return
        if method == "POST" and path == "/node/v1/leave":
            self.leaves.append((bearer, json.loads(body)))
            reply(200, {})
            await w.drain()
            w.close()
            return
        if bearer != self.credential:
            reply(401, {"error": "unknown credential", "code": "unauthorized"})
            await w.drain()
            w.close()
            return
        if headers.get("upgrade", "").lower() != "websocket":
            reply(404, {"error": "not found"})
            await w.drain()
            w.close()
            return
        import base64
        import hashlib
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + na.WS_GUID).encode()).digest()).decode()
        w.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        await w.drain()
        ws = na.WS(r, w, mask=False)
        if path == "/node/v1/session":
            self.session = ws
            while True:
                op, data = await ws.recv()
                if op is None:
                    break
                m = json.loads(data)
                if m.get("type") == "hello":
                    await ws.send_json(self.welcome)
                    await self.hellos.put(m)
                else:
                    await self.messages.put(m)
            if self.session is ws:
                self.session = None
        elif path.startswith("/node/v1/stream/"):
            fut = self.streams.get(path.rsplit("/", 1)[1])
            if fut and not fut.done():
                fut.set_result(ws)
                while not ws.closed:                   # the test drives this socket; keep the handler alive
                    await asyncio.sleep(0.05)
            else:
                await ws.close()

    async def open_stream(self, timeout=5):
        sid = os.urandom(4).hex()
        fut = asyncio.get_running_loop().create_future()
        self.streams[sid] = fut
        await self.session.send_json({"type": "open", "stream": sid})
        return await asyncio.wait_for(fut, timeout)


async def echo_server():
    async def handle(r, w):
        try:
            while chunk := await r.read(65536):
                w.write(chunk)
                await w.drain()
        except (ConnectionError, OSError):
            pass
        w.close()
    s = await asyncio.start_server(handle, "127.0.0.1", 0)
    return s, s.sockets[0].getsockname()[1]


async def wait_for(fn, timeout=10, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class NodeJoinCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fcvm-unit-")
        os.environ["FCVM_HOME"] = self.home
        na.STATE = self.home
        na.NODE_DIR = os.path.join(self.home, "vms", ".node")
        na.JOIN = os.path.join(na.NODE_DIR, "join.json")
        na.STATE_FILE = os.path.join(na.NODE_DIR, "state.json")

    def tearDown(self):
        os.environ.pop("FCVM_HOME", None)

    def joined(self, cp, **extra):
        na.write_json(na.JOIN, {"url": cp.url, "node_id": "n-test", "credential": cp.credential, "cluster": "unit",
                                "name": "fake", "ca": None, "labels": {}, "joined": time.time(), **extra})

    @staticmethod
    def run_cmd(fn, args):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                fn(args)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class WebSocketFraming(unittest.IsolatedAsyncioTestCase):
    async def test_client_and_server_frames(self):
        got = asyncio.Queue()

        async def handle(r, w):
            ws = na.WS(r, w, mask=False)
            while True:
                op, data = await ws.recv()
                if op is None:
                    break
                await got.put((op, data))
                await ws.send(data, op)        # echo, unmasked (a server)
        s = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = s.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        c = na.WS(r, w, mask=True)
        for payload in (b"x", b"y" * 200, os.urandom(70000)):   # 7-bit, 16-bit and 64-bit lengths
            await c.send(payload, 0x2)
            op, data = await got.get()
            self.assertEqual((op, data), (0x2, payload))
            self.assertEqual(await c.recv(), (0x2, payload))
        await c.send("text")
        self.assertEqual(await got.get(), (0x1, b"text"))
        self.assertEqual(await c.recv(), (0x1, b"text"))
        await c.ping()                           # answered by the server's recv, invisible to us
        await c.send(b"after", 0x2)
        self.assertEqual(await c.recv(), (0x2, b"after"))
        await c.close()
        s.close()
        await s.wait_closed()


class JoinCommands(NodeJoinCase):
    async def test_join_status_leave(self):
        cp = FakeControlPlane()
        await cp.start()
        try:
            code, out, err = await asyncio.to_thread(self.run_cmd, na.cmd_join, [cp.url, "--token", "good", "--name", "box",
                                                                                 "--label", "rack=3"])
            self.assertEqual(code, 0, err)
            self.assertIn("joined fake (cluster unit) as n-test", out)
            self.assertEqual(cp.joins[-1]["name"], "box")
            self.assertEqual(cp.joins[-1]["labels"], {"rack": "3"})
            self.assertEqual(cp.joins[-1]["protocol"], na.PROTOCOL)
            j = na.read_json(na.JOIN)
            self.assertEqual((j["url"], j["node_id"], j["credential"]), (cp.url, "n-test", "c-secret"))
            self.assertEqual(oct(os.stat(na.JOIN).st_mode & 0o777), "0o600")

            code, out, _ = self.run_cmd(na.cmd_status, [])
            self.assertIn("cluster unit  node n-test", out)
            code, out, _ = self.run_cmd(na.cmd_status, ["--json"])
            self.assertEqual(json.loads(out)["connected"], False)

            # joined again, same URL: fine (cloud-init re-ran); another URL: node_joined
            code, _, err = await asyncio.to_thread(self.run_cmd, na.cmd_join, [cp.url, "--token", "good"])
            self.assertEqual(code, 0, err)
            code, _, err = await asyncio.to_thread(self.run_cmd, na.cmd_join, ["http://127.0.0.1:1/x", "--token", "good"])
            self.assertEqual((code, err.strip().splitlines()[-1]), (1, "fcvm-error: node_joined"))

            code, out, err = await asyncio.to_thread(self.run_cmd, na.cmd_leave, [])
            self.assertEqual(code, 0, err)
            self.assertEqual(cp.leaves[-1], ("c-secret", {"node_id": "n-test"}))
            self.assertFalse(os.path.exists(na.JOIN))
            code, _, err = self.run_cmd(na.cmd_leave, [])
            self.assertEqual((code, err.strip().splitlines()[-1]), (1, "fcvm-error: node_not_joined"))
        finally:
            await cp.stop()

    async def test_join_refused_and_invalid(self):
        cp = FakeControlPlane()
        await cp.start()
        try:
            code, _, err = await asyncio.to_thread(self.run_cmd, na.cmd_join, [cp.url, "--token", "bad"])
            self.assertEqual(code, 1)
            self.assertIn("bad token (token_invalid)", err)
            self.assertEqual(err.strip().splitlines()[-1], "fcvm-error: join_refused")
            self.assertFalse(os.path.exists(na.JOIN))
        finally:
            await cp.stop()
        code, _, err = self.run_cmd(na.cmd_join, ["http://cp.example", "--token", "x"])      # plain http, remote
        self.assertEqual((code, err.strip().splitlines()[-1]), (1, "fcvm-error: invalid"))
        code, _, err = self.run_cmd(na.cmd_join, ["--token", "x"])
        self.assertEqual((code, err.strip().splitlines()[-1]), (1, "fcvm-error: invalid"))
        code, _, err = await asyncio.to_thread(self.run_cmd, na.cmd_join, ["http://127.0.0.1:1", "--token", "x"])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)


class Session(NodeJoinCase):
    async def test_session_streams_and_leave(self):
        cp = FakeControlPlane()
        await cp.start()
        echo, port = await echo_server()
        self.joined(cp)
        agent = na.Agent(port, lambda: "tok-1", orphan_cmd="")
        task = asyncio.ensure_future(agent.run())
        try:
            hello = await asyncio.wait_for(cp.hellos.get(), 10)
            self.assertEqual((hello["node_id"], hello["api_port"], hello["api_token"], hello["protocol"]),
                             ("n-test", port, "tok-1", na.PROTOCOL))
            await wait_for(lambda: (na.read_json(na.STATE_FILE) or {}).get("connected"), what="connected")
            st = na.read_json(na.STATE_FILE)
            self.assertEqual((st["cluster"], st["node_id"]), ("unit", "n-test"))

            # a work connection reaches the "API" (an echo server here), both ways, twice over
            ws = await cp.open_stream()
            await ws.send(b"GET /api/host\r\n", 0x2)
            self.assertEqual(await asyncio.wait_for(ws.recv(), 5), (0x2, b"GET /api/host\r\n"))
            big = os.urandom(200000)
            await ws.send(big, 0x2)
            data = b""
            while len(data) < len(big):
                op, chunk = await asyncio.wait_for(ws.recv(), 5)
                self.assertEqual(op, 0x2)
                data += chunk
            self.assertEqual(data, big)
            await wait_for(lambda: (na.read_json(na.STATE_FILE) or {}).get("streams_open") == 1, what="one stream")
            await ws.close()
            await wait_for(lambda: (na.read_json(na.STATE_FILE) or {}).get("streams_open") == 0, what="stream closed")
            self.assertEqual(na.read_json(na.STATE_FILE)["streams_total"], 1)

            # status? is answered with the host's view and the current token
            await cp.session.send_json({"type": "status?"})
            m = await asyncio.wait_for(cp.messages.get(), 10)
            self.assertEqual((m["type"], m["api_token"]), ("status", "tok-1"))

            # the control plane drops the session: the node is back within a few seconds
            await cp.session.close()
            hello = await asyncio.wait_for(cp.hellos.get(), 15)
            self.assertEqual(hello["node_id"], "n-test")
            await wait_for(lambda: (na.read_json(na.STATE_FILE) or {}).get("connected"), what="reconnected")

            # removed by the control plane: join.json goes, and nothing reconnects
            await cp.session.send_json({"type": "leave", "reason": "removed"})
            await wait_for(lambda: not os.path.exists(na.JOIN), what="join.json removed")
            st = na.read_json(na.STATE_FILE)
            self.assertEqual((st["connected"], st["removed_reason"]), (False, "removed"))
            code, out, _ = self.run_cmd(na.cmd_status, [])
            self.assertIn("removed by the control plane", out)
            await asyncio.sleep(1.5)
            self.assertTrue(cp.hellos.empty())
        finally:
            task.cancel()
            echo.close()
            await cp.stop()

    async def test_leave_command_closes_the_session(self):
        cp = FakeControlPlane()
        await cp.start()
        echo, port = await echo_server()
        self.joined(cp)
        agent = na.Agent(port, lambda: "tok", orphan_cmd="")
        task = asyncio.ensure_future(agent.run())
        try:
            await asyncio.wait_for(cp.hellos.get(), 10)
            await wait_for(lambda: (na.read_json(na.STATE_FILE) or {}).get("connected"), what="connected")
            os.remove(na.JOIN)                                # what `fcvm node leave` does
            # keepalive notices within ping_every; make that quick for the test
            agent_ws = agent.ws
            self.assertIsNotNone(agent_ws)
            await agent_ws.send_json({"type": "bye"})
            m = await asyncio.wait_for(cp.messages.get(), 10)
            self.assertEqual(m["type"], "bye")
            await wait_for(lambda: cp.session is None or cp.session.closed, 30, what="session closed")
        finally:
            task.cancel()
            echo.close()
            await cp.stop()

    async def test_refused_credential_backs_off(self):
        cp = FakeControlPlane(credential="other")
        await cp.start()
        self.joined(cp, credential="c-secret")             # not the fake's: it says 401
        agent = na.Agent(1, lambda: "tok", orphan_cmd="")
        task = asyncio.ensure_future(agent.run())
        try:
            await wait_for(lambda: "401" in str((na.read_json(na.STATE_FILE) or {}).get("last_error")), what="a 401")
            code, out, _ = self.run_cmd(na.cmd_status, ["--json"])
            self.assertIn("401", json.loads(out)["last_error"])
        finally:
            task.cancel()
            await cp.stop()

    async def test_orphan_command(self):
        marker = os.path.join(self.home, "orphaned")
        agent = na.Agent(1, lambda: "tok", orphan_cmd=f"touch {marker}")
        agent.orphan_after, agent.down_since = 1, time.time() - 5
        await agent.orphan_check()                         # no tenant VMs in an empty state directory
        self.assertTrue(os.path.exists(marker))
        os.remove(marker)
        await agent.orphan_check()                         # once only
        self.assertFalse(os.path.exists(marker))
        quiet = na.Agent(1, lambda: "tok", orphan_cmd="")   # no command: nothing, ever
        quiet.orphan_after, quiet.down_since = 1, 0
        await quiet.orphan_check()
        self.assertFalse(quiet.orphaned)


if __name__ == "__main__":
    unittest.main()
