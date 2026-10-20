#!/usr/bin/env python3
"""Egress proxy for restricted VMs (fcvm --allow).

  egress_proxy.py --listen IP:PORT --policy-dir DIR [--cache-dir DIR]

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

The mirror (--cache-dir, fcvm --cache): GET http://IP:PORT/cache/HOST/PATH
fetches https://HOST/PATH on the guest's behalf (the allowlist applies to
HOST), keeps immutable artifacts (wheels, tarballs, anything the server marks
immutable or cacheable for a day) under the cache directory, and rewrites
links in package indexes (PyPI's simple pages, npm's metadata) to point back
at the mirror, so pip and npm fetch every file through it. A client sees it
over plain HTTP on the isolated bridge; the TLS to the real host is the
proxy's.
"""
import argparse
import asyncio
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORTS = {80, 443}
HEADER_LIMIT = 64 * 1024
CACHE_DIR = None
CACHE_SUFFIXES = (".whl", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".crate", ".gem", ".jar", ".deb", ".apk", ".egg")
REWRITE_TYPES = ("text/html", "application/json", "application/vnd.pypi.simple", "application/vnd.npm.install-v1+json")
HOST_RE = re.compile(r"^[a-z0-9.-]+$", re.I)   # a mirror host: a plain DNS name, nothing a URL could misread
PASS_HEADERS = ("accept", "user-agent", "accept-encoding", "if-none-match", "if-modified-since", "range")


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


# --- the mirror ---------------------------------------------------------------------

def cacheable(path, headers):
    """Keep a response: an artifact by its name, or one the server calls
    immutable or good for at least a day."""
    if path.lower().endswith(CACHE_SUFFIXES):
        return True
    cc = headers.get("Cache-Control", "").lower()
    if "no-store" in cc or "private" in cc:
        return False
    if "immutable" in cc:
        return True
    m = re.search(r"max-age=(\d+)", cc)
    return bool(m) and int(m.group(1)) >= 86400


def host_regex(patterns):
    """A regex for the hosts an allowlist names (ports aside)."""
    alts = []
    for p in patterns:
        p = p.lower().rsplit(":", 1)[0] if re.search(r":\d+$", p) else p.lower()
        if p.startswith("*."):
            alts.append(r"[A-Za-z0-9.-]+" + re.escape(p[1:]))
        elif p:
            alts.append(re.escape(p))
    return re.compile(rb"https://(" + "|".join(alts).encode() + rb")(:\d+)?/") if alts else None


def rewrite(body, ctype, patterns, mirror):
    """Links to allowed hosts in an index page or package metadata, pointed at the mirror."""
    if not ctype.lower().startswith(REWRITE_TYPES):
        return body
    rx = host_regex(patterns)
    if not rx:
        return body
    return rx.sub(lambda m: mirror.encode() + b"/" + m.group(1) + (m.group(2) or b"") + b"/", body)


TENANT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def cache_paths(host, port, path, tenant=None):
    """Where an artifact lives: cache/egress/XX/KEY for VMs without a tenant,
    cache/egress/tenants/NAME/XX/KEY for a tenant's VMs, so what one tenant
    installed never shows another what to expect (or serves it a file it
    didn't fetch). The policy carries the VM's tenant."""
    key = hashlib.sha256(f"{host}:{port}{path}".encode()).hexdigest()
    base = os.path.join(CACHE_DIR, *(("tenants", tenant) if tenant else ()), key[:2], key)
    return base + ".meta", base + ".data"


class PolicyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a host the VM's allowlist covers (a GitHub
    release to its CDN, say); anything else is a policy error, not a fetch."""
    def __init__(self, patterns):
        self.patterns = patterns

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or not allowed(u.hostname or "", u.port or 443, self.patterns):
            raise urllib.error.URLError(f"redirect to {u.hostname}:{u.port or 443} is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, headers, patterns=()):
    """Upstream GET in a thread: (status, headers dict, path of the body) or raises."""
    req = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k.lower() in PASS_HEADERS and k.lower() != "accept-encoding"})
    opener = urllib.request.build_opener(PolicyRedirect(patterns))
    os.makedirs(CACHE_DIR, exist_ok=True)       # `fcvm prune` may have removed it while we run
    tmp = tempfile.NamedTemporaryFile(dir=CACHE_DIR, prefix=".fetch-", delete=False)
    try:
        with opener.open(req, timeout=120) as r:
            while chunk := r.read(1 << 20):
                tmp.write(chunk)
            tmp.close()
            return r.status, dict(r.headers), tmp.name
    except urllib.error.HTTPError as e:
        tmp.close()
        with open(tmp.name, "wb") as f:
            f.write(e.read())
        return e.code, dict(e.headers), tmp.name
    except BaseException:
        tmp.close()
        os.unlink(tmp.name)
        raise


async def send_file(writer, status, ctype, path, extra=b""):
    size = os.path.getsize(path)
    reason = {200: "OK", 304: "Not Modified", 404: "Not Found"}.get(status, "OK" if status < 400 else "Error")
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {size}\r\n"
                 f"Connection: close\r\n\r\n".encode() + extra)
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            writer.write(chunk)
            await writer.drain()


async def mirror_request(writer, pol, headers, target, listen):
    """GET /cache/HOST[:PORT]/PATH for the VM whose policy is pol."""
    rest = target[len("/cache/"):]
    hostport, _, path = rest.partition("/")
    path = "/" + path
    try:
        host, port = split_hostport(hostport, 443)
    except ValueError:
        host, port = hostport, 0
    mirror = f"http://{listen}/cache"
    # The host goes into a URL we fetch: only a plain name may pass, so that
    # "evil.com?x.pypi.org" (allowed by its suffix, fetched from evil.com) cannot.
    tenant = (pol or {}).get("tenant") or None
    ok = bool(pol) and bool(HOST_RE.match(host)) and allowed(host, port, pol.get("allow", [])) \
        and (tenant is None or bool(TENANT_RE.match(tenant)))
    entry = {"vm": pol and pol.get("vm"), "method": "GET", "host": host, "port": port, "path": path[:120]}
    if not ok:
        record(pol, {**entry, "decision": "deny"})
        body = f"fcvm egress policy: {host}:{port} is not allowed for VM '{pol.get('vm') if pol else '?'}'.\n".encode()
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        return
    meta_path, data_path = cache_paths(host, port, path, tenant)
    if os.path.exists(meta_path) and os.path.exists(data_path):
        with open(meta_path) as f:
            meta = json.load(f)
        record(pol, {**entry, "decision": "cache-hit"})
        await send_file(writer, 200, meta.get("type", "application/octet-stream"), data_path)
        return
    url = f"https://{host}{'' if port == 443 else ':' + str(port)}{path}"
    try:
        status, rheaders, body_path = await asyncio.get_running_loop().run_in_executor(
            None, fetch, url, headers, pol.get("allow", []))
    except (OSError, ssl.SSLError, ValueError, http.client.HTTPException) as e:
        record(pol, {**entry, "decision": "error"})
        body = f"fcvm egress proxy: cannot fetch {url}: {e}\n".encode()
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        return
    ctype = rheaders.get("Content-Type", "application/octet-stream")
    try:
        if status == 200 and cacheable(path, rheaders):
            os.makedirs(os.path.dirname(data_path), exist_ok=True)
            with open(meta_path + ".tmp", "w") as f:
                json.dump({"url": url, "type": ctype, "stored": time.time()}, f)
            os.replace(body_path, data_path)
            os.replace(meta_path + ".tmp", meta_path)
            record(pol, {**entry, "decision": "cache-miss"})
            await send_file(writer, 200, ctype, data_path)
            return
        record(pol, {**entry, "decision": "fetch", "status": status})
        if status == 200 and ctype.lower().startswith(REWRITE_TYPES) and os.path.getsize(body_path) < (16 << 20):
            with open(body_path, "rb") as f:
                body = rewrite(f.read(), ctype, pol.get("allow", []), mirror)
            with open(body_path, "wb") as f:
                f.write(body)
        await send_file(writer, status, ctype, body_path)
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


# --- connections --------------------------------------------------------------------

async def handle(reader, writer, policy_dir, listen):
    ip = writer.get_extra_info("peername")[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
    try:
        method, target, version = lines[0].split(" ", 2)
        tls = False
        mirror_target = None
        if method == "CONNECT":
            host, port = split_hostport(target, 443)
        elif target.startswith(("http://", "https://")):
            tls = target.startswith("https://")
            hostport, _, path = target.split("://", 1)[1].partition("/")
            host, port = split_hostport(hostport, 443 if tls else 80)
            path = "/" + path
            if CACHE_DIR and not tls and hostport == listen and path.startswith("/cache/"):
                mirror_target = path
        elif CACHE_DIR and target.startswith("/cache/"):      # straight to the proxy, no http_proxy in between
            mirror_target = target
        else:
            raise ValueError
    except ValueError:
        record(policy(policy_dir, ip), {"method": lines[0][:80], "decision": "bad-request"})
        writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nfcvm egress proxy: send CONNECT or an absolute http:// URL\n")
        await writer.drain()
        writer.close()
        return

    pol = policy(policy_dir, ip)
    if mirror_target:
        if method != "GET":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        else:
            try:
                await mirror_request(writer, pol, headers, mirror_target, listen)
            except OSError:
                pass
        await writer.drain()
        writer.close()
        return
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
        hdrs = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
        up_w.write(f"{method} {path} {version}\r\n".encode("latin-1") +
                   "\r\n".join(hdrs + ["Connection: close", "", ""]).encode("latin-1"))
    await asyncio.gather(splice(reader, up_w), splice(up_r, writer))
    for w in (writer, up_w):
        w.close()


async def main():
    global CACHE_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True)
    ap.add_argument("--policy-dir", required=True)
    ap.add_argument("--cache-dir", help="serve and fill the mirror (GET /cache/HOST/PATH) from here")
    args = ap.parse_args()
    host, port = args.listen.rsplit(":", 1)
    if args.cache_dir:
        os.makedirs(args.cache_dir, exist_ok=True)
        CACHE_DIR = args.cache_dir
    try:
        server = await asyncio.start_server(lambda r, w: handle(r, w, args.policy_dir, args.listen), host, int(port),
                                            reuse_address=True, limit=HEADER_LIMIT)
    except OSError as e:
        sys.exit(f"egress proxy: cannot listen on {args.listen}: {e.strerror}")
    print(f"egress proxy listening on {args.listen}" + (f", mirror in {CACHE_DIR}" if CACHE_DIR else ""), file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
