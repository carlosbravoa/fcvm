#!/usr/bin/env python3
"""fcvm build: build an fcvm image from a Dockerfile subset.

  fcvm build [-f FILE] [-t NAME] [--build-arg K=V]... [--no-cache]
             [--net none|full] [--allow HOSTS]... [CONTEXT]

Supported: FROM (one stage; an fcvm image name or a registry ref, imported if
missing), RUN, COPY/ADD (local sources; ADD extracts local tar archives;
--chown), ENV, ARG, WORKDIR, USER, CMD, ENTRYPOINT, EXPOSE, LABEL (ignored).
Anything else (multi-stage builds, COPY --from, ADD URLs, HEALTHCHECK, ...)
fails with a clear error instead of being silently ignored.

How it runs: every filesystem step (RUN, COPY, ADD, WORKDIR) runs in a VM
booted from the previous step's result, which is then stopped and committed as
a cache image (_bc-<key>). The key chains the previous key, the instruction
and, for COPY/ADD, the content of the copied files, so an unchanged prefix of
the file is skipped on the next build. Cache chains are squashed when they get
deep. The final image is the FROM image plus exactly one squashed layer, with
the resulting ENV/WORKDIR/USER/CMD/ENTRYPOINT written to its /.fcvm config.
--no-cache runs all steps in one VM and commits once.
"""
import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the code
FCVM = os.path.join(ROOT, "fcvm")
IMAGES = os.path.join(os.environ.get("FCVM_HOME") or ROOT, "images")   # the state
MAX_CACHE_DEPTH = 6
FS_STEPS = {"RUN", "COPY", "ADD", "WORKDIR"}
META_STEPS = {"ENV", "ARG", "USER", "CMD", "ENTRYPOINT", "EXPOSE", "LABEL", "MAINTAINER"}


def log(msg):
    print(f"\033[1;34m==>\033[0m {msg}", file=sys.stderr, flush=True)


def die(msg):
    print(f"\033[1;31merror:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def fcvm(*args, check=True, quiet=True, **kw):
    p = subprocess.run([FCVM, *map(str, args)], text=True,
                       stdout=subprocess.PIPE if quiet else None, stderr=subprocess.PIPE if quiet else None, **kw)
    if check and p.returncode != 0:
        die(f"fcvm {' '.join(map(str, args))} failed:\n{(p.stderr or '').strip()}")
    return p


def image_json(name):
    path = os.path.join(IMAGES, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def chain_depth_above_base(name):
    depth = 0
    while (meta := image_json(name)) and meta.get("parent"):
        depth += 1
        name = meta["parent"]
    return depth


# --- Dockerfile parsing ------------------------------------------------------------

def parse(text):
    """-> [(keyword, args, original line)] with continuations joined."""
    out, buf = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):          # comment inside a continuation
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        kw, _, rest = buf.partition(" ")
        out.append((kw.upper(), rest.strip(), buf))
        buf = ""
    if buf:
        kw, _, rest = buf.partition(" ")
        out.append((kw.upper(), rest.strip(), buf))
    return out


VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::([-+])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


def substitute(s, scope):
    def repl(m):
        name = m.group(1) or m.group(4)
        val = scope.get(name)
        if m.group(2) == "-":
            return val if val else m.group(3)
        if m.group(2) == "+":
            return m.group(3) if val else ""
        return val or ""
    return VAR.sub(repl, s)


def exec_form(args):
    """JSON array form -> list, else None."""
    if args.startswith("["):
        try:
            v = json.loads(args)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return v
        except ValueError:
            pass
        die(f"invalid JSON array: {args}")
    return None


def parse_kv(args, scope, keyword):
    """ENV/ARG/LABEL: 'K=V K2="v 2"' or legacy 'K V'."""
    words = shlex.split(args, posix=True)
    if keyword != "ARG" and len(words) >= 2 and "=" not in words[0]:
        return {words[0]: substitute(" ".join(words[1:]), scope)}
    out = {}
    for w in words:
        k, sep, v = w.partition("=")
        out[k] = substitute(v, scope) if sep else None
    return out


# --- build context -----------------------------------------------------------------

def load_ignore(context):
    path = os.path.join(context, ".dockerignore")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [l.strip().rstrip("/") for l in f if l.strip() and not l.startswith("#")]


def ignored(rel, patterns):
    hit = False
    for p in patterns:
        neg = p.startswith("!")
        pat = p[1:] if neg else p
        pat = pat.lstrip("/")
        if fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/") or fnmatch.fnmatch(rel, pat.replace("**/", "")):
            hit = not neg
    return hit


def gather(context, patterns, srcs):
    """COPY sources -> [(host path, relative arc path, is_dir_source)]"""
    import glob
    out = []
    for src in srcs:
        matches = sorted(glob.glob(os.path.join(context, src))) if any(c in src for c in "*?[") else [os.path.join(context, src)]
        if not matches or not all(os.path.exists(m) for m in matches):
            die(f"COPY source not found in the build context: {src}")
        for m in matches:
            real = os.path.realpath(m)
            if not (real + "/").startswith(os.path.realpath(context) + "/") and real != os.path.realpath(context):
                die(f"COPY source outside the build context: {src}")
            out.append((m, os.path.isdir(m)))
    return out


def tar_sources(context, patterns, sources, dst_is_dir, dst_name):
    """Tar stream (root-owned) of COPY sources, as they should land in the target dir."""
    buf = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        def add(path, arc):
            rel = os.path.relpath(path, context)
            if rel != "." and ignored(rel, patterns):
                return
            ti = tf.gettarinfo(path, arcname=arc)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0  # stable cache keys
            digest.update(f"{arc}\0{ti.mode}\0{ti.size}\0{ti.linkname}\0".encode())
            if ti.isreg():
                with open(path, "rb") as f:
                    data = f.read()
                digest.update(hashlib.sha256(data).digest())
                tf.addfile(ti, io.BytesIO(data))
            else:
                tf.addfile(ti)
            if ti.isdir():
                for entry in sorted(os.listdir(path)):
                    add(os.path.join(path, entry), f"{arc}/{entry}" if arc != "." else entry)
        for path, is_dir in sources:
            if is_dir:              # a directory's *contents* go into the destination
                for entry in sorted(os.listdir(path)):
                    add(os.path.join(path, entry), entry)
            else:
                add(path, os.path.basename(path) if dst_is_dir else dst_name)
    return buf.getvalue(), digest.hexdigest()


# --- the build ----------------------------------------------------------------------

def make_empty_layer(path):
    """An empty layer disk (ext4 holding upper/ and work/), like vm.sh make_rw."""
    with tempfile.TemporaryDirectory() as tmp:
        tar = os.path.join(tmp, "l.tar")
        with tarfile.open(tar, "w") as tf:
            for d in ("upper", "work"):
                ti = tarfile.TarInfo(d)
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(ti)
        subprocess.run(["truncate", "-s", "64M", path], check=True)
        subprocess.run(["mkfs.ext4", "-q", "-F", "-d", tar, path], check=True)


class Builder:
    def __init__(self, args):
        self.args = args
        self.context = os.path.abspath(args.context)
        self.vm = None
        self.vm_base = None
        self.cache_images = []

    def net_flags(self):
        f = []
        if self.args.net:
            f += ["--net", self.args.net]
        for a in self.args.allow:
            f += ["--allow", a]
        return f

    def ensure_vm(self, base):
        """A running build VM whose image is `base` (booted fresh after each commit)."""
        if self.vm and self.vm_base == base:
            return
        self.drop_vm()
        self.vm = f"_build-{os.getpid()}"
        idle = ["--idle"] if image_json(base).get("type", "app") == "app" else []   # system images stay up
        fcvm("create", self.vm, base, *idle, *self.net_flags())
        fcvm("start", self.vm)
        self.vm_base = base

    def drop_vm(self):
        if self.vm:
            fcvm("stop", self.vm, check=False)
            fcvm("rm", self.vm, check=False)
            self.vm = self.vm_base = None

    def run_in_vm(self, argv, env, workdir, user, stdin=None):
        cmd = ["exec"]
        if stdin is not None:
            cmd.append("-i")
        if user:
            cmd += ["-u", user]
        if workdir:
            cmd += ["-w", workdir]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.vm, *argv]
        p = subprocess.run([FCVM, *cmd], input=stdin)
        return p.returncode

    def snapshot(self, key, current):
        """Commit the build VM as cache image _bc-<key>, squashing deep chains."""
        name = f"_bc-{key[:20]}"
        fcvm("stop", self.vm)
        if chain_depth_above_base(current) + 1 > MAX_CACHE_DEPTH:
            tmp = f"_bct-{key[:20]}"
            fcvm("commit", self.vm, tmp)
            fcvm("squash", tmp, name)
            fcvm("rmi", tmp)
        else:
            fcvm("commit", self.vm, name)
        self.drop_vm()
        return name

    def build(self):
        a = self.args
        dockerfile = a.file or next((os.path.join(self.context, f) for f in ("Fcvmfile", "Dockerfile")
                                     if os.path.exists(os.path.join(self.context, f))), None)
        if not dockerfile or not os.path.exists(dockerfile):
            die(f"no Fcvmfile or Dockerfile in {self.context} (use -f)")
        with open(dockerfile) as f:
            steps = parse(f.read())
        patterns = load_ignore(self.context)
        build_args = dict(kv.split("=", 1) for kv in a.build_arg)

        # ARGs before FROM, then FROM.
        scope = {}
        i = 0
        while i < len(steps) and steps[i][0] == "ARG":
            for k, v in parse_kv(steps[i][1], scope, "ARG").items():
                scope[k] = build_args.get(k, v or "")
            i += 1
        if i >= len(steps) or steps[i][0] != "FROM":
            die("the file must start with FROM (after optional ARGs)")
        from_words = substitute(steps[i][1], scope).split()
        if len(from_words) not in (1, 3) or (len(from_words) == 3 and from_words[1].upper() != "AS"):
            die(f"unsupported FROM: {steps[i][2]}")
        if any(s[0] == "FROM" for s in steps[i + 1:]):
            die("multi-stage builds are not supported (only one FROM)")
        if from_words[0] == "scratch":
            die("FROM scratch is not supported (a VM needs a base filesystem)")
        base = self.resolve_from(from_words[0])
        base_meta = image_json(base)
        systemd = base_meta.get("type") == "system"
        steps = steps[i + 1:]

        cfg = {
            "env": dict(e.split("=", 1) for e in base_meta.get("env") or []),
            "workdir": base_meta.get("workdir") or "/",
            "user": base_meta.get("user") or "",
            "entrypoint": base_meta.get("entrypoint", []) or [],
            "cmd": base_meta.get("cmd", base_meta.get("argv", [])) or [],
            "exposed": list(base_meta.get("exposed_ports") or []),
        }
        if cfg["user"] == "root":
            cfg["user"] = ""
        args_scope = {}
        key = hashlib.sha256(f"FROM {base} {json.dumps(base_meta, sort_keys=True)}".encode()).hexdigest()
        current = base
        log(f"building '{a.tag}' from {base} ({len(steps)} steps)")

        try:
            for n, (kw, rest, line) in enumerate(steps, 1):
                scope = {**cfg["env"], **args_scope}
                label = f"[{n}/{len(steps)}] {line[:100]}"
                if kw not in FS_STEPS | META_STEPS:
                    die(f"{label}: '{kw}' is not supported by fcvm build")
                if kw in ("CMD", "ENTRYPOINT") and systemd:
                    die(f"{label}: {kw} has no effect on system images (systemd is the init)")

                # --- metadata-only steps
                if kw in META_STEPS:
                    if kw == "ENV":
                        cfg["env"].update(parse_kv(rest, scope, kw))
                    elif kw == "ARG":
                        for k, v in parse_kv(rest, scope, kw).items():
                            args_scope[k] = build_args.get(k, v or "")
                    elif kw == "USER":
                        cfg["user"] = substitute(rest, scope)
                    elif kw in ("CMD", "ENTRYPOINT"):
                        v = exec_form(rest) or ["/bin/sh", "-c", rest]
                        if kw == "ENTRYPOINT":
                            cfg["entrypoint"], cfg["cmd"] = v, []   # docker: new ENTRYPOINT resets CMD
                        else:
                            cfg["cmd"] = v
                    elif kw == "EXPOSE":
                        for p in substitute(rest, scope).split():
                            p = p if "/" in p else f"{p}/tcp"
                            if p not in cfg["exposed"]:
                                cfg["exposed"].append(p)
                    key = hashlib.sha256(f"{key}\n{kw} {json.dumps(cfg, sort_keys=True)} {json.dumps(args_scope, sort_keys=True)}".encode()).hexdigest()
                    log(label)
                    continue

                # --- filesystem steps
                payload, extra = None, ""
                if kw == "WORKDIR":
                    wd = substitute(rest, scope)
                    cfg["workdir"] = os.path.normpath(os.path.join(cfg["workdir"], wd))
                elif kw in ("COPY", "ADD"):
                    words = shlex.split(substitute(rest, scope)) if not rest.startswith("[") else exec_form(rest)
                    chown = None
                    while words and words[0].startswith("--"):
                        flag = words.pop(0)
                        if flag.startswith("--chown="):
                            chown = flag.split("=", 1)[1]
                        elif flag.startswith("--from"):
                            die(f"{label}: COPY --from (multi-stage) is not supported")
                        else:
                            die(f"{label}: unsupported flag {flag}")
                    if len(words) < 2:
                        die(f"{label}: needs a source and a destination")
                    srcs, dst = words[:-1], words[-1]
                    if kw == "ADD" and any(re.match(r"^[a-z]+://", s) for s in srcs):
                        die(f"{label}: ADD from URLs is not supported; RUN curl/wget instead")
                    dst = dst if dst.startswith("/") else os.path.join(cfg["workdir"], dst)
                    sources = gather(self.context, patterns, srcs)
                    archives = [p for p, d in sources if kw == "ADD" and not d and tarfile.is_tarfile(p)]
                    dst_is_dir = dst.endswith("/") or len(sources) > 1 or any(d for _, d in sources) or bool(archives)
                    target_dir = (dst.rstrip("/") or "/") if dst_is_dir else os.path.dirname(dst)
                    if archives:
                        if len(sources) > 1:
                            die(f"{label}: ADD a tar archive on its own")
                        with open(archives[0], "rb") as f:
                            payload = f.read()
                        extra = hashlib.sha256(payload).hexdigest()
                    else:
                        payload, extra = tar_sources(self.context, patterns, sources, dst_is_dir, os.path.basename(dst))
                    copy = (target_dir, chown, dst, sources, dst_is_dir, bool(archives))
                else:  # RUN
                    run_argv = exec_form(rest) or ["/bin/sh", "-c", rest]

                key = hashlib.sha256(f"{key}\n{line}\n{extra}\n{json.dumps(cfg, sort_keys=True)} {json.dumps(args_scope, sort_keys=True)}".encode()).hexdigest()
                cached = f"_bc-{key[:20]}"
                if not a.no_cache and image_json(cached):
                    log(f"{label}  (cached)")
                    current = cached
                    continue
                log(label)
                self.ensure_vm(current)
                env = {**cfg["env"], **args_scope}
                user = cfg["user"] or None
                if kw == "WORKDIR":
                    rc = self.run_in_vm(["mkdir", "-p", cfg["workdir"]], {}, "/", "root")
                elif kw in ("COPY", "ADD"):
                    target_dir, chown, dst, sources, dst_is_dir, is_archive = copy
                    if not dst_is_dir and self.run_in_vm(["test", "-d", dst], {}, "/", "root") == 0:
                        # COPY file /existing-dir: into the directory, as docker does
                        target_dir = dst
                        payload, _ = tar_sources(self.context, patterns, sources, True, "")
                    rc = self.run_in_vm(["sh", "-c", 'mkdir -p "$1" && tar -xf - -C "$1"', "sh", target_dir],
                                        {}, "/", "root", stdin=payload)
                    if rc == 0 and chown:
                        names = sorted({m.split("/")[0] for m in tarfile.open(fileobj=io.BytesIO(payload)).getnames() if m not in (".", "")})
                        rc = self.run_in_vm(["chown", "-R", chown, *[os.path.join(target_dir, x) for x in names]],
                                            {}, "/", "root")
                else:
                    rc = self.run_in_vm(run_argv, env, cfg["workdir"], user)
                if rc != 0:
                    die(f"{label}: failed with exit code {rc}")
                if a.no_cache:
                    continue          # one VM for the whole build; commit at the end
                current = self.snapshot(key, current)

            # --- final image: FROM + one layer, with the resulting config
            temp = None
            if a.no_cache and self.vm:
                temp = f"_bc-nocache-{os.getpid()}"
                fcvm("stop", self.vm)
                fcvm("commit", self.vm, temp)
                self.drop_vm()
                current = temp
            self.finish(base, current, cfg)
            if temp:
                fcvm("rmi", temp, check=False)   # the final layer is a copy, not a child
        finally:
            self.drop_vm()

    def resolve_from(self, ref):
        if image_json(ref):
            return ref
        name = subprocess.run([sys.executable, os.path.join(ROOT, "lib", "oci_import.py"), "--suggest-name", ref],
                              capture_output=True, text=True).stdout.strip()
        if name and image_json(name):
            return name
        log(f"FROM {ref}: importing")
        fcvm("import", ref, name, quiet=False)
        return name

    def finish(self, base, current, cfg):
        tag = self.args.tag
        if image_json(tag):
            users = fcvm("images", "--json").stdout
            used = next((i["used_by"] for i in json.loads(users) if i["name"] == tag), [])
            if used:
                die(f"image '{tag}' exists and is used by {', '.join(used)}; pick another -t")
            fcvm("rmi", tag)
        depth = chain_depth_above_base(current)
        out = os.path.join(IMAGES, f"{tag}.ext4")
        if depth == 0:          # only metadata changed: an empty layer
            make_empty_layer(out)
        elif depth == 1:
            subprocess.run(["cp", "--sparse=always", os.path.join(IMAGES, f"{current}.ext4"), out], check=True)
        else:
            fcvm("squash", current, tag)
        os.chmod(out, 0o644)

        # The image's /.fcvm config, written into the layer's upper/ with debugfs.
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "argv": b"\0".join(x.encode() for x in (cfg["entrypoint"] + cfg["cmd"]) or ["/bin/sh"]) + b"\0",
                "env": b"\0".join(f"{k}={v}".encode() for k, v in cfg["env"].items()) + b"\0",
                "workdir": cfg["workdir"].encode(),
                "user": (cfg["user"] or "0:0").encode(),
            }
            cmds = ["mkdir /upper/.fcvm", "sif /upper/.fcvm mode 040755", "sif /upper/.fcvm uid 0", "sif /upper/.fcvm gid 0"]
            for fname, data in files.items():
                p = os.path.join(tmp, fname)
                with open(p, "wb") as f:
                    f.write(data)
                cmds += [f"rm /upper/.fcvm/{fname}", f"write {p} /upper/.fcvm/{fname}",
                         f"sif /upper/.fcvm/{fname} mode 0100644", f"sif /upper/.fcvm/{fname} uid 0",
                         f"sif /upper/.fcvm/{fname} gid 0"]
            subprocess.run(["debugfs", "-w", "-f", "-", out], input="\n".join(cmds) + "\n", text=True,
                           capture_output=True)
        os.chmod(out, 0o444)

        meta = {k: v for k, v in image_json(base).items() if k in ("type",)}
        meta.update({
            "parent": base, "ref": f"build of {os.path.relpath(self.context)}",
            "argv": cfg["entrypoint"] + cfg["cmd"], "entrypoint": cfg["entrypoint"], "cmd": cfg["cmd"],
            "env": [f"{k}={v}" for k, v in cfg["env"].items()], "workdir": cfg["workdir"],
            "user": cfg["user"] or "root", "exposed_ports": cfg["exposed"],
        })
        with open(os.path.join(IMAGES, f"{tag}.json"), "w") as f:
            json.dump(meta, f, indent=2)
        size = subprocess.run(["du", "-h", out], capture_output=True, text=True).stdout.split()[0]
        log(f"image '{tag}' ready: {base} + one layer ({size}). Try: fcvm run {tag}")


def main():
    ap = argparse.ArgumentParser(prog="fcvm build", description="Build an fcvm image from a Dockerfile subset.")
    ap.add_argument("context", nargs="?", default=".")
    ap.add_argument("-f", "--file")
    ap.add_argument("-t", "--tag")
    ap.add_argument("--build-arg", action="append", default=[])
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--net", choices=["full", "none"])
    ap.add_argument("--allow", action="append", default=[])
    args = ap.parse_args()
    if not os.path.isdir(args.context):
        die(f"build context {args.context} is not a directory")
    args.tag = args.tag or re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(os.path.abspath(args.context)))
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", args.tag):
        die(f"invalid image name '{args.tag}'")
    if args.net == "none" and args.allow:
        die("--net none and --allow are mutually exclusive")
    Builder(args).build()


if __name__ == "__main__":
    main()
