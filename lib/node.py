#!/usr/bin/env python3
"""The node view of this host (docs/node-contract.md): tenants and their
quotas, what the host can hold and what's allocated, and admission, the
check a VM passes before it takes resources.

  fcvm tenant set NAME [--vcpus N|none] [--mem MIB|none] [--vms N|none]
  fcvm tenant ls [--json] | show NAME | rm NAME
  fcvm status --json              lib/status.sh runs `node.py status`
  node.py admit [--claim --tenant T] VM
                                  lib/vm.sh, before a VM starts, is forked
                                  or is claimed for a tenant

A tenant is the `tenant` label of a VM (`--tenant NAME` sets it; it can't
be changed afterwards). vms/.tenants/NAME.json holds the tenant's quota:
{"name", "quota": {"vcpus", "mem_mib", "vms"}, ...}, each null for no
limit, written by `fcvm tenant set`. A tenant without a file has no quota.

Allocated means the vCPUs, memory and count of the VMs that run, plus
those admitted in the last two minutes that haven't recorded a pid yet
(the `starting` marker). The host's capacity comes from NODE_VCPUS,
NODE_MEM_MIB and NODE_VMS in fcvm.conf, or from the host's CPUs and
memory times NODE_CPU_OVERCOMMIT (2) and NODE_MEM_OVERCOMMIT (1) less
NODE_RESERVE_MIB (1024); it is enforced only with NODE_ADMIT=1 (tenant
quotas always are). `starting` and a VM's lock live under vms/.locks;
admission serializes on vms/.locks/admission. Standard library only.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("FCVM_HOME") or ROOT
VMS = os.path.join(STATE, "vms")
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
KEYS = ("vcpus", "mem_mib", "vms")
STARTING_FRESH = 120            # seconds an admitted VM counts as allocated before it has a pid


def die(code, msg):
    """Fail the way lib/common.sh's die_code does (docs/errors.md)."""
    sys.stderr.write(f"\033[1;31merror:\033[0m {msg}\n")
    if not sys.stderr.isatty():
        sys.stderr.write(f"fcvm-error: {code}\n")
    sys.exit(1)


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.rename(tmp, path)


class Lock:
    """flock on vms/.locks/NAME, for a `with` block."""

    def __init__(self, name):
        os.makedirs(os.path.join(VMS, ".locks"), exist_ok=True)
        self.path = os.path.join(VMS, ".locks", name)

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


# --- what runs --------------------------------------------------------------------------

def boot_id():
    return read("/proc/sys/kernel/random/boot_id").strip()


def proc_start(pid):
    stat = read(f"/proc/{pid}/stat")
    return stat[stat.rfind(")") + 2:].split()[19] if stat else ""


def running(d):
    """Mirrors vm_running in lib/vm.sh: the pid file names a live process
    that is the one recorded at start (pid.id), or a Firecracker."""
    pid = read(os.path.join(d, "pid")).strip()
    if not pid or not os.path.exists(f"/proc/{pid}"):
        return False
    ident = read(os.path.join(d, "pid.id")).split()
    if ident:
        return ident == [boot_id(), proc_start(pid)]
    return read(f"/proc/{pid}/comm").strip() == "firecracker"


def starting(d):
    """Admitted recently, no pid yet (start is between admission and the boot)."""
    try:
        return time.time() - os.stat(os.path.join(d, "starting")).st_mtime < STARTING_FRESH
    except OSError:
        return False


def vms():
    """Every VM with what it would take: name, vcpus, mem_mib, tenant, state
    (running, starting or stopped)."""
    out = []
    try:
        names = sorted(os.listdir(VMS))
    except OSError:
        return out
    for name in names:
        d = os.path.join(VMS, name)
        cfg = read_json(os.path.join(d, "vm.json")) if not name.startswith(".") else None
        if not isinstance(cfg, dict):
            continue
        state = "running" if running(d) else "starting" if starting(d) else "stopped"
        out.append({"name": name, "vcpus": int(cfg.get("vcpus") or 0), "mem_mib": int(cfg.get("mem_mib") or 0),
                    "tenant": (cfg.get("labels") or {}).get("tenant"), "state": state})
    return out


def allocated(all_vms, tenant=None, exclude=None):
    """The vCPUs, memory and count of the VMs that hold resources (running
    or starting), for one tenant when given, leaving one VM out."""
    sel = [v for v in all_vms if v["state"] != "stopped" and v["name"] != exclude
           and (tenant is None or v["tenant"] == tenant)]
    return {"vcpus": sum(v["vcpus"] for v in sel), "mem_mib": sum(v["mem_mib"] for v in sel), "vms": len(sel)}


# --- capacity ---------------------------------------------------------------------------

def meminfo(key):
    for line in read("/proc/meminfo").splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1]) // 1024
    return 0


def number(name, default):
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        die("invalid", f"{name} wants a whole number (fcvm.conf)")


def factor(name, default):
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        die("invalid", f"{name} wants a number (fcvm.conf)")


def capacity():
    """What the host may hold (docs/configuration.md, Node), with where it came from."""
    cpus, mem = os.cpu_count() or 1, meminfo("MemTotal")
    reserve = number("NODE_RESERVE_MIB", 1024)
    cap = {"vcpus": number("NODE_VCPUS", int(cpus * factor("NODE_CPU_OVERCOMMIT", 2))),
           "mem_mib": number("NODE_MEM_MIB", max(int((mem - reserve) * factor("NODE_MEM_OVERCOMMIT", 1)), 0)),
           "vms": number("NODE_VMS", None)}
    return cap, {"cpus": cpus, "mem_mib": mem}


def enforced():
    return os.environ.get("NODE_ADMIT", "").strip() == "1"


# --- tenants ----------------------------------------------------------------------------

def tenant_path(name):
    return os.path.join(VMS, ".tenants", f"{name}.json")


def tenant(name):
    """The tenant's record, or None when it has no quota file."""
    if not NAME.match(name):
        return None
    t = read_json(tenant_path(name))
    return t if isinstance(t, dict) else None


def tenants():
    out = {}
    try:
        names = sorted(os.listdir(os.path.join(VMS, ".tenants")))
    except OSError:
        names = []
    for fn in names:
        if fn.endswith(".json") and (t := tenant(fn[:-5])):
            out[fn[:-5]] = t
    return out


def quota_of(name):
    t = tenant(name)
    q = (t or {}).get("quota") or {}
    return {k: (int(q[k]) if q.get(k) is not None else None) for k in KEYS}


def tenant_view(name, all_vms, t=None):
    return {"name": name, "quota": quota_of(name) if t is None else {k: t.get("quota", {}).get(k) for k in KEYS},
            "allocated": allocated(all_vms, tenant=name),
            "vms": [v["name"] for v in all_vms if v["tenant"] == name],
            **({k: t[k] for k in ("created", "updated") if k in t} if t else {})}


def event(type_, data):
    """Record an event through the CLI (lib/common.sh emit_event); never fails the command."""
    try:
        subprocess.run([os.path.join(ROOT, "fcvm"), "_event", type_, "", "", json.dumps(data)],
                       check=False, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def tenant_cmd(args):
    usage = "usage: fcvm tenant set NAME [--vcpus N|none] [--mem MIB|none] [--vms N|none] | ls [--json] | show NAME | rm NAME"
    sub = args[0] if args else "ls"
    rest = args[1:]
    if sub == "set":
        if not rest or not NAME.match(rest[0]):
            die("invalid", usage if not rest else f"invalid tenant name '{rest[0]}'")
        name, rest = rest[0], rest[1:]
        t = tenant(name) or {"name": name, "quota": {k: None for k in KEYS}, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        quota = {k: t.get("quota", {}).get(k) for k in KEYS}
        flags = {"--vcpus": "vcpus", "--mem": "mem_mib", "--vms": "vms"}
        while rest:
            a = rest.pop(0)
            if a not in flags or not rest:
                die("invalid", f"unknown option {a} ({usage})" if a not in flags else usage)
            v = rest.pop(0)
            if v == "none":
                quota[flags[a]] = None
            elif re.fullmatch(r"[0-9]+", v):
                quota[flags[a]] = int(v)
            else:
                die("invalid", f"{a} wants a whole number, or none ({usage})")
        t["quota"], t["updated"] = quota, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(tenant_path(name), t)
        event("tenant.set", {"tenant": name, "quota": quota})
        limits = ", ".join(f"{k} {v}" for k, v in quota.items() if v is not None) or "no limits"
        sys.stderr.write(f"\033[1;34m==>\033[0m tenant '{name}': {limits}\n")
    elif sub == "ls":
        as_json = "--json" in rest
        all_vms = vms()
        names = sorted(set(tenants()) | {v["tenant"] for v in all_vms if v["tenant"]})
        views = [tenant_view(n, all_vms, tenants().get(n)) for n in names]
        if as_json:
            print(json.dumps(views, indent=1))
            return
        rows = [("NAME", "VCPUS", "MEM", "VMS", "QUOTA")]
        for t in views:
            q, a = t["quota"], t["allocated"]
            cell = lambda k: f"{a[k]}/{q[k]}" if q[k] is not None else str(a[k])   # noqa: E731
            rows.append((t["name"], cell("vcpus"), cell("mem_mib"), cell("vms"), "yes" if n_quota(q) else "none"))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    elif sub == "show":
        if not rest or not NAME.match(rest[0]):
            die("invalid", usage)
        all_vms = vms()
        if not tenant(rest[0]) and not any(v["tenant"] == rest[0] for v in all_vms):
            die("tenant_not_found", f"no tenant '{rest[0]}' (fcvm tenant ls)")
        print(json.dumps(tenant_view(rest[0], all_vms, tenant(rest[0])), indent=1))
    elif sub == "rm":
        if not rest or not NAME.match(rest[0]):
            die("invalid", usage)
        if not tenant(rest[0]):
            die("tenant_not_found", f"no tenant '{rest[0]}' (fcvm tenant ls)")
        os.unlink(tenant_path(rest[0]))
        event("tenant.removed", {"tenant": rest[0]})
        sys.stderr.write(f"\033[1;34m==>\033[0m removed tenant '{rest[0]}' (its VMs keep their label; no quota applies to them now)\n")
    else:
        die("invalid", usage)


def n_quota(q):
    return any(v is not None for v in q.values())


# --- admission --------------------------------------------------------------------------

def admit(args):
    """node.py admit [--claim --tenant T] VM: refuse with quota_exceeded or
    node_full when the VM wouldn't fit; else count it as starting. --claim
    checks only the tenant's quota (the VM already runs), for pool claim."""
    claim, tenant_name, vm = False, None, None
    while args:
        a = args.pop(0)
        if a == "--claim":
            claim = True
        elif a == "--tenant":
            tenant_name = args.pop(0) if args else die("invalid", "--tenant wants a name")
        else:
            vm = a
    if not vm or not NAME.match(vm):
        die("invalid", "usage: node.py admit [--claim --tenant T] VM")
    with Lock("admission"):
        all_vms = vms()
        me = next((v for v in all_vms if v["name"] == vm), None)
        if not me:
            die("vm_not_found", f"no VM '{vm}'")
        t = tenant_name or me["tenant"]
        need = {"vcpus": me["vcpus"], "mem_mib": me["mem_mib"], "vms": 1}
        if t:
            quota, used = quota_of(t), allocated(all_vms, tenant=t, exclude=vm)
            for k in KEYS:
                if quota[k] is not None and used[k] + need[k] > quota[k]:
                    die("quota_exceeded", f"tenant '{t}' would exceed its {k} quota: {used[k]} allocated + {need[k]} > {quota[k]} (fcvm tenant show {t})")
        if not claim and enforced():
            cap, _ = capacity()
            used = allocated(all_vms, exclude=vm)
            for k in KEYS:
                if cap[k] is not None and used[k] + need[k] > cap[k]:
                    die("node_full", f"this host is full: {k} {used[k]} allocated + {need[k]} > {cap[k]} (fcvm status --json)")
        if not claim:
            with open(os.path.join(VMS, vm, "starting"), "w"):
                pass


# --- status -----------------------------------------------------------------------------

def free_taps(prefix, total):
    """Taps nobody holds: claim_tap in lib/vm.sh flocks vms/.locks/PREFIXn."""
    free = 0
    for i in range(total):
        if not os.path.exists(f"/sys/class/net/{prefix}{i}"):
            continue
        try:
            fd = os.open(os.path.join(VMS, ".locks", f"{prefix}{i}"), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return free


def status():
    """The node view: capacity, allocated, allocatable, tenants, slots."""
    all_vms = vms()
    cap, host = capacity()
    alloc = allocated(all_vms)
    taps = number("NET_TAPS", 64)
    os.makedirs(os.path.join(VMS, ".locks"), exist_ok=True)
    present = lambda p: len([n for n in os.listdir("/sys/class/net") if re.fullmatch(p + r"\d+", n)])   # noqa: E731
    load = read("/proc/loadavg").split()[:3]
    out = {
        "version": os.environ.get("FCVM_VERSION") or read(os.path.join(ROOT, "VERSION")).strip() or None,
        "contract": int(os.environ.get("CONTRACT") or 1),
        "hostname": os.uname().nodename,
        "cordoned": read(os.path.join(VMS, ".node", "cordoned")).strip() or None,
        "admission": enforced(),
        "host": {**host, "load": [float(x) for x in load] if len(load) == 3 else None,
                 "mem_available_mib": meminfo("MemAvailable")},
        "capacity": cap,
        "allocated": alloc,
        "allocatable": {k: (cap[k] - alloc[k] if cap[k] is not None else None) for k in KEYS},
        "vms": {s: len([v for v in all_vms if v["state"] == s]) for s in ("running", "starting", "stopped")},
        "tenants": [tenant_view(n, all_vms, t) for n, t in tenants().items()],
        "slots": {"taps_full": {"total": present("fctap"), "free": free_taps("fctap", taps)},
                  "taps_restricted": {"total": present("fcrtap"), "free": free_taps("fcrtap", taps)},
                  "jail": os.path.exists("/run/fcvm/jaild.sock")},
        "kernel": os.path.basename(os.path.realpath(os.path.join(STATE, "kernels", "vmlinux"))) or None,
        "firecracker": os.environ.get("FIRECRACKER_VERSION") or None,
    }
    if out["kernel"] == "vmlinux":
        out["kernel"] = None
    return out


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "tenant":
        tenant_cmd(argv[1:])
    elif cmd == "admit":
        admit(argv[1:])
    elif cmd == "status":
        print(json.dumps(status(), indent=1))
    else:
        die("invalid", "usage: node.py tenant ... | admit [--claim --tenant T] VM | status")


if __name__ == "__main__":
    main(sys.argv[1:])
