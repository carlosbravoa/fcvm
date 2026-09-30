#!/usr/bin/env python3
"""Launch templates: saved recipes for `fcvm create`, shared by the CLI, the
web console and the MCP server.

A template is JSON in STATE/templates/NAME.json (or a built-in one, in
lib/builtin-templates/, which a user template of the same name replaces):

  {"description": "...", "image": "python-3.13-slim", "ref": "python:3.13-slim",
   "vcpus": 2, "mem_mib": 2048, "disk": null, "copy": false,
   "network": "full" | "none" | "restricted", "allow": ["@pypi"],
   "ports": ["8080:80"], "volumes": ["cache:/root/.cache", "~/src"],
   "process": "image" | "idle" | "command", "command": ["python", "app.py"],
   "entrypoint": null, "jail": true | false | null, "restart": "no"}

`ref`, when set, is imported if the image is missing. `jail: null` means the
default (JAIL for the CLI, jailed for MCP when the helper exists).

  templates.py ls [--json] | show NAME | rm NAME | save NAME   (JSON on stdin)
  templates.py from-vm NAME VM [DESCRIPTION]
  templates.py options NAME | process NAME   (create arguments, NUL-separated)
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("FCVM_HOME") or ROOT
USER_DIR = os.path.join(STATE, "templates")
BUILTIN_DIR = os.path.join(ROOT, "lib", "builtin-templates")
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
POLICIES = ("no", "on-failure", "unless-stopped", "always")
FIELDS = {"description": "", "image": None, "ref": None, "vcpus": None, "mem_mib": None, "disk": None,
          "copy": False, "network": "full", "allow": [], "ports": [], "volumes": [], "process": "image",
          "command": [], "entrypoint": None, "jail": None, "restart": "no"}


class Invalid(Exception):
    pass


def validate(t):
    """A complete, checked template from a partial one."""
    if not isinstance(t, dict):
        raise Invalid("a template is a JSON object")
    unknown = set(t) - set(FIELDS) - {"name", "builtin"}
    if unknown:
        raise Invalid(f"unknown field(s): {', '.join(sorted(unknown))}")
    out = {**FIELDS, **{k: v for k, v in t.items() if k in FIELDS}}
    if not isinstance(out["image"], str) or not NAME.match(out["image"]):
        raise Invalid("image is required (an fcvm image name)")
    for k in ("vcpus", "mem_mib"):
        if out[k] is not None and (not isinstance(out[k], int) or out[k] < 1):
            raise Invalid(f"{k} must be a positive number")
    if out["network"] not in ("full", "none", "restricted"):
        raise Invalid("network must be full, none or restricted")
    if out["network"] == "restricted" and not out["allow"]:
        raise Invalid("a restricted network needs an allowlist")
    for k in ("allow", "ports", "volumes", "command"):
        if not isinstance(out[k], list) or not all(isinstance(x, str) for x in out[k]):
            raise Invalid(f"{k} must be a list of strings")
    if out["process"] not in ("image", "idle", "command"):
        raise Invalid("process must be image, idle or command")
    if out["process"] == "command" and not out["command"]:
        raise Invalid("process 'command' needs a command")
    if out["restart"] not in POLICIES:
        raise Invalid(f"restart must be one of {', '.join(POLICIES)}")
    if out["jail"] not in (True, False, None):
        raise Invalid("jail must be true, false or null (the default)")
    return out


def _read(path):
    with open(path) as f:
        return json.load(f)


def all_templates():
    """name -> template, user templates over built-in ones."""
    out = {}
    for d, builtin in ((BUILTIN_DIR, True), (USER_DIR, False)):
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.endswith(".json") and NAME.match(f[:-5]):
                try:
                    out[f[:-5]] = {**validate(_read(os.path.join(d, f))), "name": f[:-5], "builtin": builtin}
                except (OSError, ValueError, Invalid):
                    continue
    return out


def get(name):
    t = all_templates().get(name)
    if not t:
        raise Invalid(f"no template '{name}' (see: fcvm template ls)")
    return t


def save(name, t):
    if not NAME.match(name or "-"):
        raise Invalid(f"invalid template name '{name}'")
    t = validate(t)
    os.makedirs(USER_DIR, exist_ok=True)
    path = os.path.join(USER_DIR, f"{name}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(t, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)
    return {**t, "name": name, "builtin": False}


def remove(name):
    path = os.path.join(USER_DIR, f"{name}.json")
    if not os.path.exists(path):
        if name in all_templates():
            raise Invalid(f"'{name}' is built in (a template you save under that name replaces it)")
        raise Invalid(f"no template '{name}'")
    os.unlink(path)


def options(t):
    """`fcvm create` options for everything but the process."""
    a = []
    if t["vcpus"]:
        a += ["--vcpus", str(t["vcpus"])]
    if t["mem_mib"]:
        a += ["--mem", str(t["mem_mib"])]
    if t["disk"]:
        a += ["--disk", str(t["disk"])]
    if t["copy"]:
        a.append("--copy")
    for p in t["ports"]:
        a += ["-p", p]
    for v in t["volumes"]:
        a += ["-v", v]
    if t["network"] == "none":
        a += ["--net", "none"]
    elif t["network"] == "restricted":
        a += ["--allow", ",".join(t["allow"])]
    if t["jail"] is not None:
        a.append("--jail" if t["jail"] else "--no-jail")
    if t["restart"] != "no":
        a += ["--restart", t["restart"]]
    return a


def process(t):
    """`fcvm create` arguments for the process (app images)."""
    a = ["--entrypoint", t["entrypoint"]] if t["entrypoint"] is not None else []
    if t["process"] == "idle":
        return a + ["--idle"]
    if t["process"] == "command":
        return a + ["--", *t["command"]]
    return a


def from_vm(vm, description=""):
    """A template from an existing VM's configuration."""
    vms = os.path.join(STATE, "vms", vm)
    try:
        c = _read(os.path.join(vms, "vm.json"))
    except OSError:
        raise Invalid(f"no VM '{vm}'")
    img = {}
    try:
        img = _read(os.path.join(STATE, "images", f"{c['image']}.json"))
    except OSError:
        pass
    t = {"description": description, "image": c["image"], "ref": img.get("ref"), "vcpus": c.get("vcpus"),
         "mem_mib": c.get("mem_mib"), "ports": c.get("ports") or [],
         "volumes": (c.get("volumes") or []) + [f"{s['host']}:{s['path']}" + (":ro" if s.get("ro") else "")
                                                 for s in c.get("shares") or []],
         "network": (c.get("net") or {}).get("mode") or "full", "allow": (c.get("net") or {}).get("allow") or [],
         "jail": bool(c.get("jail")), "restart": c.get("restart") or "no"}
    # The command lives on the VM's writable layer (/.fcvm/argv, NUL-separated).
    disk = next((os.path.join(vms, d) for d in ("rw.ext4", "disk.ext4") if os.path.exists(os.path.join(vms, d))), None)
    if disk and c.get("type") == "app":
        where = "/upper/.fcvm/argv" if disk.endswith("rw.ext4") else "/.fcvm/argv"
        raw = subprocess.run(["debugfs", "-R", f"cat {where}", disk], capture_output=True).stdout
        argv = [a.decode() for a in raw.split(b"\0") if a]
        if argv == ["/.fcvm/bin/fc-init", "--idle"]:
            t["process"] = "idle"
        elif argv:
            t["process"], t["command"] = "command", argv
    return validate(t)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "ls"
    try:
        if cmd == "ls":
            ts = all_templates()
            if "--json" in args:
                print(json.dumps(list(ts.values()), indent=2))
                return
            print(f"{'NAME':<22} {'IMAGE':<22} {'NETWORK':<24} {'PROCESS':<9} DESCRIPTION")
            for n, t in ts.items():
                net = t["network"] if t["network"] != "restricted" else "allow:" + ",".join(t["allow"])
                print(f"{n + (' *' if t['builtin'] else ''):<22} {t['image']:<22} {net[:24]:<24} {t['process']:<9} "
                      f"{t['description']}")
            if any(t["builtin"] for t in ts.values()):
                print("\n* built in (save a template under that name to replace it)")
        elif cmd == "show":
            print(json.dumps(get(args[1]), indent=2))
        elif cmd == "rm":
            remove(args[1])
        elif cmd == "save":
            print(json.dumps(save(args[1], json.load(sys.stdin))))
        elif cmd == "from-vm":
            print(json.dumps(from_vm(args[2], args[3] if len(args) > 3 else "")))
        elif cmd in ("options", "process"):
            t = get(args[1])
            out = options(t) if cmd == "options" else process(t)
            sys.stdout.write("".join(a + "\0" for a in out))
        elif cmd == "image":
            t = get(args[1])
            print(f"{t['image']}\t{t['ref'] or ''}")
        else:
            raise Invalid(f"unknown command {cmd}")
    except (Invalid, IndexError, ValueError) as e:
        sys.exit(f"\033[1;31merror:\033[0m {e if not isinstance(e, IndexError) else 'missing argument'}")


if __name__ == "__main__":
    main()
