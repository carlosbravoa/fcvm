#!/usr/bin/env python3
"""Egress proxy for restricted VMs (fcvm --allow).

  egress_proxy.py --listen IP:PORT --policy-dir DIR

VMs on the restricted bridge can reach nothing but this proxy. Each request is
matched to its VM by source address: DIR/<vm-ip>.json holds
{"vm": NAME, "allow": [patterns], "log": PATH}, written by `fcvm start` and
removed when the VM stops, and re-read on every request, so allowlist changes
apply immediately.

Handles CONNECT (HTTPS and any TLS; the host name is checked, never the
content) and absolute-form requests (one per connection): http:// is
forwarded as is; https:// (what busybox wget sends instead of CONNECT) is
fetched over TLS by the proxy, with certificate verification. Names
are resolved on the host, so split-DNS / corporate names work. Every decision
is appended as a JSON line to the VM's log.

Patterns: "example.com" (exact), "*.example.com" (any subdomain), optionally
with ":PORT"; without a port, 80 and 443 are allowed.
"""
import argparse
import asyncio
import json
import os
import ssl
import sys
import time

DEFAULT_PORTS = {80, 443}
HEADER_LIMIT = 64 * 1024


def allowed(host, port, patterns):
    host = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower()
        pport = None
        if ":" in p:
            p, pport = p.rsplit(":", 1)
        if (int(pport) != port) if pport else (port not in DEFAULT_PORTS):
            continue
        if p == host or (p.startswith("*.") and host.endswith(p[1:])):
            return True
    return False


def policy(policy_dir, ip):
    try:
        with open(os.path.join(policy_dir, f"{ip}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def record(pol, entry):
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    try:
        with open(pol["log"], "a") as f:
            f.write(json.dumps(entry) + "\n")
    except (OSError, KeyError, TypeError):
        pass


async def splice(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        pass


def split_hostport(s, default_port):
    if s.startswith("["):                       # [v6]:port
        host, _, rest = s[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    host, _, port = s.partition(":")
    return host, int(port) if port else default_port


async def handle(reader, writer, policy_dir):
    ip = writer.get_extra_info("peername")[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
        tls = False
        if method == "CONNECT":
            host, port = split_hostport(target, 443)
        elif target.startswith(("http://", "https://")):
            tls = target.startswith("https://")
            hostport, _, path = target.split("://", 1)[1].partition("/")
            host, port = split_hostport(hostport, 443 if tls else 80)
            path = "/" + path
        else:
            raise ValueError
    except ValueError:
        record(policy(policy_dir, ip), {"method": lines[0][:80], "decision": "bad-request"})
        writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nfcvm egress proxy: send CONNECT or an absolute http:// URL\n")
        await writer.drain()
        writer.close()
        return

    pol = policy(policy_dir, ip)
    ok = bool(pol) and allowed(host, port, pol.get("allow", []))
    record(pol, {"vm": pol and pol.get("vm"), "method": method, "host": host, "port": port,
                 "decision": "allow" if ok else "deny"})
    if not ok:
        vm = pol.get("vm") if pol else "?"
        body = (f"fcvm egress policy: {host}:{port} is not allowed for VM '{vm}'.\n"
                f"Allow it on the host with: fcvm egress {vm} --allow {host}\n").encode()
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()
        return

    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(
            host, port, ssl=ssl.create_default_context() if tls else None,
            server_hostname=host if tls else None), 15)
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
        body = f"fcvm egress proxy: cannot reach {host}:{port}: {e or 'timeout'}\n".encode()
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()
        return

    if method == "CONNECT":
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
    else:
        headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
        up_w.write(f"{method} {path} {version}\r\n".encode("latin-1") +
                   "\r\n".join(headers + ["Connection: close", "", ""]).encode("latin-1"))
    await asyncio.gather(splice(reader, up_w), splice(up_r, writer))
    for w in (writer, up_w):
        w.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True)
    ap.add_argument("--policy-dir", required=True)
    args = ap.parse_args()
    host, port = args.listen.rsplit(":", 1)
    try:
        server = await asyncio.start_server(lambda r, w: handle(r, w, args.policy_dir), host, int(port),
                                            reuse_address=True, limit=HEADER_LIMIT)
    except OSError as e:
        sys.exit(f"egress proxy: cannot listen on {args.listen}: {e.strerror}")
    print(f"egress proxy listening on {args.listen}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
