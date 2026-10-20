"""Events (docs/events.md): emit_event in lib/common.sh, and lib/events.py."""
import json
import os
import tempfile
import unittest

from helpers import bash
import events


class Emit(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fcvm-unit-")
        os.makedirs(os.path.join(self.home, "vms", "box"))
        with open(os.path.join(self.home, "vms", "box", "vm.json"), "w") as f:
            json.dump({"labels": {"team": "a"}}, f)
        events.VMS = os.path.join(self.home, "vms")
        events.FILES = tuple(os.path.join(events.VMS, f) for f in (".events.1.jsonl", ".events.jsonl"))

    def emit(self, *args, env=None):
        r = bash("emit_event " + " ".join(f"'{a}'" for a in args), home=self.home, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sequence_fields_and_labels(self):
        self.emit("vm.created", "box", "", '{"image": "alpine-latest"}')
        self.emit("vm.started", "box", "fork", env={"FCVM_ACTOR": "serve"})
        self.emit("vm.deleted", "nope")                      # no vm.json: still recorded
        evs = events.read()
        self.assertEqual([e["seq"] for e in evs], [1, 2, 3])
        self.assertEqual((evs[0]["type"], evs[0]["vm"], evs[0]["reason"], evs[0]["data"], evs[0]["labels"]),
                         ("vm.created", "box", None, {"image": "alpine-latest"}, {"team": "a"}))
        self.assertTrue(evs[0]["by"].startswith("cli:"))
        self.assertEqual((evs[1]["reason"], evs[1]["by"]), ("fork", "serve"))
        self.assertEqual(evs[2]["labels"], {})
        self.assertEqual(events.last_seq(), 3)
        self.assertEqual([e["seq"] for e in events.read(since=1)], [2, 3])
        self.assertEqual([e["seq"] for e in events.read(vm="box")], [1, 2])
        self.assertEqual([e["seq"] for e in events.read(limit=1)], [3])

    def test_rotation_keeps_the_previous_file(self):
        for i in range(4):
            self.emit("vm.updated", "box", env={"FCVM_EVENTS_ROTATE": "300"})   # rotate after ~2 lines
        self.assertTrue(os.path.exists(events.FILES[0]))
        evs = events.read()
        self.assertEqual([e["seq"] for e in evs][-1], 4)
        self.assertEqual(events.oldest_seq(), evs[0]["seq"])
        self.assertLess(len(evs), 4 + 1)

    def test_bad_data_does_not_fail_the_command(self):
        r = bash("emit_event vm.x box '' 'not json'; echo survived", home=self.home)
        self.assertIn("survived", r.stdout)
        self.assertIn("could not record event", r.stderr)


class Tailing(Emit):
    def test_tail_reads_only_what_was_appended(self):
        self.emit("vm.created", "box")
        t = events.Tail()
        self.assertEqual([e["seq"] for e in t.poll()], [1])
        self.assertEqual(t.poll(), [])
        self.emit("vm.started", "box")
        self.emit("vm.stopped", "other")
        self.assertEqual([e["seq"] for e in t.poll()], [2, 3])
        only = events.Tail(since=1, vm="box")
        self.assertEqual([e["seq"] for e in only.poll()], [2])
        with open(events.FILES[-1], "a") as f:
            f.write('{"seq": 4, "type": "vm.x", "vm": "box"')              # a line still being written
        self.assertEqual(t.poll(), [])
        with open(events.FILES[-1], "a") as f:
            f.write("}\n")
        self.assertEqual([e["seq"] for e in t.poll()], [4])

    def test_tail_survives_a_rotation(self):
        t = events.Tail()
        for i in range(4):
            self.emit("vm.updated", "box", env={"FCVM_EVENTS_ROTATE": "300"})
            t.poll()
        self.assertEqual(t.since, 4)
        self.emit("vm.deleted", "box")
        self.assertEqual([e["seq"] for e in t.poll()], [5])


class Format(unittest.TestCase):
    def test_fmt(self):
        line = events.fmt({"seq": 7, "t": 0, "type": "vm.stopped", "vm": "web", "reason": "ttl", "by": "supervisor", "data": {"x": None}})
        self.assertIn("vm.stopped", line)
        self.assertIn("ttl", line)
        self.assertTrue(line.strip().endswith("supervisor"))


if __name__ == "__main__":
    unittest.main()
