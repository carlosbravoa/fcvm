"""Egress allowlist matching, and the mirror (lib/egress_proxy.py)."""
import asyncio
import http.client
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

from helpers import ROOT  # noqa: F401  (sets up sys.path)
import egress_proxy as ep


class Mirror(unittest.TestCase):
    def test_cacheable(self):
        self.assertTrue(ep.cacheable("/packages/x/six-1.0-py3-none-any.whl", {}))
        self.assertTrue(ep.cacheable("/x/-/x-1.0.0.tgz", {"Cache-Control": "no-store"}))   # by name: always
        self.assertTrue(ep.cacheable("/simple/six/", {"Cache-Control": "public, max-age=86400"}))
        self.assertTrue(ep.cacheable("/a", {"Cache-Control": "immutable"}))
        self.assertFalse(ep.cacheable("/simple/six/", {"Cache-Control": "max-age=600"}))
        self.assertFalse(ep.cacheable("/simple/six/", {}))
        self.assertFalse(ep.cacheable("/a", {"Cache-Control": "private, max-age=90000"}))

    def test_rewrite_points_allowed_hosts_at_the_mirror(self):
        page = (b'<a href="https://files.pythonhosted.org/packages/a.whl#sha256=1">a</a> '
                b'<a href="https://evil.example/x">x</a> https://api.github.com:8443/y')
        out = ep.rewrite(page, "text/html", ["pypi.org", "files.pythonhosted.org", "*.github.com"], "http://172.30.1.1:3128/cache")
        self.assertIn(b'href="http://172.30.1.1:3128/cache/files.pythonhosted.org/packages/a.whl#sha256=1"', out)
        self.assertIn(b"https://evil.example/x", out)                    # not allowed: untouched
        self.assertIn(b"http://172.30.1.1:3128/cache/api.github.com:8443/y", out)
        self.assertEqual(ep.rewrite(page, "application/octet-stream", ["pypi.org"], "m"), page)   # binaries: never
        self.assertEqual(ep.rewrite(page, "text/html", [], "m"), page)

    def test_rewrites_npm_abbreviated_metadata(self):
        """npm asks for application/vnd.npm.install-v1+json; its tarball links
        must point at the mirror too, or nothing of npm's is ever cached."""
        body = b'{"versions":{"1.0.0":{"dist":{"tarball":"https://registry.npmjs.org/left-pad/-/left-pad-1.0.0.tgz"}}}}'
        out = ep.rewrite(body, "application/vnd.npm.install-v1+json; charset=utf-8", ["registry.npmjs.org"], "http://10.1.0.1:3128/cache")
        self.assertIn(b'"tarball":"http://10.1.0.1:3128/cache/registry.npmjs.org/left-pad/-/left-pad-1.0.0.tgz"', out)

    def test_redirects_stay_inside_the_allowlist(self):
        h = ep.PolicyRedirect(["github.com", "*.githubusercontent.com"])
        req = urllib.request.Request("https://github.com/x/releases/download/v1/x.tar.gz")
        self.assertIsNotNone(h.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/a/b"))
        with self.assertRaises(urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/a")
        with self.assertRaises(urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "http://github.com/plain")   # no downgrade

    def test_failed_fetch_leaves_no_temp_file(self):
        d = tempfile.mkdtemp(prefix="fcvm-mirror-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ep.CACHE_DIR = d
        try:
            with self.assertRaises(OSError):
                ep.fetch("https://invalid.invalid/x", {})
        finally:
            ep.CACHE_DIR = None
        self.assertEqual(os.listdir(d), [])

    def test_end_to_end_with_a_fake_upstream(self):
        d = tempfile.mkdtemp(prefix="fcvm-mirror-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        policy_dir, cache_dir = os.path.join(d, "policy"), os.path.join(d, "cache")
        os.makedirs(policy_dir)
        os.makedirs(cache_dir)
        log = os.path.join(d, "egress.log")
        with open(os.path.join(policy_dir, "127.0.0.1.json"), "w") as f:
            json.dump({"vm": "box", "allow": ["pypi.org", "files.pythonhosted.org"], "log": log}, f)
        fetched = []

        def fake_fetch(url, headers, patterns=()):
            fetched.append(url)
            path = os.path.join(d, "body")
            if url.endswith(".whl"):
                body, ctype = b"WHEEL-BYTES", "application/octet-stream"
            elif "pypi.org/simple/" in url:
                body, ctype = b'<a href="https://files.pythonhosted.org/packages/six.whl">six</a>', "text/html"
            else:
                with open(path, "wb") as f:
                    f.write(b"nope")
                return 404, {"Content-Type": "text/plain"}, path
            with open(path, "wb") as f:
                f.write(body)
            return 200, {"Content-Type": ctype}, path

        async def go():
            ep.CACHE_DIR = cache_dir
            saved, ep.fetch = ep.fetch, fake_fetch
            try:
                srv = await asyncio.start_server(lambda r, w: ep.handle(r, w, policy_dir, f"127.0.0.1:{port}"), "127.0.0.1", 0)
                port = srv.sockets[0].getsockname()[1]
                srv.close()                       # the listen string must carry the real port: start again with it
                srv = await asyncio.start_server(lambda r, w: ep.handle(r, w, policy_dir, f"127.0.0.1:{port}"), "127.0.0.1", port)
                async with srv:
                    return await asyncio.get_running_loop().run_in_executor(None, requests, port)
            finally:
                ep.fetch = saved
                ep.CACHE_DIR = None

        def get(port, target, absolute=False):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", (f"http://127.0.0.1:{port}" if absolute else "") + target, headers={"Accept": "text/html"})
            r = c.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()

        def requests(port):
            out = [get(port, "/cache/pypi.org/simple/six/", absolute=True)]        # through http_proxy: absolute form
            out.append(get(port, "/cache/files.pythonhosted.org/packages/six.whl"))  # a wheel: cached
            out.append(get(port, "/cache/files.pythonhosted.org/packages/six.whl"))  # again: from the cache
            out.append(get(port, "/cache/evil.example/x"))                           # not allowed
            out.append(get(port, "/cache/pypi.org/missing"))                         # upstream 404
            # allowed by its suffix, but a URL would fetch it from evil.example
            out.append(get(port, "/cache/evil.example?x.pythonhosted.org/packages/six.whl"))
            out.append(get(port, "/cache/evil.example#files.pythonhosted.org/x"))
            out.append(get(port, "/cache/bob@files.pythonhosted.org/x"))
            return out
        page, whl1, whl2, denied, missing, *injected = asyncio.run(go())
        self.assertEqual(page[0], 200)
        self.assertIn(b'href="http://127.0.0.1:', page[2])                           # rewritten to the mirror
        self.assertEqual((whl1[0], whl1[2]), (200, b"WHEEL-BYTES"))
        self.assertEqual((whl2[0], whl2[2]), (200, b"WHEEL-BYTES"))
        self.assertEqual(len([u for u in fetched if u.endswith(".whl")]), 1)         # fetched once
        self.assertEqual(denied[0], 403)
        self.assertEqual(missing[0], 404)
        self.assertEqual([r[0] for r in injected], [403, 403, 403])
        self.assertFalse([u for u in fetched if "evil" in u or "bob@" in u])
        decisions = [json.loads(l)["decision"] for l in open(log)]
        self.assertEqual(decisions, ["fetch", "cache-miss", "cache-hit", "deny", "fetch", "deny", "deny", "deny"])
        self.assertTrue(any(f.endswith(".data") for _, _, fs in os.walk(cache_dir) for f in fs))


class Allowed(unittest.TestCase):
    def test_exact_host_default_ports(self):
        self.assertTrue(ep.allowed("example.com", 443, ["example.com"]))
        self.assertTrue(ep.allowed("example.com", 80, ["example.com"]))
        self.assertFalse(ep.allowed("example.com", 22, ["example.com"]))

    def test_case_and_trailing_dot(self):
        self.assertTrue(ep.allowed("Example.COM.", 443, ["example.com"]))

    def test_wildcard_is_subdomains_only(self):
        self.assertTrue(ep.allowed("api.github.com", 443, ["*.github.com"]))
        self.assertTrue(ep.allowed("a.b.github.com", 443, ["*.github.com"]))
        self.assertFalse(ep.allowed("github.com", 443, ["*.github.com"]))
        self.assertFalse(ep.allowed("evilgithub.com", 443, ["*.github.com"]))

    def test_explicit_port(self):
        self.assertTrue(ep.allowed("corp.example", 8443, ["corp.example:8443"]))
        self.assertFalse(ep.allowed("corp.example", 443, ["corp.example:8443"]))

    def test_suffix_is_not_a_match(self):
        self.assertFalse(ep.allowed("pypi.org.evil.com", 443, ["pypi.org"]))
        self.assertFalse(ep.allowed("notpypi.org", 443, ["pypi.org"]))

    def test_empty_allowlist(self):
        self.assertFalse(ep.allowed("example.com", 443, []))


class HostPort(unittest.TestCase):
    def test_split(self):
        self.assertEqual(ep.split_hostport("example.com:8443", 443), ("example.com", 8443))
        self.assertEqual(ep.split_hostport("example.com", 443), ("example.com", 443))
        self.assertEqual(ep.split_hostport("[::1]:8080", 80), ("::1", 8080))
        self.assertEqual(ep.split_hostport("[::1]", 80), ("::1", 80))


if __name__ == "__main__":
    unittest.main()
