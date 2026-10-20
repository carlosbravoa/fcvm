#!/usr/bin/env python3
"""Reading fcvm's events (docs/events.md): the state changes every fcvm
command records in vms/.events.jsonl (emit_event in lib/common.sh), one JSON
object per line with a strictly increasing `seq`.

  fcvm events [--since N] [-f] [--json] [--by ACTOR] [--type TYPE] [VM]
                                                  the command (this file's main)

`read(since, vm, limit, by, type_)` and `Tail` are what the command, the
web console's GET /api/events (lib/web/server.py) and the MCP server use.
Standard library only.
"""
import json
import os
import shlex
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


def wanted(e, since, vm, by, type_):
    return e.get("seq", 0) > since and (vm is None or e.get("vm") == vm) \
        and (by is None or e.get("by") == by) and (type_ is None or e.get("type") == type_)


def read(since=0, vm=None, limit=None, by=None, type_=None):
    """Events with seq > since (oldest first), optionally for one VM, one
    actor (`by`) or one type, at most limit (the latest ones)."""
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
            if wanted(e, since, vm, by, type_):
                out.append(e)
    out.sort(key=lambda e: e["seq"])
    return out[-limit:] if limit else out


class Tail:
    """Events after `since` as they are recorded, reading only what was
    appended since the last poll (and the whole file again after a
    rotation), so a follower costs nothing while nothing happens."""

    def __init__(self, since=0, vm=None, by=None, type_=None):
        self.since, self.vm, self.by, self.type_ = since, vm, by, type_
        self.ino, self.pos = None, 0

    def poll(self):
        """The new events, oldest first (possibly none)."""
        path = FILES[-1]
        try:
            st = os.stat(path)
        except OSError:
            return []
        if st.st_ino != self.ino or st.st_size < self.pos:
            # a rotation (or the first poll): everything after `since`, across both files
            out = read(self.since, self.vm, None, self.by, self.type_)
            self.ino, self.pos = st.st_ino, st.st_size
        else:
            if st.st_size == self.pos:
                return []
            out = []
            with open(path) as f:
                f.seek(self.pos)
                text = f.read()
            # a line still being written stays for the next poll
            cut = text.rfind("\n") + 1
            self.pos += len(text[:cut].encode())
            for line in text[:cut].splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if wanted(e, self.since, self.vm, self.by, self.type_):
                    out.append(e)
        if out:
            self.since = out[-1]["seq"]
        return out


def follow(since=0, vm=None, by=None, type_=None):
    """Yield events as they are recorded, forever."""
    tail = Tail(since, vm, by, type_)
    while True:
        yield from tail.poll()
        time.sleep(POLL)


def fmt(e):
    t = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
    extra = []
    if e.get("reason"):
        extra.append(e["reason"])
    for k, v in (e.get("data") or {}).items():
        if v not in (None, "", [], {}, False) and k != "at":
            extra.append(f"{k}={v}")
    line = f"{e['seq']:>6}  {t}  {e['type']:<14} {e.get('vm') or '':<24} {' '.join(extra):<24} {e.get('by', '')}"
    if e.get("argv"):
        line += "  $ fcvm " + " ".join(shlex.quote(a) for a in e["argv"])
    return line


def main(argv):
    since, follow_, as_json, vm, by, type_ = 0, False, False, None, None, None
    usage = "usage: fcvm events [--since N] [-f] [--json] [--by ACTOR] [--type TYPE] [VM]"
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--since":
            since = int(args.pop(0)) if args else 0
        elif a in ("-f", "--follow"):
            follow_ = True
        elif a == "--json":
            as_json = True
        elif a == "--by" and args:
            by = args.pop(0)
        elif a == "--type" and args:
            type_ = args.pop(0)
        elif a.startswith("-"):
            sys.stderr.write(f"\033[1;31merror:\033[0m unknown option {a} ({usage})\n")
            if not sys.stderr.isatty():
                sys.stderr.write("fcvm-error: invalid\n")
            sys.exit(1)
        else:
            vm = a
    if since < 0:                                   # the last -N events
        events = read(0, vm, None, by, type_)[since:]
    else:
        events = read(since, vm, None, by, type_)
    for e in events:
        print(json.dumps(e) if as_json else fmt(e), flush=True)
    if follow_:
        last = events[-1]["seq"] if events else max(since, 0)
        try:
            for e in follow(last, vm, by, type_):
                print(json.dumps(e) if as_json else fmt(e), flush=True)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main(sys.argv[1:])
