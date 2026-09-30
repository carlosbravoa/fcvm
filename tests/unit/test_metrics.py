"""Guest metrics, history and the Prometheus output (lib/web/metrics.py)."""
import tempfile
import time
import unittest

from helpers import ROOT  # noqa: F401
import metrics as m

READING = b"""cpu 100 0 50 800 10 0 0 0
cpus 2
mem 1000 400
load 0.50 0.25 0.10
uptime 42
disk 2000000 500000
p 1 0 0 S 5 100 root\tinit\t/init
p 2 0 0 S 0 0 root\tkthreadd\t
p 9 2 0 I 0 0 root\tkworker/0\t
p 40 1 1000 R 300 2000 app\tpython3\tpython3 -m http.server
"""


class ParseGuest(unittest.TestCase):
    def test_reading(self):
        g = m.parse_guest(READING)
        self.assertEqual((g["cpu_busy"], g["cpu_total"], g["cpus"]), (150, 960, 2))
        self.assertEqual((g["mem_total"], g["mem_used"]), (1000 * 1024, 600 * 1024))
        self.assertEqual((g["load1"], g["uptime"], g["disk_used"]), (0.5, 42, 500000))
        self.assertEqual(len(g["procs"]), 4)
        p = g["procs"][3]
        self.assertEqual((p["pid"], p["user"], p["comm"], p["cmd"], p["rss"]),
                         (40, "app", "python3", "python3 -m http.server", 2000 * 4096))

    def test_garbage_is_ignored(self):
        self.assertEqual(m.parse_guest(b"cpu 1\nnonsense\np 1 2\n\xff"), {"procs": []})


class Rates(unittest.TestCase):
    def test_cpu_percent_counts_one_vcpu_as_100(self):
        gm = m.Guest()
        g = m.parse_guest(READING)
        self.assertIsNone(gm.sample("vm", g, 0)["g_cpu_pct"])      # needs two readings
        g2 = {**g, "cpu_busy": g["cpu_busy"] + 100, "cpu_total": g["cpu_total"] + 100}
        s = gm.sample("vm", g2, 10)
        self.assertEqual(s["g_cpu_pct"], 200.0)                     # both vCPUs busy
        self.assertEqual(s["g_procs"], 2)                           # kernel threads not counted

    def test_processes(self):
        gm = m.Guest()
        g = m.parse_guest(READING)
        first = {p["pid"]: p for p in gm.processes("vm", g, 0)}
        self.assertIsNone(first[40]["cpu_pct"])
        self.assertEqual(first[1]["fcvm"], "fc-init (init)")
        self.assertTrue(first[9]["kernel"])
        g["procs"][3] = {**g["procs"][3], "ticks": 350}
        later = {p["pid"]: p for p in gm.processes("vm", g, 1)}
        self.assertEqual(later[40]["cpu_pct"], 50.0)                # 50 ticks of 100 in one second


class History(unittest.TestCase):
    def test_average(self):
        a = m.average([{"t": 1, "x": 1, "y": None}, {"t": 2, "x": 3, "y": None}])
        self.assertEqual(a, {"t": 2, "x": 2.0, "y": None})

    def test_persisted_and_trimmed_to_a_day(self):
        d = tempfile.mkdtemp(prefix="fcvm-hist-")
        h = m.History(d)
        now = time.time()
        h.add("vm", {"t": now - 90000, "x": 1})                    # older than 24 h
        h.add("vm", {"t": now - 60, "x": 2})
        self.assertEqual([p["x"] for p in h.since("vm", 3600)], [2])
        again = m.History(d)                                        # a restarted server
        self.assertEqual([p["x"] for p in again.points["vm"]], [2])
        again.forget("vm")
        self.assertEqual(m.History(d).points, {})


class Prometheus(unittest.TestCase):
    def test_exposition(self):
        vms = [{"name": "a", "state": "running", "image": "alpine", "type": "app", "jail": True, "mem_mib": 256},
               {"name": 'b"x', "state": "stopped", "image": "u", "type": "system", "mem_mib": 1024}]
        text = m.prometheus("0.5.3", {"cpu_pct": 12.5}, vms, {"a": (1, 0, 250, 10, 20, 30, 40)},
                            {"a": {"rss": 1000}}, {"a": {"g_cpu_pct": 50.0, "g_procs": 3}}, 100)
        lines = text.splitlines()
        self.assertIn('fcvm_info{version="0.5.3"} 1', lines)
        self.assertIn("fcvm_host_cpu_percent 12.5", lines)
        self.assertIn('fcvm_vm_up{vm="a",image="alpine",type="app",jailed="true"} 1', lines)
        self.assertIn('fcvm_vm_up{vm="b\\"x",image="u",type="system",jailed="false"} 0', lines)
        self.assertIn('fcvm_vm_cpu_seconds_total{vm="a"} 2.5', lines)
        self.assertIn('fcvm_guest_cpu_percent{vm="a"} 50.0', lines)
        self.assertIn('fcvm_vms{state="running"} 1', lines)
        self.assertNotIn('fcvm_guest_load1{vm="a"}', text)          # unknown values are left out
        for l in lines:                                             # every sample has a TYPE
            if not l.startswith("#"):
                self.assertIn(f"# TYPE {l.split('{')[0].split(' ')[0]} ", text)


if __name__ == "__main__":
    unittest.main()
