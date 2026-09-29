#!/usr/bin/env python3
"""fcvm web console (fcvm serve): a local, cloud-console-like UI for one host.

  fcvm serve [--port 8686]

Standard library only. HTTP/1.1 and WebSockets (RFC 6455) are implemented
here on asyncio streams. Every change goes through the fcvm CLI, like the MCP
server, so the UI behaves exactly like the command line. Reads that must be
fast (stats, terminals) use the VM directories and sockets directly.

Security (local only): binds 127.0.0.1. The startup URL carries a random
token, exchanged for an HttpOnly SameSite=Strict cookie. Requests must have a
localhost Host header (against DNS rebinding), and changes and WebSockets a
same-origin Origin (against cross-site requests).
"""
import argparse
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import struct
import sys
import time
import urllib.parse
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FCVM = os.path.join(ROOT, "fcvm")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VMS = os.path.join(ROOT, "vms")
IMAGES = os.path.join(ROOT, "images")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455
SAMPLE_EVERY = 2.0
HISTORY = 300           # samples kept per VM (10 minutes)
CLK_TCK = os.sysconf("SC_CLK_TCK")
R_PREFIX = "172.30.1"   # restricted network prefix (fcvm passes NET_R_PREFIX)


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


# --- fcvm CLI ------------------------------------------------------------------------

async def fcvm(*args, timeout=120, stdin=None):
    p = await asyncio.create_subprocess_exec(
        FCVM, *map(str, args), stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise HTTPError(504, f"fcvm {args[0]} timed out")
    return p.returncode, out.decode(errors="replace"), ANSI.sub("", err.decode(errors="replace"))


async def fcvm_ok(*args, **kw):
    code, out, err = await fcvm(*args, **kw)
    if code != 0:
        msg = "\n".join(l for l in err.splitlines() if l.startswith("error:")) or err.strip() or f"exit {code}"
        raise HTTPError(400, msg.replace("error: ", ""))
    return out, err


async def fcvm_json(*args):
    return json.loads((await fcvm_ok(*args))[0] or "null")


# --- background jobs (slow operations such as image imports) -------------------------

class Jobs:
    def __init__(self):
        self.jobs = {}
        self.seq = 0

    def start(self, kind, title, args):
        self.seq += 1
        job = {"id": self.seq, "kind": kind, "title": title, "status": "running", "log": [],
               "started": time.time(), "ended": None}
        self.jobs[self.seq] = job
        asyncio.get_running_loop().create_task(self._run(job, args))
        return job

    async def _run(self, job, args):
        p = await asyncio.create_subprocess_exec(FCVM, *map(str, args), stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async for line in p.stdout:
            job["log"].append(ANSI.sub("", line.decode(errors="replace")).rstrip())
            del job["log"][:-400]
        job["status"] = "done" if await p.wait() == 0 else "failed"
        job["ended"] = time.time()

    def list(self):
        return sorted(self.jobs.values(), key=lambda j: -j["id"])[:20]


# --- stats ----------------------------------------------------------------------------

def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def vm_tap(ip):
    """fcvm's tap naming: fctap<i> / fcrtap<i> for <prefix>.(10+i)."""
    if not ip:
        return None
    idx = int(ip.rsplit(".", 1)[1]) - 10
    tap = f"fcrtap{idx}" if ip.startswith(R_PREFIX + ".") else f"fctap{idx}"
    return tap if os.path.exists(f"/sys/class/net/{tap}") else None


class Stats:
    """Samples host and per-VM resource use every SAMPLE_EVERY seconds."""

    def __init__(self):
        self.vms = {}            # name -> deque of samples
        self.prev = {}           # name -> (t, cpu_ticks, rbytes, wbytes, rx, tx)
        self.host = deque(maxlen=HISTORY)
        self.host_prev = None

    def host_sample(self, now):
        cpu = [int(x) for x in read("/proc/stat").split("\n", 1)[0].split()[1:]]
        idle, total = cpu[3] + cpu[4], sum(cpu)
        pct = None
        if self.host_prev:
            di, dt = idle - self.host_prev[0], total - self.host_prev[1]
            pct = round(100 * (1 - di / dt), 1) if dt else 0.0
        self.host_prev = (idle, total)
        mem = dict(l.split(":", 1) for l in read("/proc/meminfo").splitlines() if ":" in l)
        kb = lambda k: int(mem.get(k, "0 kB").split()[0]) * 1024
        self.host.append({"t": now, "cpu_pct": pct, "mem_total": kb("MemTotal"), "mem_available": kb("MemAvailable")})

    def vm_sample(self, name, now):
        d = os.path.join(VMS, name)
        pid = read(os.path.join(d, "pid")).strip()
        if not pid or not os.path.exists(f"/proc/{pid}"):
            self.prev.pop(name, None)
            return
        st = read(f"/proc/{pid}/stat")
        fields = st[st.rfind(")") + 2:].split()
        ticks = int(fields[11]) + int(fields[12])
        status = read(f"/proc/{pid}/status")
        rss = int(status.split("VmRSS:")[1].split()[0]) * 1024 if "VmRSS:" in status else 0
        io = dict(l.split(": ") for l in read(f"/proc/{pid}/io").splitlines() if ": " in l)
        rb, wb = int(io.get("read_bytes", 0)), int(io.get("write_bytes", 0))
        tap = vm_tap(read(os.path.join(d, "ip")).strip())
        # tap counters are from the host's side: the VM's outgoing traffic is the tap's rx
        tx = int(read(f"/sys/class/net/{tap}/statistics/rx_bytes", "0") or 0) if tap else 0
        rx = int(read(f"/sys/class/net/{tap}/statistics/tx_bytes", "0") or 0) if tap else 0
        sample = {"t": now, "rss": rss, "cpu_pct": None, "disk_read_bps": None, "disk_write_bps": None,
                  "net_rx_bps": None, "net_tx_bps": None}
        p = self.prev.get(name)
        if p and p[0] == pid and now > p[1]:
            dt = now - p[1]
            sample["cpu_pct"] = round(100 * (ticks - p[2]) / CLK_TCK / dt, 1)
            sample["disk_read_bps"] = max(0, int((rb - p[3]) / dt))
            sample["disk_write_bps"] = max(0, int((wb - p[4]) / dt))
            sample["net_rx_bps"] = max(0, int((rx - p[5]) / dt))
            sample["net_tx_bps"] = max(0, int((tx - p[6]) / dt))
        self.prev[name] = (pid, now, ticks, rb, wb, rx, tx)
        self.vms.setdefault(name, deque(maxlen=HISTORY)).append(sample)

    async def run(self):
        while True:
            now = time.time()
            try:
                self.host_sample(now)
                names = [n for n in os.listdir(VMS) if os.path.isfile(os.path.join(VMS, n, "vm.json"))]
                for n in names:
                    self.vm_sample(n, now)
                for gone in set(self.vms) - set(names):
                    self.vms.pop(gone, None)
            except Exception as e:      # stats must never take the server down
                print(f"stats: {e}", file=sys.stderr)
            await asyncio.sleep(SAMPLE_EVERY)


# --- WebSockets -----------------------------------------------------------------------

class WebSocket:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.closed = False

    async def recv(self):
        """-> (opcode, bytes) of the next complete message, or (None, None) when closed."""
        data, first_op = b"", None
        while True:
            try:
                b0, b1 = await self.r.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None, None
            fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self.r.readexactly(8))[0]
            mask = await self.r.readexactly(4) if masked else b"\0\0\0\0"
            payload = bytearray(await self.r.readexactly(n))
            for i in range(n):
                payload[i] ^= mask[i % 4]
            if op == 0x8:                       # echo the close once, then we're done
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
        head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else
                                     b"\x7e" + struct.pack(">H", n) if n < 65536 else
                                     b"\x7f" + struct.pack(">Q", n))
        try:
            self.w.write(head + data)
            await self.w.drain()
        except ConnectionError:
            self.closed = True

    async def close(self):
        if not self.closed:
            await self.send(b"", 0x8)
            self.closed = True
        self.w.close()


async def pump_ws(ws, sock_r, sock_w, on_text=None):
    """Relay a WebSocket and a stream both ways until either side ends."""
    async def up():
        while True:
            op, data = await ws.recv()
            if op is None:
                break
            if op == 0x1 and on_text:
                await on_text(data.decode(errors="replace"))
            elif data:
                sock_w.write(data)
                await sock_w.drain()

    async def down():
        while chunk := await sock_r.read(65536):
            await ws.send(chunk)

    tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    sock_w.close()
    await ws.close()


# --- the application -----------------------------------------------------------------

class App:
    def __init__(self, port):
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.jobs = Jobs()
        self.stats = Stats()

    # auth -------------------------------------------------------------------------
    def check(self, method, path, query, headers):
        host = headers.get("host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            raise HTTPError(403, "bad Host header")
        cookies = dict(c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c)
        if not secrets.compare_digest(cookies.get("fcvm_token", ""), self.token):
            raise HTTPError(401, "open the URL printed by `fcvm serve` (it carries the access token)")
        if method != "GET" or headers.get("upgrade", "").lower() == "websocket":
            origin = headers.get("origin", "")
            if origin not in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
                raise HTTPError(403, "cross-origin request refused")

    # routing ----------------------------------------------------------------------
    async def route(self, method, path, query, headers, body, reader, writer):
        if path == "/" and "token" in query:
            if not secrets.compare_digest(query["token"], self.token):
                raise HTTPError(401, "wrong token")
            return 302, {"Location": "/", "Set-Cookie":
                         f"fcvm_token={self.token}; HttpOnly; SameSite=Strict; Path=/"}, b""
        self.check(method, path, query, headers)
        if path == "/favicon.ico":
            return self.static("favicon.svg")
        if path == "/" or path.startswith("/static/"):
            return self.static("index.html" if path == "/" else path[len("/static/"):])
        if path.startswith("/ws/"):
            return await self.websocket(path, headers, reader, writer)
        if not path.startswith("/api/"):
            raise HTTPError(404, "not found")
        data = json.loads(body or b"{}") if body else {}
        result = await self.api(method, path[5:].strip("/").split("/"), query, data)
        return 200, {"Content-Type": "application/json"}, json.dumps(result).encode()

    def static(self, rel):
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            raise HTTPError(404, "not found")
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        return 200, {"Content-Type": ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/javascript")) else ""),
                     "Cache-Control": "no-cache"}, data

    # API ----------------------------------------------------------------------------
    async def api(self, method, parts, q, d):
        name = parts[1] if len(parts) > 1 else None
        if name is not None and not NAME.match(name):
            raise HTTPError(400, "invalid name")
        match (method, parts[0], len(parts)):
            case ("GET", "host", 1):
                return await self.host_info()
            case ("GET", "vms", 1):
                return await fcvm_json("ls", "--json")
            case ("GET", "vms", 2):
                return await fcvm_json("inspect", name)
            case ("POST", "vms", 1):
                return await self.launch(d)
            case ("POST", "vms", 3):
                return await self.vm_action(name, parts[2], d)
            case ("DELETE", "vms", 2):
                await fcvm("stop", name, timeout=60)
                if os.path.isdir(os.path.join(VMS, name)):
                    await fcvm_ok("rm", name)
                return {"removed": name}
            case ("GET", "vms", 3) if parts[2] == "stats":
                return {"samples": list(self.stats.vms.get(name, []))}
            case ("GET", "vms", 3) if parts[2] == "logs":
                out, _ = await fcvm_ok("logs", name)
                return {"log": out[-200000:]}
            case ("GET", "vms", 3) if parts[2] == "egress":
                out, _ = await fcvm_ok("egress", name, "-n", "200")
                return {"text": ANSI.sub("", out)}
            case ("GET", "images", 1):
                return await fcvm_json("images", "--json")
            case ("POST", "images", 1):
                ref = d.get("ref", "").strip()
                if not ref or ref.startswith("-"):
                    raise HTTPError(400, "image reference required")
                args = ["import", ref] + ([d["name"]] if d.get("name") else [])
                return self.jobs.start("import", f"import {ref}", args)
            case ("DELETE", "images", 2):
                await fcvm_ok("rmi", name)
                return {"removed": name}
            case ("GET", "snapshots", 1):
                return await fcvm_json("snapshot", "ls", "--json")
            case ("POST", "snapshots", 3) if parts[2] == "fork":
                count = int(d.get("count") or 1)
                args = ["fork", name] + ([d["name"]] if d.get("name") else []) + ["-n", count]
                out, err = await fcvm_ok(*args, timeout=60 + 30 * count)
                return {"log": err}
            case ("DELETE", "snapshots", 2):
                await fcvm_ok("snapshot", "rm", name)
                return {"removed": name}
            case ("GET", "volumes", 1):
                return await fcvm_json("volume", "ls", "--json")
            case ("DELETE", "volumes", 2):
                await fcvm_ok("volume", "rm", name)
                return {"removed": name}
            case ("GET", "jobs", 1):
                return self.jobs.list()
            case ("GET", "presets", 1):
                return self.presets()
        raise HTTPError(404, "unknown API endpoint")

    def presets(self):
        out = []
        for line in read(os.path.join(ROOT, "lib", "egress-presets.conf")).splitlines():
            if line.startswith("@"):
                name, *hosts = line.split()
                out.append({"name": name, "hosts": hosts})
        return out

    async def host_info(self):
        st = os.statvfs(ROOT)
        load = read("/proc/loadavg").split()[:3]
        taps = lambda p: len([n for n in os.listdir("/sys/class/net") if re.fullmatch(p + r"\d+", n)])
        locks = os.path.join(VMS, ".locks")
        return {
            "hostname": os.uname().nodename, "cpus": os.cpu_count(), "load": [float(x) for x in load],
            "kernel": os.path.basename(os.path.realpath(os.path.join(ROOT, "kernels", "vmlinux"))),
            "disk_total": st.f_blocks * st.f_frsize, "disk_free": st.f_bavail * st.f_frsize,
            "taps_full": taps("fctap"), "taps_restricted": taps("fcrtap"),
            "history": list(self.stats.host),
            "vm_history": {n: list(s)[-1:] for n, s in self.stats.vms.items()},
        }

    async def launch(self, d):
        name, image = d.get("name", "").strip(), d.get("image", "")
        if not NAME.match(name or "-"):
            raise HTTPError(400, "a VM name is required (letters, digits, . _ -)")
        args = ["create", name, image, "--vcpus", int(d.get("vcpus") or 2), "--mem", int(d.get("mem_mib") or 1024)]
        net = d.get("network", "full")
        if net == "none":
            args += ["--net", "none"]
        elif net == "restricted":
            allow = [a.strip() for a in d.get("allow", []) if a.strip()]
            if not allow:
                raise HTTPError(400, "a restricted network needs at least one allowed host or preset")
            args += ["--allow", ",".join(allow)]
        for p in d.get("ports", []):
            if p.strip():
                args += ["-p", p.strip()]
        for v in d.get("volumes", []):
            if v.strip():
                args += ["-v", v.strip()]
        if d.get("idle"):
            args.append("--idle")
        cmd = d.get("command", "").strip()
        if cmd and not d.get("idle"):
            args += ["--", "sh", "-c", cmd]
        await fcvm_ok(*args)
        if d.get("start", True):
            try:
                await fcvm_ok("start", name, timeout=60)
            except HTTPError as e:
                raise HTTPError(400, f"created '{name}', but it failed to start: {e.message}")
        return await fcvm_json("inspect", name)

    async def vm_action(self, name, action, d):
        if action == "start":
            await fcvm_ok("start", name, timeout=60)
        elif action == "stop":
            await fcvm_ok("stop", name, timeout=60)
        elif action == "restart":
            await fcvm_ok("stop", name, timeout=60)
            await fcvm_ok("start", name, timeout=60)
        elif action == "snapshot":
            snap = d.get("name", "").strip()
            if not NAME.match(snap or "-"):
                raise HTTPError(400, "a snapshot name is required")
            await fcvm_ok("snapshot", name, snap, timeout=300)
        elif action == "commit":
            img = d.get("image", "").strip()
            if not NAME.match(img or "-"):
                raise HTTPError(400, "an image name is required")
            await fcvm_ok("commit", name, img)
        else:
            raise HTTPError(404, f"unknown action {action}")
        return await fcvm_json("inspect", name) if os.path.isdir(os.path.join(VMS, name)) else {}

    # terminals ------------------------------------------------------------------------
    async def websocket(self, path, headers, reader, writer):
        m = re.fullmatch(r"/ws/vms/([A-Za-z0-9_][A-Za-z0-9_.-]*)/(console|shell)", path)
        if not m or headers.get("upgrade", "").lower() != "websocket":
            raise HTTPError(404, "not found")
        name, kind = m.groups()
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + WS_GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        d = os.path.join(VMS, name)
        try:
            if kind == "console":
                r, w = await asyncio.open_unix_connection(os.path.join(d, "console.sock"))
                w.write(b"T")                       # recent output first, then live
                await w.drain()
                await pump_ws(ws, r, w)
            else:
                await self.shell(ws, d)
        except (OSError, asyncio.IncompleteReadError) as e:
            await ws.send(f"\r\n\x1b[31m[fcvm: {e.strerror if isinstance(e, OSError) else e} - is the VM running?]\x1b[0m\r\n".encode())
            await ws.close()
        return None

    async def shell(self, ws, d):
        """An interactive shell through the exec agent (vsock), like `fcvm shell`."""
        op, first = await ws.recv()         # {"rows": R, "cols": C, "user": "..."}
        opts = json.loads(first or b"{}") if op == 0x1 else {}
        r, w = await asyncio.open_unix_connection(os.path.join(d, "vsock.sock"))
        w.write(b"CONNECT 1024\n")
        await w.drain()
        line = await r.readline()
        if not line.startswith(b"OK "):
            raise OSError(0, "the exec agent is not answering")
        fields = ["fcvm2", "1", str(opts.get("rows", 24)), str(opts.get("cols", 80)), "xterm-256color",
                  opts.get("user", ""), "", "0"]
        payload = b"\0".join(f.encode() for f in fields) + b"\0"
        w.write(b"R" + struct.pack(">I", len(payload)) + payload)
        await w.drain()

        async def up():
            while True:
                op, data = await ws.recv()
                if op is None:
                    break
                if op == 0x1:                   # control: {"resize": [rows, cols]}
                    msg = json.loads(data)
                    if "resize" in msg:
                        rows, cols = msg["resize"]
                        w.write(b"W" + struct.pack(">IHH", 4, rows, cols))
                else:
                    w.write(b"D" + struct.pack(">I", len(data)) + data)
                await w.drain()

        async def down():
            buf = b""
            while chunk := await r.read(65536):
                buf += chunk
                while len(buf) >= 5:
                    kind, n = buf[:1], struct.unpack(">I", buf[1:5])[0]
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if kind in (b"D", b"E"):
                        await ws.send(body)
                    elif kind == b"X":
                        code = struct.unpack(">i", body)[0]
                        await ws.send(f"\r\n\x1b[2m[shell exited with status {code}]\x1b[0m\r\n".encode())
                        return

        tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        w.close()
        await ws.close()

    # HTTP --------------------------------------------------------------------------------
    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            url = urllib.parse.urlsplit(target)
            query = dict(urllib.parse.parse_qsl(url.query))
            n = int(headers.get("content-length") or 0)
            if n > 1 << 20:
                raise HTTPError(413, "request too large")
            body = await reader.readexactly(n) if n else b""
            try:
                result = await self.route(method, url.path, query, headers, body, reader, writer)
            except HTTPError as e:
                result = e.status, {"Content-Type": "application/json"}, json.dumps({"error": e.message}).encode()
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                result = 400, {"Content-Type": "application/json"}, json.dumps({"error": f"bad request: {e}"}).encode()
            if result is None:                  # a WebSocket took over the connection
                return
            status, hdrs, data = result
            reason = {200: "OK", 302: "Found", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                      404: "Not Found", 413: "Payload Too Large", 504: "Gateway Timeout"}.get(status, "Error")
            hdrs = {**hdrs, "Content-Length": str(len(data)), "Connection": "close",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'"}
            writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode() +
                         "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode() + b"\r\n" + data)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()

    async def serve(self):
        server = await asyncio.start_server(self.handle, "127.0.0.1", self.port, limit=1 << 20)
        asyncio.get_running_loop().create_task(self.stats.run())
        url = f"http://127.0.0.1:{self.port}/?token={self.token}"
        print(f"\033[1;34m==>\033[0m fcvm web console: {url}", file=sys.stderr, flush=True)
        print("    (local only; the token in the URL is the key - Ctrl-C to stop)", file=sys.stderr, flush=True)
        async with server:
            await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(prog="fcvm serve")
    ap.add_argument("--port", type=int, default=8686)
    ap.add_argument("--r-prefix", default="172.30.1")
    args = ap.parse_args()
    global R_PREFIX
    R_PREFIX = args.r_prefix
    try:
        asyncio.run(App(args.port).serve())
    except KeyboardInterrupt:
        pass
    except OSError as e:
        sys.exit(f"fcvm serve: cannot listen on 127.0.0.1:{args.port}: {e.strerror}")


if __name__ == "__main__":
    main()
