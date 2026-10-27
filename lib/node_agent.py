"""fcvm node join: make this host a node of a control plane.

  fcvm node join URL --token TOKEN [--name NAME] [--label K=V]... [--ca FILE]
  fcvm node status [--json]
  fcvm node leave

`join` enrolls once (POST /node/v1/join) and writes vms/.node/join.json. From
then on the fcvm service (fcvm serve, which runs Agent.run as a task) keeps a
session with the control plane: one outbound WebSocket carrying JSON
messages, and, whenever the control plane says {"type": "open", "stream":
ID}, a work connection: a second WebSocket (/node/v1/stream/ID) piped byte
for byte to this host's own API on 127.0.0.1. The control plane sets Host,
Origin and Authorization itself (hello tells it the port and the token), so
nothing here parses HTTP. The protocol is part of the node contract
(docs/node-contract.md, "Joining a control plane").

Standard library only (os.urandom rather than the secrets module, which
lib/secrets.py shadows when lib/ is on sys.path). The WebSocket client below
masks its frames as clients must; WS(mask=False) is a server side, used by
the unit tests' fake control plane.
"""
import asyncio
import base64
import hashlib
import json
import os
import random
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = 1
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("FCVM_HOME") or ROOT
FCVM = os.path.join(ROOT, "fcvm")
NODE_DIR = os.path.join(STATE, "vms", ".node")
JOIN = os.path.join(NODE_DIR, "join.json")
STATE_FILE = os.path.join(NODE_DIR, "state.json")
MAX_STREAMS = 256
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg):
    print(f"node: {msg}", file=sys.stderr, flush=True)


def die(code, msg):
    """Fail the way lib/common.sh's die_code does (docs/errors.md)."""
    sys.stderr.write(f"\033[1;31merror:\033[0m {msg}\n")
    if not sys.stderr.isatty():
        sys.stderr.write(f"fcvm-error: {code}\n")
    sys.exit(1)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.rename(tmp, path)


def parse_url(url):
    """-> (scheme, host, port, base path) of a control plane URL; refuses
    plain http except to this machine (or with FCVM_NODE_INSECURE=1)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"{url}: want https://HOST[:PORT][/PATH]")
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1") \
            and os.environ.get("FCVM_NODE_INSECURE") != "1":
        raise ValueError(f"{url}: plain http to a remote control plane (FCVM_NODE_INSECURE=1 to allow it)")
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), u.path.rstrip("/")


def tls_context(scheme, ca):
    if scheme != "https":
        return None
    ctx = ssl.create_default_context(cafile=ca or None)
    return ctx


def http_json(method, url, body, ca=None, bearer=None, timeout=20):
    """One JSON request -> (status, dict). Errors from the server come back as
    (status, {"error", "code"}); connection failures raise OSError."""
    scheme = parse_url(url)[0]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls_context(scheme, ca)) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": f"HTTP {e.code}", "code": "error"}
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from e


def host_status(timeout=30):
    """fcvm status --json, or None when it can't be had (a host without a kernel yet, say)."""
    try:
        r = subprocess.run([FCVM, "status", "--json"], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def serve_running():
    s = read_json(os.path.join(STATE, "vms", ".serve.json"))
    return bool(s and os.path.exists(f"/proc/{s.get('pid')}"))


# --- the WebSocket client (and the server side, for tests) ------------------------------------

class WSError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status


class WS:
    """A WebSocket on asyncio streams. mask=True is a client (frames masked, as
    RFC 6455 requires); mask=False a server. recv answers pings by itself."""

    def __init__(self, reader, writer, mask=True):
        self.r, self.w, self.mask = reader, writer, mask
        self.closed = False

    async def recv(self):
        """-> (opcode, bytes) of the next complete message; (None, None) once closed."""
        data, first_op = b"", None
        while True:
            try:
                b0, b1 = await self.r.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                return None, None
            fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            try:
                if n == 126:
                    n = struct.unpack(">H", await self.r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self.r.readexactly(8))[0]
                mask = await self.r.readexactly(4) if masked else None
                payload = bytearray(await self.r.readexactly(n))
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                return None, None
            if mask:
                for i in range(n):
                    payload[i] ^= mask[i % 4]
            if op == 0x8:                       # close: echo it, done
                await self.send(bytes(payload[:2]), 0x8)
                self.closed = True
                return None, None
            if op == 0x9:
                await self.send(bytes(payload), 0xA)
                continue
            if op == 0xA:
                continue
            if op != 0:
                first_op = op
            data += payload
            if fin:
                return first_op, data

    async def send(self, data, op=0x2):
        if self.closed:
            return
        if isinstance(data, str):
            data, op = data.encode(), 0x1
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([(0x80 if self.mask else 0) | n]) if n < 126 else
                                     bytes([(0x80 if self.mask else 0) | 126]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([(0x80 if self.mask else 0) | 127]) + struct.pack(">Q", n))
        if self.mask:
            key = os.urandom(4)
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data)) if n else b""
            head += key
        try:
            self.w.write(head + data)
            await self.w.drain()
        except (ConnectionError, OSError):
            self.closed = True

    async def send_json(self, obj):
        await self.send(json.dumps(obj), 0x1)

    async def ping(self):
        await self.send(b"", 0x9)

    async def close(self):
        if not self.closed:
            await self.send(b"", 0x8)
            self.closed = True
        try:
            self.w.close()
        except OSError:
            pass


async def ws_connect(url, path, bearer, ca=None, timeout=15):
    """Open a WebSocket to URL + PATH as a client -> WS. A non-101 answer raises
    WSError(status, the server's error message)."""
    scheme, host, port, base = parse_url(url)
    ctx = tls_context(scheme, ca)
    r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if ctx else None),
                                  timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    hostname = host if port in (80, 443) else f"{host}:{port}"
    w.write((f"GET {base}{path} HTTP/1.1\r\nHost: {hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
             f"Authorization: Bearer {bearer}\r\nUser-Agent: fcvm-node/{PROTOCOL}\r\n\r\n").encode())
    await w.drain()
    try:
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout)
    except asyncio.LimitOverrunError:
        w.close()
        raise WSError(0, "bad response")
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 and lines[0].split()[1].isdigit() else 0
    headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
    if status != 101:
        body = b""
        n = int(headers.get("content-length") or 0)
        if n:
            try:
                body = await asyncio.wait_for(r.readexactly(min(n, 4096)), 5)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass
        w.close()
        try:
            msg = json.loads(body).get("error") or f"HTTP {status}"
        except (ValueError, AttributeError):
            msg = f"HTTP {status}"
        raise WSError(status, msg)
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    if headers.get("sec-websocket-accept") != accept:
        w.close()
        raise WSError(0, "bad Sec-WebSocket-Accept")
    return WS(r, w, mask=True)


async def pump(ws, reader, writer):
    """Relay a WebSocket (binary frames) and a stream both ways until either ends."""
    async def up():
        while True:
            op, data = await ws.recv()
            if op is None:
                break
            if data:
                writer.write(data)
                try:
                    await writer.drain()
                except (ConnectionError, OSError):
                    break

    async def down():
        try:
            while chunk := await reader.read(65536):
                await ws.send(chunk, 0x2)
                if ws.closed:
                    break
        except (ConnectionError, OSError):
            pass

    tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    try:
        writer.close()
    except OSError:
        pass
    await ws.close()


# --- the session (runs inside fcvm serve) ---------------------------------------------------

class Agent:
    """Keeps the session with the control plane while vms/.node/join.json exists."""

    def __init__(self, api_port, token_fn, orphan_cmd=None):
        self.api_port = api_port
        self.token_fn = token_fn                   # the serve's current bearer token
        self.orphan_cmd = os.environ.get("NODE_ORPHAN_CMD", "") if orphan_cmd is None else orphan_cmd
        self.streams = {}                          # id -> task
        self.served = 0
        self.state = {"connected": False}
        self.down_since = time.time()
        self.orphan_after = None
        self.orphaned = False
        self.ws = None

    def save(self, **changes):
        self.state.update(changes, streams_open=len(self.streams), streams_total=self.served, updated=time.time())
        try:
            write_json(STATE_FILE, self.state, 0o644)
        except OSError:
            pass

    async def run(self):
        delay, prev = 1.0, None
        while True:
            j = read_json(JOIN)
            if not j:
                if prev:
                    log("left: no control plane")
                    self.save(connected=False, control_plane=None, cluster=None, node_id=None)
                prev = None
                await asyncio.sleep(5)
                continue
            if j.get("node_id") != prev:
                log(f"joining {j.get('url')} as {j.get('node_id')}")
                prev = j.get("node_id")
                delay = 1.0
            started = time.time()
            try:
                await self.session(j)
                err = None
            except WSError as e:
                err = f"{e}"
                if e.status in (401, 403):
                    delay = 60.0
                elif e.status == 410:
                    self.removed("gone")
                    continue
            except (OSError, asyncio.TimeoutError, ValueError) as e:
                err = str(e) or type(e).__name__
            except asyncio.CancelledError:
                raise
            except Exception as e:                 # a bug must not end the service's session for good
                err = f"{type(e).__name__}: {e}"
            if self.state.get("connected"):
                self.down_since = time.time()
            if not read_json(JOIN):             # fcvm node leave, or the control plane removed us
                self.save(connected=False)
                continue
            self.save(connected=False, last_error=err)
            if err:
                log(f"session to {j.get('url')} failed: {err}; retrying in {delay:.0f} s")
            await self.orphan_check()
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = 1.0 if time.time() - started > 60 else min(delay * 2, 30.0) if delay < 60 else delay

    async def session(self, j):
        ws = await ws_connect(j["url"], "/node/v1/session", j["credential"], j.get("ca"))
        self.ws = ws
        try:
            status = await asyncio.get_running_loop().run_in_executor(None, host_status)
            await ws.send_json({"type": "hello", "node_id": j["node_id"], "protocol": PROTOCOL,
                                "fcvm": fcvm_version(), "contract": contract(), "api_port": self.api_port,
                                "api_token": self.token_fn(), "status": status})
            every = 15
            pinger = asyncio.ensure_future(self.keepalive(ws, j, lambda: every))
            try:
                while True:
                    try:
                        op, data = await asyncio.wait_for(ws.recv(), every * 3)
                    except asyncio.TimeoutError:
                        raise OSError(f"no message from the control plane in {every * 3} s")
                    if op is None:
                        if self.state.get("connected"):
                            raise OSError("the control plane closed the session")
                        raise OSError("session refused")
                    if op != 0x1:
                        continue
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    t = m.get("type")
                    if t == "welcome":
                        every = int(m.get("ping_every") or 15)
                        self.orphan_after = m.get("orphan_after")
                        self.orphaned = False
                        self.save(connected=True, since=time.time(), control_plane=j["url"],
                                  cluster=m.get("cluster"), name=m.get("name"), node_id=j["node_id"], last_error=None)
                        log(f"connected to {m.get('name') or j['url']} (cluster {m.get('cluster')})")
                    elif t == "open":
                        sid = str(m.get("stream") or "")
                        if sid and len(self.streams) < MAX_STREAMS:
                            self.streams[sid] = asyncio.ensure_future(self.stream(j, sid))
                    elif t == "status?":
                        status = await asyncio.get_running_loop().run_in_executor(None, host_status)
                        await ws.send_json({"type": "status", "status": status, "api_token": self.token_fn()})
                    elif t == "leave":
                        self.removed(m.get("reason") or "removed")
                        return
            finally:
                pinger.cancel()
        finally:
            self.ws = None
            await ws.close()

    async def keepalive(self, ws, j, every):
        """Ping on our side too, and notice `fcvm node leave` (join.json gone)."""
        while not ws.closed:
            await asyncio.sleep(every())
            if not os.path.exists(JOIN):
                await ws.send_json({"type": "bye"})
                await ws.close()
                return
            await ws.ping()

    async def stream(self, j, sid):
        """A work connection: the control plane's WebSocket piped to the local API."""
        try:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.api_port), 5)
            except (OSError, asyncio.TimeoutError) as e:
                log(f"stream {sid}: cannot reach the local API: {e}")
                return
            try:
                ws = await ws_connect(j["url"], f"/node/v1/stream/{sid}", j["credential"], j.get("ca"))
            except (OSError, WSError, asyncio.TimeoutError) as e:
                w.close()
                log(f"stream {sid}: {e}")
                return
            self.served += 1
            self.save()
            await pump(ws, r, w)
        finally:
            self.streams.pop(sid, None)
            self.save()

    def removed(self, reason):
        log(f"removed by the control plane ({reason})")
        try:
            os.remove(JOIN)
        except OSError:
            pass
        self.save(connected=False, control_plane=None, removed=time.time(), removed_reason=reason)

    async def orphan_check(self):
        """After orphan_after seconds without the control plane, and with no tenant
        VMs on this host, run NODE_ORPHAN_CMD (the node image's way out)."""
        if not self.orphan_after or not self.orphan_cmd or self.orphaned:
            return
        if time.time() - self.down_since < self.orphan_after:
            return
        if await self.tenant_vms():
            return
        self.orphaned = True
        log(f"orphaned for {self.orphan_after} s with no tenant VMs: running NODE_ORPHAN_CMD")
        self.save(orphan_cmd_at=time.time())
        p = await asyncio.create_subprocess_shell(self.orphan_cmd)
        await p.wait()

    async def tenant_vms(self):
        try:
            p = await asyncio.create_subprocess_exec(FCVM, "ls", "--json", stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 30)
            return any((v.get("labels") or {}).get("tenant") for v in json.loads(out or b"[]"))
        except (OSError, ValueError, asyncio.TimeoutError):
            return True   # unsure: don't destroy anything


def fcvm_version():
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return None


def contract():
    try:
        return int(os.environ.get("CONTRACT") or 1)
    except ValueError:
        return 1


# --- the commands -----------------------------------------------------------------------------

def cmd_join(args):
    url, token, name, ca, labels = None, None, None, None, {}
    it = iter(args)
    for a in it:
        if a == "--token":
            token = next(it, None)
        elif a == "--name":
            name = next(it, None)
        elif a == "--ca":
            ca = next(it, None)
        elif a == "--label":
            kv = next(it, "") or ""
            if "=" not in kv:
                die("invalid", "--label wants KEY=VALUE")
            k, v = kv.split("=", 1)
            labels[k] = v
        elif a.startswith("-"):
            die("invalid", f"unknown option {a}")
        elif url is None:
            url = a
        else:
            die("invalid", "usage: fcvm node join URL --token TOKEN [--name NAME] [--label K=V]... [--ca FILE]")
    if not url or not token:
        die("invalid", "usage: fcvm node join URL --token TOKEN [--name NAME] [--label K=V]... [--ca FILE]")
    try:
        parse_url(url)
    except ValueError as e:
        die("invalid", str(e))
    if ca:
        ca = os.path.abspath(ca)
        if not os.path.isfile(ca):
            die("invalid", f"--ca: no such file: {ca}")
    url = url.rstrip("/")
    cur = read_json(JOIN)
    if cur and cur.get("url") != url:
        die("node_joined", f"already joined to {cur.get('url')} as {cur.get('node_id')} (fcvm node leave first)")
    body = {"token": token, "hostname": os.uname().nodename, "name": name, "labels": labels,
            "fcvm": fcvm_version(), "contract": contract(), "protocol": PROTOCOL, "status": host_status()}
    try:
        status, reply = http_json("POST", f"{url}/node/v1/join", body, ca)
    except OSError as e:
        die("error", f"cannot reach {url}: {e}")
    if status != 200 or not reply.get("node_id") or not reply.get("credential"):
        die("join_refused", f"{url} refused the join: {reply.get('error') or f'HTTP {status}'}"
                            + (f" ({reply['code']})" if reply.get("code") else ""))
    write_json(JOIN, {"url": url, "node_id": reply["node_id"], "credential": reply["credential"],
                      "cluster": reply.get("cluster"), "name": reply.get("name"), "ca": ca,
                      "labels": labels, "joined": time.time()})
    print(f"joined {reply.get('name') or url} (cluster {reply.get('cluster')}) as {reply['node_id']}")
    if serve_running():
        print("the fcvm service connects within a few seconds (fcvm node status)")
    else:
        print("nothing is serving: start the fcvm service (fcvm service install) or fcvm serve to connect")


def cmd_status(args):
    as_json = "--json" in args
    j, s = read_json(JOIN), read_json(STATE_FILE) or {}
    if as_json:
        print(json.dumps({"joined": bool(j), "url": j.get("url") if j else None, "node_id": j.get("node_id") if j else None,
                          "cluster": j.get("cluster") if j else None, "name": j.get("name") if j else None,
                          "joined_at": j.get("joined") if j else None, "labels": j.get("labels") if j else None,
                          "connected": bool(j) and bool(s.get("connected")), "since": s.get("since") if j else None,
                          "streams_open": s.get("streams_open", 0) if j else 0, "streams_total": s.get("streams_total", 0),
                          "last_error": s.get("last_error") if j else None,
                          "removed": s.get("removed"), "removed_reason": s.get("removed_reason")}, indent=1))
        return
    when = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"   # noqa: E731
    if not j:
        if s.get("removed"):
            print(f"not joined (removed by the control plane at {when(s['removed'])}: {s.get('removed_reason')})")
        else:
            print("not joined (fcvm node join URL --token TOKEN)")
        return
    print(f"control plane  {j.get('url')}  cluster {j.get('cluster')}  node {j.get('node_id')}")
    if s.get("connected"):
        print(f"session        connected since {when(s.get('since'))}, {s.get('streams_open', 0)} streams open, "
              f"{s.get('streams_total', 0)} served")
    elif serve_running():
        print(f"session        not connected" + (f": {s['last_error']}" if s.get("last_error") else " (connecting)"))
    else:
        print("session        nothing is serving: start the fcvm service (fcvm service install) or fcvm serve")


def cmd_leave(args):
    j = read_json(JOIN)
    if not j:
        die("node_not_joined", "this host isn't joined to a control plane")
    try:
        http_json("POST", f"{j['url']}/node/v1/leave", {"node_id": j["node_id"]}, j.get("ca"), bearer=j["credential"],
                  timeout=5)
    except OSError as e:
        print(f"could not tell {j['url']} (left anyway): {e}", file=sys.stderr)
    try:
        os.remove(JOIN)
    except OSError:
        pass
    print(f"left {j.get('name') or j.get('url')}; the session closes within a few seconds")


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "join":
        cmd_join(argv[1:])
    elif cmd == "status":
        cmd_status(argv[1:])
    elif cmd == "leave":
        cmd_leave(argv[1:])
    else:
        die("invalid", "usage: fcvm node join URL --token TOKEN [--name NAME] [--label K=V]... [--ca FILE] | status [--json] | leave")


if __name__ == "__main__":
    main(sys.argv[1:])
