#!/usr/bin/env python3
"""Pull an OCI/Docker image and flatten it into a rootfs tarball for fcvm.

Standard library only; needs no root and no container runtime. Speaks the
registry v2 API (Docker Hub, ghcr.io, quay.io, ...) with anonymous bearer
tokens, or REGISTRY_USER / REGISTRY_PASSWORD from the environment.

Layers are applied with OCI whiteout semantics and written parent-first, the
order `mkfs.ext4 -d <tar>` needs. The image config becomes /.fcvm/{argv,env,
workdir,user,hostname}, read at boot by fc-init (which itself comes from the
initramfs, not the image).

  oci_import.py REF --out rootfs.tar --meta image.json
"""
import argparse
import base64
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GOARCH = {"x86_64": "amd64", "aarch64": "arm64"}


def log(msg):
    print(f"\033[1;34m==>\033[0m {msg}", file=sys.stderr)


def die(msg):
    print(f"\033[1;31merror:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# Registry client
# --------------------------------------------------------------------------

def parse_ref(ref):
    """'nginx' -> ('registry-1.docker.io', 'library/nginx', 'latest')."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "registry-1.docker.io", ref
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
    tag = "latest"
    if ":" in repo.rsplit("/", 1)[-1]:
        repo, tag = repo.rsplit(":", 1)
    if registry == "registry-1.docker.io" and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, digest or tag


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Registry:
    def __init__(self, registry, repo):
        self.base = f"https://{registry}/v2/{repo}"
        self.repo = repo
        self.token = None
        self.opener = urllib.request.build_opener(NoRedirect)

    def _auth(self, challenge):
        scheme, _, params = challenge.partition(" ")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
        user, pw = os.environ.get("REGISTRY_USER"), os.environ.get("REGISTRY_PASSWORD")
        basic = None
        if user and pw:
            basic = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        if scheme.lower() == "basic":
            self.token = basic
            return
        query = {"service": fields.get("service", ""),
                 "scope": fields.get("scope", f"repository:{self.repo}:pull")}
        req = urllib.request.Request(fields["realm"] + "?" + urllib.parse.urlencode(query))
        if basic:
            req.add_header("Authorization", basic)
        with urllib.request.urlopen(req) as r:
            body = json.load(r)
        self.token = "Bearer " + (body.get("token") or body["access_token"])

    def get(self, path, accept=None):
        """GET a registry path, handling auth and blob redirects (to S3/CDN)."""
        url = self.base + path
        for _ in range(10):
            req = urllib.request.Request(url)
            if accept:
                req.add_header("Accept", accept)
            if self.token and url.startswith(self.base):
                req.add_header("Authorization", self.token)
            try:
                return self.opener.open(req)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not self.token and "WWW-Authenticate" in e.headers:
                    self._auth(e.headers["WWW-Authenticate"])
                elif e.code in (301, 302, 303, 307, 308):
                    url = urllib.parse.urljoin(url, e.headers["Location"])
                else:
                    detail = e.read().decode(errors="replace")[:300]
                    die(f"GET {url}: HTTP {e.code} {detail}")
        die(f"too many redirects for {path}")

    def manifest(self, ref):
        with self.get(f"/manifests/{ref}", MANIFEST_TYPES) as r:
            return json.load(r), r.headers.get("Docker-Content-Digest", "")

    def blob(self, digest, cache_dir):
        """Download a blob into the content-addressed cache; returns its path."""
        algo, hexd = digest.split(":", 1)
        path = os.path.join(cache_dir, algo, hexd)
        if os.path.exists(path):
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = hashlib.new(algo)
        tmp = path + ".part"
        with self.get(f"/blobs/{digest}") as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
                done += len(chunk)
                if total and sys.stderr.isatty():
                    print(f"\r    {digest[7:19]}  {done >> 20}/{total >> 20} MiB", end="", file=sys.stderr)
        if total and sys.stderr.isatty():
            print(file=sys.stderr)
        if h.hexdigest() != hexd:
            os.unlink(tmp)
            die(f"digest mismatch for {digest}")
        os.rename(tmp, path)
        return path


def resolve_image(reg, ref, arch):
    manifest, digest = reg.manifest(ref)
    if manifest.get("mediaType") in INDEX_TYPES or "manifests" in manifest:
        want = GOARCH.get(arch, arch)
        for m in manifest["manifests"]:
            p = m.get("platform", {})
            if p.get("os") == "linux" and p.get("architecture") == want:
                manifest, _ = reg.manifest(m["digest"])
                digest = m["digest"]
                break
        else:
            die(f"no linux/{want} image in {ref}")
    return manifest, digest


# --------------------------------------------------------------------------
# Layer flattening
# --------------------------------------------------------------------------

def open_layer(path):
    """Open a (possibly gzip/zstd-compressed) layer as a streaming tarfile."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == b"\x28\xb5\x2f\xfd":
        try:
            from compression import zstd
            return tarfile.open(fileobj=zstd.open(path, "rb"), mode="r|")
        except ImportError:
            proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
            return tarfile.open(fileobj=proc.stdout, mode="r|")
    return tarfile.open(path, mode="r|*")


def norm(name):
    name = posixpath.normpath("/" + name).lstrip("/")
    return "" if name == "." else name


def ancestors(path):
    parts = path.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def flatten(layers):
    """Pick the winning entry for every path, top layer first.

    Returns {path: (layer_index, TarInfo)}. A lower-layer entry is dropped if an
    upper layer whited it (or an ancestor) out, marked an ancestor opaque, or
    replaced an ancestor directory with a non-directory.
    """
    winners = {}
    whiteouts, opaques = set(), set()
    for idx in reversed(range(len(layers))):
        layer_wh, layer_opq = set(), set()
        with open_layer(layers[idx]) as tf:
            for m in tf:
                name = norm(m.name)
                if not name:
                    continue
                d, base = posixpath.split(name)
                if base == ".wh..wh..opq":
                    layer_opq.add(d)
                    continue
                if base.startswith(".wh."):
                    layer_wh.add(posixpath.join(d, base[4:]))
                    continue
                if name in winners or name in whiteouts:
                    continue
                hidden = False
                for a in ancestors(name):
                    w = winners.get(a)
                    if a in whiteouts or a in opaques or (w and not w[1].isdir()):
                        hidden = True
                        break
                if not hidden:
                    m.name = name
                    if m.islnk():
                        m.linkname = norm(m.linkname)
                    winners[name] = (idx, m)
        whiteouts |= layer_wh
        opaques |= layer_opq
    return winners


def dir_info(name, mode=0o755):
    ti = tarfile.TarInfo(name)
    ti.type, ti.mode, ti.uid, ti.gid, ti.mtime = tarfile.DIRTYPE, mode, 0, 0, int(time.time())
    return ti


def file_info(name, data, mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = len(data), mode, 0, 0, int(time.time())
    return ti


# --------------------------------------------------------------------------
# Image config -> fc-init config
# --------------------------------------------------------------------------

def parse_passwd(text):
    users = {}
    for line in text.splitlines():
        f = line.split(":")
        if len(f) >= 4:
            users[f[0]] = (f[2], f[3])
    return users


def parse_group(text):
    groups = {}
    for line in text.splitlines():
        f = line.split(":")
        if len(f) >= 4:
            groups[f[0]] = (f[2], [u for u in f[3].split(",") if u])
    return groups


def resolve_user(spec, passwd, group):
    """Docker USER ('', name, uid, name:group, uid:gid) -> 'uid:gid:supp,...'."""
    if not spec:
        spec = "root"
    u, _, g = spec.partition(":")
    users, groups = parse_passwd(passwd), parse_group(group)
    if u.isdigit():
        uid = u
        name = next((n for n, (i, _) in users.items() if i == u), None)
        gid = users[name][1] if name else "0"
    elif u in users:
        name, (uid, gid) = u, users[u]
    else:
        die(f"image USER '{u}' not found in /etc/passwd")
    if g:
        gid = g if g.isdigit() else groups.get(g, (None,))[0]
        if gid is None:
            die(f"image group '{g}' not found in /etc/group")
    supp = sorted({gi for n, (gi, members) in groups.items() if name and name in members} - {gid})
    return f"{uid}:{gid}" + (":" + ",".join(supp) if supp else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref")
    ap.add_argument("--out", required=True, help="output rootfs tar")
    ap.add_argument("--meta", required=True, help="output image metadata json")
    ap.add_argument("--hostname", default="fcvm")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/fcvm/blobs"))
    ap.add_argument("--arch", default=os.uname().machine)
    args = ap.parse_args()

    registry, repo, ref = parse_ref(args.ref)
    log(f"resolving {registry}/{repo}:{ref}")
    reg = Registry(registry, repo)
    manifest, digest = resolve_image(reg, ref, args.arch)
    with open(reg.blob(manifest["config"]["digest"], args.cache)) as f:
        image_config = json.load(f)
    cfg = image_config.get("config") or {}

    layers = []
    for i, layer in enumerate(manifest["layers"], 1):
        log(f"layer {i}/{len(manifest['layers'])} {layer['digest'][:19]} ({layer.get('size', 0) >> 20} MiB)")
        layers.append(reg.blob(layer["digest"], args.cache))

    log("flattening layers")
    winners = flatten(layers)

    # Paths fc-init owns or rewrites at boot; the image's versions are dropped.
    injected = {".fcvm", "etc/hosts", "etc/hostname", "etc/resolv.conf"}
    for name in list(winners):
        if name in injected or name.startswith(".fcvm/"):
            del winners[name]

    # Mount points and dirs every rootfs needs, even FROM scratch images.
    dirs = {n: ti for n, (_, ti) in winners.items() if ti.isdir()}
    for d, mode in [("etc", 0o755), ("proc", 0o555), ("sys", 0o555), ("dev", 0o755),
                    ("tmp", 0o1777), ("run", 0o755), ("root", 0o700), (".fcvm", 0o755)]:
        if d not in winners:
            dirs[d] = dir_info(d, mode)
    for name, (_, ti) in winners.items():   # parents missing from the layers
        for a in ancestors(name):
            if a not in dirs:
                w = winners.get(a)
                if w and not w[1].isdir():
                    break
                dirs[a] = dir_info(a)

    # Grab passwd/group while writing, for USER resolution.
    wanted = {"etc/passwd": "", "etc/group": ""}
    content_bytes = 0
    with tarfile.open(args.out, "w", format=tarfile.PAX_FORMAT) as out:
        for name in sorted(dirs):
            out.addfile(dirs[name])
        hardlinks = []
        for idx, path in enumerate(layers):
            with open_layer(path) as tf:
                for m in tf:
                    name = norm(m.name)
                    w = winners.get(name)
                    if not w or w[0] != idx or w[1].isdir():
                        continue
                    ti = w[1]
                    if ti.islnk():
                        hardlinks.append(ti)
                        continue
                    if ti.isreg():
                        data = tf.extractfile(m).read() if name in wanted else None
                        if data is not None:
                            wanted[name] = data.decode(errors="replace")
                            out.addfile(ti, io.BytesIO(data))
                        else:
                            out.addfile(ti, tf.extractfile(m))
                        content_bytes += ti.size
                    else:
                        out.addfile(ti)
        for ti in hardlinks:
            target = winners.get(ti.linkname)
            if target and target[1].isreg():
                out.addfile(ti)
            else:
                print(f"    skipping hardlink {ti.name} -> {ti.linkname} (target gone)", file=sys.stderr)

        # Container config for fc-init.
        entrypoint = cfg.get("Entrypoint") or []
        cmd = cfg.get("Cmd") or []
        argv = entrypoint + cmd or ["/bin/sh"]
        env = cfg.get("Env") or []
        if not any(e.startswith("PATH=") for e in env):
            env = [f"PATH={DEFAULT_PATH}"] + env
        user = resolve_user(cfg.get("User", ""), wanted["etc/passwd"], wanted["etc/group"])
        files = {
            ".fcvm/argv": (b"\0".join(a.encode() for a in argv) + b"\0", 0o644),
            ".fcvm/env": (b"\0".join(e.encode() for e in env) + b"\0", 0o644),
            ".fcvm/workdir": ((cfg.get("WorkingDir") or "/").encode(), 0o644),
            ".fcvm/user": (user.encode(), 0o644),
            ".fcvm/hostname": (args.hostname.encode(), 0o644),
        }
        for name, (data, mode) in files.items():
            out.addfile(file_info(name, data, mode), io.BytesIO(data))

    meta = {
        "ref": args.ref,
        "resolved": f"{registry}/{repo}",
        "digest": digest,
        "argv": argv,
        "env": env,
        "workdir": cfg.get("WorkingDir") or "/",
        "user": cfg.get("User") or "root",
        "uid_gid": user,
        "exposed_ports": sorted((cfg.get("ExposedPorts") or {}).keys()),
        "entries": len(winners.keys() | dirs.keys()) + len(files),
        "content_bytes": content_bytes,
    }
    with open(args.meta, "w") as f:
        json.dump(meta, f, indent=2)
    log(f"rootfs: {meta['entries']} entries, {content_bytes >> 20} MiB; runs {' '.join(argv)} as {user}")


if __name__ == "__main__":
    main()
