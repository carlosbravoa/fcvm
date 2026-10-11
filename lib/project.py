#!/usr/bin/env python3
"""Project files: the VMs a directory needs, in fcvm.toml, brought up and
down together (`fcvm up`, `fcvm down`), like docker compose for one host.

  project.py up [-f FILE] [VM...]      create what's missing, start what's stopped
  project.py down [-f FILE] [VM...]    stop and remove them (volumes are kept)
  project.py show [-f FILE]            what the file means, as JSON

  # fcvm.toml
  name = "shop"                        # optional; default: the directory's name

  [vms.web]                            # the VM "shop-web"
  image = "nginx-latest"               # an fcvm image, or ref = "nginx:latest" to import it
  ports = ["8080:80"]
  volumes = ["site:/usr/share/nginx/html", "./site:/srv:ro"]   # host paths relative to the file
  restart = "unless-stopped"

  [vms.box]
  template = "python-sandbox"          # start from a template; the fields below override it
  volumes = ["./:/work"]
  ttl = "4h"

A VM takes the fields of a template (see lib/templates.py: image, ref, vcpus,
mem_mib, disk, copy, network, allow, ports, volumes, process, command,
entrypoint, jail, restart, ttl, idle_stop, secrets, forward), plus
`template` to start from one, and `idle = true` as a shorthand for
process = "idle". Standard library only (tomllib).
"""
import json
import os
import subprocess
import sys

try:
    import tomllib
except ImportError:                      # Python < 3.11
    tomllib = None

import templates

FCVM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fcvm")
FILES = ("fcvm.toml",)
PATH_PREFIXES = ("./", "../", "~", "/")


class Invalid(Exception):
    pass


def find_file(given=None):
    if given:
        if not os.path.isfile(given):
            raise Invalid(f"no such file: {given}")
        return os.path.abspath(given)
    d = os.getcwd()
    while True:
        for f in FILES:
            p = os.path.join(d, f)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            raise Invalid("no fcvm.toml here or above (fcvm up -f FILE names one)")
        d = parent


def resolve_volume(spec, base):
    """A host path in a volume spec, made absolute against the file's directory."""
    if not spec.startswith(PATH_PREFIXES[:2]):
        return spec
    host, sep, rest = spec.partition(":")
    return os.path.normpath(os.path.join(base, host)) + sep + rest


def load(path):
    """-> (project name, {vm name: template dict}) from a project file."""
    if tomllib is None:
        raise Invalid("project files need Python 3.11 or newer (tomllib)")
    with open(path, "rb") as f:
        try:
            doc = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise Invalid(f"{path}: {e}")
    base = os.path.dirname(path)
    name = doc.get("name") or os.path.basename(base) or "project"
    if not templates.NAME.match(name):
        raise Invalid(f"{path}: bad project name {name!r}")
    unknown = set(doc) - {"name", "vms"}
    if unknown:
        raise Invalid(f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))} (expected name and [vms.*])")
    vms = doc.get("vms")
    if not isinstance(vms, dict) or not vms:
        raise Invalid(f"{path}: no VMs; define them as [vms.NAME] tables")
    out = {}
    for key, spec in vms.items():
        if not templates.NAME.match(key) or not isinstance(spec, dict):
            raise Invalid(f"{path}: [vms.{key}] must be a table with a valid name")
        spec = dict(spec)
        t = {}
        if "template" in spec:
            try:
                t = {k: v for k, v in templates.get(spec.pop("template")).items() if k not in ("name", "builtin", "description")}
            except templates.Invalid as e:
                raise Invalid(f"{path}: [vms.{key}]: {e}")
        if spec.pop("idle", False):
            spec["process"] = "idle"
        if "command" in spec and spec.get("process", t.get("process")) not in ("command", None):
            spec["process"] = "command"
        if "command" in spec and "process" not in spec:
            spec["process"] = "command"
        for k in ("ports", "volumes", "allow", "secrets", "forward"):   # lists add to the template's
            if k in spec and k in t:
                spec[k] = t[k] + [x for x in spec[k] if x not in t[k]]
        merged = {**t, **spec}
        if "volumes" in merged:
            merged["volumes"] = [resolve_volume(v, base) for v in merged["volumes"]]
        try:
            out[f"{name}-{key}"] = templates.validate(merged)
        except templates.Invalid as e:
            raise Invalid(f"{path}: [vms.{key}]: {e}")
    return name, out


def create_args(vm, t):
    return ["create", vm, t["image"], *templates.options(t), *templates.process(t)]


def fcvm(*args, check=True):
    p = subprocess.run([FCVM, *args], stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        sys.exit(p.returncode)
    return p.returncode


def vms_now():
    r = subprocess.run([FCVM, "ls", "--json"], capture_output=True, text=True)
    return {v["name"]: v for v in json.loads(r.stdout or "[]")}


def images_now():
    r = subprocess.run([FCVM, "images", "--json"], capture_output=True, text=True)
    return {i["name"] for i in json.loads(r.stdout or "[]")}


def log(msg):
    print(f"\033[1;34m==>\033[0m {msg}", file=sys.stderr, flush=True)


def up(path, only):
    name, vms = load(path)
    chosen = pick(name, vms, only)
    have, images = vms_now(), images_now()
    for vm, t in chosen.items():
        if t["image"] not in images:
            if not t["ref"]:
                raise Invalid(f"{vm}: image '{t['image']}' isn't imported, and the file gives no ref to import it from")
            log(f"{vm}: importing {t['ref']} as {t['image']}")
            fcvm("import", t["ref"], t["image"])
            images.add(t["image"])
        if vm not in have:
            log(f"{vm}: creating")
            fcvm(*create_args(vm, t))
        elif have[vm]["state"] == "running":
            log(f"{vm}: already running")
            continue
        else:
            log(f"{vm}: exists (fcvm down, then up, applies a changed file)")
        fcvm("start", vm)
    log(f"{name}: {len(chosen)} VM(s) up")


def down(path, only):
    name, vms = load(path)
    chosen = pick(name, vms, only)
    have = vms_now()
    for vm in chosen:
        if vm not in have:
            continue
        if have[vm]["state"] == "running":
            fcvm("stop", vm)
        fcvm("rm", vm)
    log(f"{name}: down (volumes are kept)")


def pick(name, vms, only):
    if not only:
        return vms
    chosen = {}
    for o in only:
        full = o if o in vms else f"{name}-{o}"
        if full not in vms:
            raise Invalid(f"no VM '{o}' in the project (it has: {', '.join(k[len(name) + 1:] for k in vms)})")
        chosen[full] = vms[full]
    return chosen


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    file, only = None, []
    rest = args[1:]
    while rest:
        a = rest.pop(0)
        if a in ("-f", "--file"):
            if not rest:
                sys.exit("\033[1;31merror:\033[0m -f wants a file")
            file = rest.pop(0)
        elif a.startswith("-"):
            sys.exit(f"\033[1;31merror:\033[0m unknown option {a}")
        else:
            only.append(a)
    try:
        path = find_file(file)
        if cmd == "up":
            up(path, only)
        elif cmd == "down":
            down(path, only)
        elif cmd == "show":
            name, vms = load(path)
            print(json.dumps({"name": name, "file": path, "vms": {vm: {**t, "create": create_args(vm, t)} for vm, t in vms.items()}}, indent=2))
        else:
            sys.exit("usage: fcvm up [-f FILE] [VM...] | fcvm down [-f FILE] [VM...] | fcvm project show [-f FILE]")
    except Invalid as e:
        sys.exit(f"\033[1;31merror:\033[0m {e}")


if __name__ == "__main__":
    main()
