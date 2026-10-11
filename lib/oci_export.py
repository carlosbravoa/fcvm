#!/usr/bin/env python3
"""Turn an fcvm image into an OCI image, and push it to a registry.

  oci_export.py build --rootfs rootfs.tar --meta images/NAME.json --name NAME --out DIR
  oci_export.py push DIR REF

build: the flattened rootfs (written by fc-init's export mode) becomes one
gzip layer, the image's metadata (entrypoint, cmd, env, workdir, user,
exposed ports) its config, in an OCI image layout directory: `docker load`,
`podman load` and `fcvm import oci:DIR` read it, and `tar -C DIR -cf x.tar .`
makes the archive form.

push: uploads the layout's blobs and manifest with the registry v2 API
(anonymous or REGISTRY_USER / REGISTRY_PASSWORD, as `fcvm import`).
Standard library only.
"""
import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import oci_import

MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"


def log(msg):
    print(f"\033[1;34m==>\033[0m {msg}", file=sys.stderr)


def die(msg):
    print(f"\033[1;31merror:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def write_blob(out_dir, data):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    os.makedirs(os.path.join(out_dir, "blobs", "sha256"), exist_ok=True)
    with open(os.path.join(out_dir, "blobs", "sha256", digest[7:]), "wb") as f:
        f.write(data)
    return digest, len(data)


def gzip_layer(rootfs, out_dir):
    """The rootfs tar, gzipped into the blob store: (digest, size, diff_id)."""
    os.makedirs(os.path.join(out_dir, "blobs", "sha256"), exist_ok=True)
    tmp = os.path.join(out_dir, "blobs", "sha256", ".layer")
    raw, comp = hashlib.sha256(), hashlib.sha256()

    class Tee:
        def __init__(self, f):
            self.f, self.n = f, 0

        def write(self, b):
            comp.update(b)
            self.n += len(b)
            self.f.write(b)

    with open(rootfs, "rb") as src, open(tmp, "wb") as dst:
        tee = Tee(dst)
        with gzip.GzipFile(fileobj=tee, mode="wb", mtime=0) as gz:
            while chunk := src.read(1 << 20):
                raw.update(chunk)
                gz.write(chunk)
    digest = "sha256:" + comp.hexdigest()
    os.replace(tmp, os.path.join(out_dir, "blobs", "sha256", digest[7:]))
    return digest, tee.n, "sha256:" + raw.hexdigest()


def build(rootfs, meta_path, name, out_dir, arch):
    with open(meta_path) as f:
        meta = json.load(f)
    log("compressing the layer")
    layer_digest, layer_size, diff_id = gzip_layer(rootfs, out_dir)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg = {}
    if meta.get("entrypoint"):
        cfg["Entrypoint"] = meta["entrypoint"]
    if meta.get("cmd"):
        cfg["Cmd"] = meta["cmd"]
    if not cfg.get("Entrypoint") and not cfg.get("Cmd"):
        cfg["Cmd"] = meta.get("argv") or (["/sbin/init"] if meta.get("type") == "system" else ["/bin/sh"])
    if meta.get("env"):
        cfg["Env"] = meta["env"]
    if meta.get("workdir") and meta["workdir"] != "/":
        cfg["WorkingDir"] = meta["workdir"]
    if meta.get("user") and meta["user"] != "root":
        cfg["User"] = meta["user"]
    if meta.get("exposed_ports"):
        cfg["ExposedPorts"] = {p: {} for p in meta["exposed_ports"]}
    config = {"architecture": oci_import.GOARCH.get(arch, arch), "os": "linux", "created": now, "config": cfg,
              "rootfs": {"type": "layers", "diff_ids": [diff_id]},
              "history": [{"created": now, "created_by": f"fcvm export {name}" +
                           (f" (from {meta['ref']})" if meta.get("ref") else "")}]}
    config_digest, config_size = write_blob(out_dir, json.dumps(config, indent=1).encode())
    manifest = {"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                "config": {"mediaType": CONFIG_TYPE, "digest": config_digest, "size": config_size},
                "layers": [{"mediaType": LAYER_TYPE, "digest": layer_digest, "size": layer_size}],
                "annotations": {"org.opencontainers.image.created": now}}
    manifest_digest, manifest_size = write_blob(out_dir, json.dumps(manifest, indent=1).encode())
    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump({"schemaVersion": 2, "manifests": [
            {"mediaType": MANIFEST_TYPE, "digest": manifest_digest, "size": manifest_size,
             "annotations": {"org.opencontainers.image.ref.name": f"{name}:latest",
                             "io.containerd.image.name": f"{name}:latest"}}]}, f, indent=1)
    with open(os.path.join(out_dir, "oci-layout"), "w") as f:
        json.dump({"imageLayoutVersion": "1.0.0"}, f)
    log(f"OCI image {manifest_digest[:19]}: one layer of {layer_size >> 20} MiB")
    return manifest_digest


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------

class Pusher(oci_import.Registry):
    """The registry client of oci_import, with the other verbs."""

    def call(self, method, url, data=None, headers=None, ok=(200, 201, 202)):
        for _ in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
            if self.token and url.startswith(self.base.rsplit("/v2/", 1)[0]):
                req.add_header("Authorization", self.token)
            try:
                with self.opener.open(req) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                if e.code == 401 and "WWW-Authenticate" in e.headers:
                    self.token = None
                    self._auth(e.headers["WWW-Authenticate"])
                    continue
                if e.code in ok:
                    return e.code, dict(e.headers), e.read()
                detail = e.read().decode(errors="replace")[:300]
                die(f"{method} {url}: HTTP {e.code} {detail}")
        die(f"{method} {url}: authentication failed")

    def has_blob(self, digest):
        try:
            status, _, _ = self.call("HEAD", f"{self.base}/blobs/{digest}", ok=(200, 404))
        except SystemExit:
            return False
        return status == 200

    def upload_blob(self, digest, data):
        if self.has_blob(digest):
            return
        status, headers, _ = self.call("POST", f"{self.base}/blobs/uploads/", data=b"",
                                       headers={"Content-Length": "0"})
        location = headers.get("Location") or headers.get("location")
        if not location:
            die("the registry gave no upload location")
        location = urllib.parse.urljoin(self.base + "/", location)
        sep = "&" if "?" in location else "?"
        self.call("PUT", f"{location}{sep}digest={digest}", data=data,
                  headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})

    def put_manifest(self, tag, data, media_type):
        self.call("PUT", f"{self.base}/manifests/{tag}", data=data,
                  headers={"Content-Type": media_type, "Content-Length": str(len(data))})


def push(layout, ref):
    registry, repo, tag = oci_import.parse_ref(ref)
    if tag.startswith("sha256:"):
        die("push wants a tag, not a digest")
    with open(os.path.join(layout, "index.json")) as f:
        desc = json.load(f)["manifests"][0]

    def blob(digest):
        with open(os.path.join(layout, "blobs", digest.split(":")[0], digest.split(":")[1]), "rb") as f:
            return f.read()

    manifest_bytes = blob(desc["digest"])
    manifest = json.loads(manifest_bytes)
    pusher = Pusher(registry, repo)
    pusher.scope = f"repository:{repo}:pull,push"
    for layer in manifest["layers"]:
        log(f"layer {layer['digest'][:19]} ({layer['size'] >> 20} MiB)")
        pusher.upload_blob(layer["digest"], blob(layer["digest"]))
    pusher.upload_blob(manifest["config"]["digest"], blob(manifest["config"]["digest"]))
    pusher.put_manifest(tag, manifest_bytes, manifest.get("mediaType", MANIFEST_TYPE))
    log(f"pushed {registry}/{repo}:{tag} ({desc['digest']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--rootfs", required=True)
    b.add_argument("--meta", required=True)
    b.add_argument("--name", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--arch", default=os.uname().machine)
    p = sub.add_parser("push")
    p.add_argument("layout")
    p.add_argument("ref")
    args = ap.parse_args()
    if args.cmd == "build":
        os.makedirs(args.out, exist_ok=True)
        build(args.rootfs, args.meta, args.name, args.out, args.arch)
    else:
        push(args.layout, args.ref)


if __name__ == "__main__":
    main()
