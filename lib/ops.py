#!/usr/bin/env python3
"""Operations: slow fcvm commands (import, build, export, push, snapshot,
commit) run detached, with their state in vms/.ops/ID.json and their output
in vms/.ops/ID.log, so they survive the caller and the service, and anyone
can ask how they went.

  fcvm import nginx:latest --op          prints the operation's id and returns
  fcvm ops                               recent operations
  fcvm ops ID [-f] [--json]              one, with its output (-f follows)
  fcvm ops start [--title T] KIND ARGS   what --op does (the web console uses it)

The web console's GET /api/jobs reads the same store (lib/web/server.py).
An operation whose process is gone without having ended (the host rebooted,
the process was killed) is marked failed with the error "interrupted".
Finished operations are kept for a week. Standard library only.
"""
import json
import os
import base64
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get("FCVM_HOME") or ROOT
FCVM = os.path.join(ROOT, "fcvm")
OPS = os.path.join(STATE, "vms", ".ops")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ID = re.compile(r"^op-[a-z0-9]{10}$")
KEEP_DAYS = 7
TAIL = 2000          # lines kept in memory for one GET


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def _boot_id():
    return _read("/proc/sys/kernel/random/boot_id").strip()


def _write(op, meta):
    tmp = os.path.join(OPS, f".{op}.json.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, os.path.join(OPS, f"{op}.json"))


def meta(op):
    """The operation's record, or None."""
    if not ID.match(op or ""):
        return None
    try:
        return json.loads(_read(os.path.join(OPS, f"{op}.json")) or "null")
    except ValueError:
        return None


def lines(op, since=0):
    """Output lines from offset since: (lines, next offset)."""
    text = ANSI.sub("", _read(os.path.join(OPS, f"{op}.log")))
    all_lines = [l.rstrip() for l in re.split(r"\r\n|\n|\r", text)]
    if all_lines and all_lines[-1] == "":
        all_lines.pop()                          # the trailing newline
    return all_lines[since:since + TAIL], len(all_lines)


def get(op, since=0):
    """What GET /api/jobs/ID returns: the record, lines from since, next."""
    m = meta(op)
    if not m:
        return None
    _sweep_one(op, m)
    ls, nxt = lines(op, since)
    return {**m, "lines": ls, "next": nxt}


def list_ops(limit=20):
    """The most recent operations, each with the last 3 lines of output."""
    sweep()
    out = []
    for f in os.listdir(OPS) if os.path.isdir(OPS) else []:
        if f.endswith(".json") and not f.startswith("."):
            m = meta(f[:-5])
            if m:
                out.append(m)
    out.sort(key=lambda m: -m["started"])
    return [{**m, "log": lines(m["id"])[0][-3:]} for m in out[:limit]]


def _sweep_one(op, m):
    """Mark a running operation whose process is gone as interrupted."""
    if m.get("status") != "running":
        return m
    alive = m.get("boot_id") == _boot_id() and m.get("pid") and os.path.exists(f"/proc/{m['pid']}")
    if alive:
        return m
    m.update(status="failed", ended=m.get("ended") or time.time(), error="interrupted")
    _write(op, m)
    return m


def sweep():
    """Interrupted operations marked, old ones removed."""
    now = time.time()
    for f in os.listdir(OPS) if os.path.isdir(OPS) else []:
        if not f.endswith(".json") or f.startswith("."):
            continue
        op = f[:-5]
        m = meta(op)
        if not m:
            continue
        m = _sweep_one(op, m)
        if m.get("ended") and now - m["ended"] > KEEP_DAYS * 86400:
            for ext in (".json", ".log"):
                try:
                    os.unlink(os.path.join(OPS, op + ext))
                except OSError:
                    pass


def start(args, kind, title=None):
    """Run `fcvm ARGS` detached as an operation; returns its id at once."""
    os.makedirs(OPS, exist_ok=True)
    op = "op-" + base64.b32encode(os.urandom(7)).decode().lower()[:10]   # (lib/secrets.py shadows the secrets module)
    m = {"id": op, "kind": kind, "title": title or " ".join(args), "command": list(args), "status": "running",
         "started": time.time(), "ended": None, "exit": None, "error": None, "pid": None, "boot_id": _boot_id()}
    _write(op, m)
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)                       # the child exits at once; the grandchild runs the command
        return op
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        _run(op, m, args)
    finally:
        os._exit(0)


def _run(op, m, args):
    m["pid"] = os.getpid()
    _write(op, m)
    with open(os.path.join(OPS, f"{op}.log"), "ab") as log, open(os.devnull, "rb") as null:
        p = subprocess.run([FCVM, *args], stdin=null, stdout=log, stderr=subprocess.STDOUT)
    m["status"] = "done" if p.returncode == 0 else "failed"
    m["exit"], m["ended"] = p.returncode, time.time()
    if p.returncode:
        errs = [l for l in lines(op)[0] if l.startswith("error:") or l.startswith("fcvm-error:")]
        m["error"] = next((l[7:] for l in errs if l.startswith("error: ")), None) or f"exit {p.returncode}"
        m["code"] = next((l.split(": ", 1)[1] for l in errs if l.startswith("fcvm-error: ")), "error")
    subprocess.run([FCVM, "_event", "operation.done", "", m["status"],   # before the record says so: a
                    json.dumps({"op": op, "kind": m["kind"], "title": m["title"], "error": m["error"]})],   # reader that sees
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)         # done finds the event
    _write(op, m)


def fmt(m):
    t = time.strftime("%H:%M:%S", time.localtime(m["started"]))
    took = f"{(m['ended'] or time.time()) - m['started']:.0f}s"
    return f"{m['id']}  {t}  {m['status']:<8} {took:>6}  {m['title']}" + (f"  ({m['error']})" if m.get("error") else "")


def main(argv):
    args = list(argv)
    if args[:1] == ["start"]:
        args.pop(0)
        title = None
        if args[:1] == ["--title"]:
            title = args[1]
            args = args[2:]
        if len(args) < 2:
            sys.exit("usage: fcvm ops start [--title T] KIND FCVM-ARGS...")
        print(start(args[1:], args[0], title))
        return
    follow, as_json, op = False, False, None
    for a in args:
        if a in ("-f", "--follow"):
            follow = True
        elif a == "--json":
            as_json = True
        elif a.startswith("-"):
            sys.exit(f"\033[1;31merror:\033[0m unknown option {a} (usage: fcvm ops [ID] [-f] [--json])")
        else:
            op = a
    if not op:
        ops = list_ops()
        if as_json:
            print(json.dumps(ops, indent=2))
        else:
            for m in ops:
                print(fmt(m))
        return
    j = get(op)
    if not j:
        print(f"\033[1;31merror:\033[0m no operation '{op}' (see: fcvm ops)", file=sys.stderr)
        if not sys.stderr.isatty():
            print("fcvm-error: not_found", file=sys.stderr)
        sys.exit(1)
    if as_json and not follow:
        print(json.dumps(j, indent=2))
        return
    print(fmt(j), file=sys.stderr)
    for l in j["lines"]:
        print(l)
    nxt = j["next"]
    while follow and j["status"] == "running":
        time.sleep(0.5)
        j = get(op, nxt)
        for l in j["lines"]:
            print(l, flush=True)
        nxt = j["next"]
    if follow:
        print(fmt(j), file=sys.stderr)
    sys.exit(0 if j["status"] != "failed" else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
