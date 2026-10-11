"""lib/oci_export.py: an OCI layout from a rootfs tar and fcvm's metadata,
readable by oci_import, and a push to a (fake) registry."""
import http.server
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
import urllib.request

from helpers import ROOT  # noqa: F401
import oci_export as ex
import oci_import as im


def rootfs_tar(path):
    with tarfile.open(path, "w") as tf:
        for name, mode in (("bin", 0o755), ("etc", 0o755)):
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode = tarfile.DIRTYPE, mode
            tf.addfile(ti)
        ti = tarfile.TarInfo("bin/app")
        ti.mode, ti.uid, ti.gid, ti.size = 0o755, 1000, 1000, 6
        tf.addfile(ti, io.BytesIO(b"#!/bin/sh\n"[:6]))
        ti = tarfile.TarInfo("etc/passwd")
        ti.size = 0
        tf.addfile(ti)


META = {"ref": "example/app:1", "entrypoint": ["/bin/app"], "cmd": ["--serve"], "env": ["PATH=/bin", "X=1"],
        "workdir": "/srv", "user": "app", "exposed_ports": ["8080/tcp"], "type": "app"}


class Build(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-ociexp-")
        rootfs_tar(os.path.join(self.d, "rootfs.tar"))
        with open(os.path.join(self.d, "meta.json"), "w") as f:
            json.dump(META, f)
        self.out = os.path.join(self.d, "oci")
        os.makedirs(self.out)
        ex.build(os.path.join(self.d, "rootfs.tar"), os.path.join(self.d, "meta.json"), "myapp", self.out, "x86_64")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_layout_is_importable(self):
        config, layers, digest, name = im.load_local(self.out, None, "x86_64", self.d)
        self.assertEqual(name, "myapp:latest")
        c = config["config"]
        self.assertEqual((c["Entrypoint"], c["Cmd"], c["WorkingDir"], c["User"]), (["/bin/app"], ["--serve"], "/srv", "app"))
        self.assertEqual(c["Env"], ["PATH=/bin", "X=1"])
        self.assertEqual(list(c["ExposedPorts"]), ["8080/tcp"])
        self.assertEqual(config["architecture"], "amd64")
        self.assertEqual(len(layers), 1)
        with im.open_layer(layers[0]) as tf:
            names = sorted(m.name for m in tf)
        self.assertEqual(names, ["bin", "bin/app", "etc", "etc/passwd"])
        # the diff id is the uncompressed layer's digest
        with open(os.path.join(self.d, "rootfs.tar"), "rb") as f:
            self.assertEqual(config["rootfs"]["diff_ids"], [im.sha256_file(os.path.join(self.d, "rootfs.tar"))])

    def test_system_image_runs_init(self):
        with open(os.path.join(self.d, "meta.json"), "w") as f:
            json.dump({"type": "system", "ref": "ubuntu:resolute"}, f)
        out = os.path.join(self.d, "oci2")
        os.makedirs(out)
        ex.build(os.path.join(self.d, "rootfs.tar"), os.path.join(self.d, "meta.json"), "ubuntu", out, "x86_64")
        config = im.load_local(out, None, "x86_64", self.d)[0]
        self.assertEqual(config["config"]["Cmd"], ["/sbin/init"])


class FakeRegistry(http.server.BaseHTTPRequestHandler):
    """Enough of the v2 API for a push: an upload session, blobs, manifests."""
    store = {}

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200 if self.path.split("/")[-1] in self.store else 404)
        self.end_headers()

    def do_POST(self):
        self.send_response(202)
        self.send_header("Location", "/v2/org/app/blobs/uploads/session1")
        self.end_headers()

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if "/blobs/uploads/" in self.path:
            self.store[self.path.split("digest=")[1].split(":")[1]] = body
        else:
            self.store["manifest:" + self.path.split("/")[-1]] = (self.headers.get("Content-Type"), body)
        self.send_response(201)
        self.end_headers()


class Push(unittest.TestCase):
    def test_token_asks_for_push_scope(self):
        """When the 401 challenge names no scope, the token request must carry
        the pusher's (pull,push), or the push loops on 401."""
        asked = []

        class Tok:
            def __init__(self, req):
                asked.append(req.full_url)
            def __enter__(self):
                return io.BytesIO(b'{"token": "t"}')
            def __exit__(self, *a):
                pass
        p = ex.Pusher("reg.example", "org/app")
        p.scope = "repository:org/app:pull,push"
        saved, urllib.request.urlopen = urllib.request.urlopen, Tok
        try:
            p._auth('Bearer realm="https://reg.example/token",service="reg"')
            p._auth('Bearer realm="https://reg.example/token",service="reg",scope="repository:org/app:pull"')
        finally:
            urllib.request.urlopen = saved
        self.assertEqual(p.token, "Bearer t")
        self.assertIn("scope=repository%3Aorg%2Fapp%3Apull%2Cpush", asked[0])
        self.assertIn("scope=repository%3Aorg%2Fapp%3Apull&", asked[1] + "&")

    def test_push_to_a_registry(self):
        d = tempfile.mkdtemp(prefix="fcvm-ocipush-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        rootfs_tar(os.path.join(d, "rootfs.tar"))
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(META, f)
        out = os.path.join(d, "oci")
        os.makedirs(out)
        ex.build(os.path.join(d, "rootfs.tar"), os.path.join(d, "meta.json"), "app", out, "x86_64")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        saved = ex.Pusher.__init__

        def plain_http(self, registry, repo):       # the fake speaks http, not https
            saved(self, registry, repo)
            self.base = f"http://{registry}/v2/{repo}"
        ex.Pusher.__init__ = plain_http
        try:
            ex.push(out, f"127.0.0.1:{port}/org/app:v1")
        finally:
            ex.Pusher.__init__ = saved
        with open(os.path.join(out, "index.json")) as f:
            mdigest = json.load(f)["manifests"][0]["digest"]
        ctype, body = FakeRegistry.store["manifest:v1"]
        self.assertEqual(ctype, ex.MANIFEST_TYPE)
        manifest = json.loads(body)
        self.assertIn(manifest["layers"][0]["digest"][7:], FakeRegistry.store)
        self.assertIn(manifest["config"]["digest"][7:], FakeRegistry.store)
        self.assertEqual(im.sha256_file(os.path.join(out, "blobs", "sha256", mdigest[7:])), mdigest)


if __name__ == "__main__":
    unittest.main()
