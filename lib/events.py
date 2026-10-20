#!/usr/bin/env python3
"""Reading fcvm's events (docs/events.md): the state changes every fcvm
command records in vms/.events.jsonl (emit_event in lib/common.sh), one JSON
object per line with a strictly increasing `seq`.

  fcvm events [--since N] [-f] [--json] [VM]      the command (this file's main)

`read(since, vm)` and `follow(since, vm)` are what the command and the web
console's GET /api/events (lib/web/server.py) use. Standard library only.
"""
import json
import os
import sys
import time

STATE = os.environ.get("FCVM_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VMS = os.path.join(STATE, "vms")
FILES = (os.path.join(VMS, ".events.1.jsonl"), os.path.join(VMS, ".events.jsonl"))   # older first
POLL = 0.5


def last_seq():
    """The sequence number of the latest event (0 when there is none)."""
    try:
        with open(os.path.join(VMS, ".events.seq")) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def oldest_seq():
    """The sequence number of the oldest event still kept (rotation drops older ones)."""
    for path in FILES:
        try:
            with open(path) as f:
                for line in f:
                    try:
                        return json.loads(line)["seq"]
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue
    return None


def read(since=0, vm=None, limit=None):
    """Events with seq > since (oldest first), optionally for one VM, at most limit."""
    out = []
    for path in FILES:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue                        # a line cut by a crash, or still being written
            if e.get("seq", 0) > since and (vm is None or e.get("vm") == vm):
                out.append(e)
    out.sort(key=lambda e: e["seq"])
    return out[-limit:] if limit else out


def follow(since=0, vm=None):
    """Yield events as they are recorded, forever."""
    while True:
        for e in read(since, vm):
            since = e["seq"]
            yield e
        time.sleep(POLL)


def fmt(e):
    t = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
    extra = []
    if e.get("reason"):
        extra.append(e["reason"])
    for k, v in (e.get("data") or {}).items():
        if v not in (None, "", [], {}, False) and k != "at":
            extra.append(f"{k}={v}")
    return f"{e['seq']:>6}  {t}  {e['type']:<14} {e.get('vm') or '':<24} {' '.join(extra):<24} {e.get('by', '')}"


def main(argv):
    since, follow_, as_json, vm = 0, False, False, None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--since":
            since = int(args.pop(0)) if args else 0
        elif a in ("-f", "--follow"):
            follow_ = True
        elif a == "--json":
            as_json = True
        elif a.startswith("-"):
            sys.exit(f"\033[1;31merror:\033[0m unknown option {a} (usage: fcvm events [--since N] [-f] [--json] [VM])")
        else:
            vm = a
    if since < 0:                                   # the last -N events
        events = read(0, vm)[since:]
    else:
        events = read(since, vm)
    for e in events:
        print(json.dumps(e) if as_json else fmt(e), flush=True)
    if follow_:
        last = events[-1]["seq"] if events else max(since, 0)
        try:
            for e in follow(last, vm):
                print(json.dumps(e) if as_json else fmt(e), flush=True)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main(sys.argv[1:])
