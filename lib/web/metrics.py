"""Metrics for fcvm serve: the guest's own view, longer history, Prometheus.

- Guest metrics come from the exec agent's "metrics" operation (fc-init): CPU
  time, memory and disk as the guest sees them, load, and its processes. That
  works in any image, with nothing installed in it.
- History: fcvm serve samples every 2 s for 10 minutes. Every minute, those
  samples are averaged into one point, kept for 24 hours and appended to
  STATE/metrics/NAME.jsonl, so the longer views survive restarts and reboots.
- Prometheus: the text exposition format, from the latest samples and the
  cumulative counters.
"""
import json
import os
import re
import time
from collections import deque

GUEST_TICK = 100            # the guest kernel's USER_HZ
LONG = 1440                 # one-minute points kept (24 h)
PAGE = 4096


def parse_guest(raw):
    """The agent's metrics reply -> dict (see metrics_op in init/fc-init.c)."""
    g = {"procs": []}
    for line in raw.decode(errors="replace").splitlines():
        if line.startswith("p "):
            head, _, rest = line.partition("\t")
            f = head.split(" ")
            if len(f) < 8:
                continue
            comm, _, cmd = rest.partition("\t")
            g["procs"].append({"pid": int(f[1]), "ppid": int(f[2]), "uid": int(f[3]), "state": f[4],
                               "ticks": int(f[5]), "rss": int(f[6]) * PAGE, "user": f[7], "comm": comm,
                               "cmd": cmd})
            continue
        key, _, rest = line.partition(" ")
        v = rest.split()
        if key == "cpu" and len(v) >= 8:
            ticks = list(map(int, v))
            g["cpu_busy"], g["cpu_total"] = sum(ticks) - ticks[3] - ticks[4], sum(ticks)
        elif key == "cpus" and v:
            g["cpus"] = int(v[0])
        elif key == "mem" and len(v) >= 2:
            g["mem_total"], g["mem_used"] = int(v[0]) * 1024, (int(v[0]) - int(v[1])) * 1024
        elif key == "load" and v:
            g["load1"] = float(v[0])
        elif key == "uptime" and v:
            g["uptime"] = int(v[0])
        elif key == "disk" and len(v) >= 2:
            g["disk_total"], g["disk_used"] = int(v[0]), int(v[1])
    return g


def kernel_thread(p):
    return p["pid"] == 2 or p["ppid"] == 2


class Guest:
    """Turns successive guest readings into rates (CPU %, per process too)."""

    def __init__(self):
        self.prev = {}      # name -> (t, busy, total, cpus)
        self.pprev = {}     # name -> (t, {pid: ticks})

    def sample(self, name, g, now):
        """A chartable sample from a reading: g_* keys (None until a delta exists)."""
        s = {"t": now, "g_cpu_pct": None, "g_mem_used": g.get("mem_used"), "g_mem_total": g.get("mem_total"),
             "g_disk_used": g.get("disk_used"), "g_disk_total": g.get("disk_total"), "g_load1": g.get("load1"),
             "g_procs": sum(1 for p in g["procs"] if not kernel_thread(p))}
        p = self.prev.get(name)
        if p and "cpu_total" in g and g["cpu_total"] > p[2]:
            # as a share of one CPU, like the host-side chart (100% = one vCPU busy)
            s["g_cpu_pct"] = round(100 * (g["cpu_busy"] - p[1]) / (g["cpu_total"] - p[2]) * g.get("cpus", 1), 1)
        if "cpu_total" in g:
            self.prev[name] = (now, g["cpu_busy"], g["cpu_total"], g.get("cpus", 1))
        return s

    def processes(self, name, g, now):
        """The process list with CPU % since the previous call for this VM."""
        prev_t, prev = self.pprev.get(name, (None, {}))
        out = []
        for p in g["procs"]:
            cpu = None
            if prev_t is not None and p["pid"] in prev and now > prev_t:
                cpu = round(100 * (p["ticks"] - prev[p["pid"]]) / GUEST_TICK / (now - prev_t), 1)
            # fcvm's own processes in the guest: fc-init as PID 1, and its exec agent
            # (/init, or /.fcvm/bin/fc-init --agent in system images) with its sessions
            fcvm = ("fc-init (init)" if p["pid"] == 1 and p["cmd"] == "/init" else
                    "fc-init (exec agent)" if p["cmd"] in ("/init", "/.fcvm/bin/fc-init --agent") else None)
            out.append({**p, "cpu_pct": cpu, "kernel": kernel_thread(p), "fcvm": fcvm})
        self.pprev[name] = (now, {p["pid"]: p["ticks"] for p in g["procs"]})
        return out

    def forget(self, name):
        self.prev.pop(name, None)
        self.pprev.pop(name, None)


def average(samples):
    """One point from a minute of samples: the mean of each numeric key."""
    keys = {k for s in samples for k in s if k != "t"}
    out = {"t": samples[-1]["t"]}
    for k in keys:
        vals = [s[k] for s in samples if isinstance(s.get(k), (int, float))]
        out[k] = round(sum(vals) / len(vals), 2) if vals else None
    return out


class History:
    """One-minute points for 24 h, per VM and for the host ("_host"), on disk."""

    def __init__(self, directory):
        self.dir = directory
        self.points = {}    # name -> deque of points
        os.makedirs(directory, exist_ok=True)
        for f in os.listdir(directory):
            if f.endswith(".jsonl"):
                self.points[f[:-6]] = deque(self._load(os.path.join(directory, f)), maxlen=LONG)

    @staticmethod
    def _load(path):
        try:
            with open(path) as f:
                lines = f.readlines()[-LONG:]
        except OSError:
            return []
        cutoff, out = time.time() - LONG * 60, []
        for l in lines:
            try:
                p = json.loads(l)
            except ValueError:
                continue
            if p.get("t", 0) >= cutoff:
                out.append(p)
        return out

    def add(self, name, point):
        self.points.setdefault(name, deque(maxlen=LONG)).append(point)
        path = os.path.join(self.dir, f"{name}.jsonl")
        try:
            with open(path, "a") as f:
                f.write(json.dumps(point) + "\n")
            if os.path.getsize(path) > 600_000:       # keep the file near the 24 h it's for
                with open(path + ".tmp", "w") as f:
                    f.writelines(json.dumps(p) + "\n" for p in self.points[name])
                os.replace(path + ".tmp", path)
        except OSError:
            pass

    def forget(self, name):
        self.points.pop(name, None)
        try:
            os.unlink(os.path.join(self.dir, f"{name}.jsonl"))
        except OSError:
            pass

    def since(self, name, seconds):
        cutoff = time.time() - seconds
        return [p for p in self.points.get(name, ()) if p["t"] >= cutoff]


# --- Prometheus -----------------------------------------------------------------------------

def _label(v):
    return re.sub(r'(["\\\\])', r"\\\1", str(v)).replace("\n", " ")


def prometheus(version, host, vms, counters, latest, guest, clk_tck):
    """Text exposition format.
    host: the latest host sample; vms: `fcvm ls --json`; counters: name ->
    (pid, t, cpu_ticks, read_bytes, write_bytes, rx, tx); latest: name -> the
    latest VM sample; guest: name -> the latest guest sample."""
    out = []

    def metric(name, kind, help_, rows):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in rows:
            if value is None:
                continue
            ls = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{ls}}} {value}" if ls else f"{name} {value}")

    metric("fcvm_info", "gauge", "fcvm's version.", [({"version": version}, 1)])
    metric("fcvm_host_cpu_percent", "gauge", "Host CPU use, all CPUs (%).", [({}, host.get("cpu_pct"))])
    metric("fcvm_host_memory_total_bytes", "gauge", "Host memory.", [({}, host.get("mem_total"))])
    metric("fcvm_host_memory_available_bytes", "gauge", "Host memory available.", [({}, host.get("mem_available"))])
    running = [v for v in vms if v.get("state") == "running"]
    metric("fcvm_vms", "gauge", "VMs by state.",
           [({"state": st}, sum(1 for v in vms if v.get("state") == st)) for st in sorted({v.get("state") for v in vms})])
    metric("fcvm_vm_up", "gauge", "1 if the VM is running.",
           [({"vm": v["name"], "image": v.get("image"), "type": v.get("type"), "jailed": str(bool(v.get("jail"))).lower()},
             1 if v.get("state") == "running" else 0) for v in vms])
    metric("fcvm_vm_memory_allocated_bytes", "gauge", "Memory given to the VM.",
           [({"vm": v["name"]}, (v.get("mem_mib") or 0) << 20) for v in vms])
    names = [v["name"] for v in running]
    metric("fcvm_vm_memory_rss_bytes", "gauge", "Host memory backing the VM (Firecracker's RSS).",
           [({"vm": n}, (latest.get(n) or {}).get("rss")) for n in names])
    metric("fcvm_vm_cpu_seconds_total", "counter", "CPU time of the VM's Firecracker process.",
           [({"vm": n}, round(counters[n][2] / clk_tck, 2)) for n in names if n in counters])
    metric("fcvm_vm_disk_read_bytes_total", "counter", "Bytes the VM read from its disks.",
           [({"vm": n}, counters[n][3]) for n in names if n in counters])
    metric("fcvm_vm_disk_written_bytes_total", "counter", "Bytes the VM wrote to its disks.",
           [({"vm": n}, counters[n][4]) for n in names if n in counters])
    metric("fcvm_vm_network_receive_bytes_total", "counter", "Bytes the VM received.",
           [({"vm": n}, counters[n][5]) for n in names if n in counters])
    metric("fcvm_vm_network_transmit_bytes_total", "counter", "Bytes the VM sent.",
           [({"vm": n}, counters[n][6]) for n in names if n in counters])
    for key, name, help_ in (("g_cpu_pct", "fcvm_guest_cpu_percent", "CPU use inside the guest (100 = one vCPU)."),
                             ("g_mem_used", "fcvm_guest_memory_used_bytes", "Memory used, as the guest sees it."),
                             ("g_mem_total", "fcvm_guest_memory_total_bytes", "Memory the guest has."),
                             ("g_disk_used", "fcvm_guest_disk_used_bytes", "Root filesystem used, in the guest."),
                             ("g_disk_total", "fcvm_guest_disk_total_bytes", "Root filesystem size, in the guest."),
                             ("g_load1", "fcvm_guest_load1", "The guest's 1-minute load average."),
                             ("g_procs", "fcvm_guest_processes", "User processes in the guest.")):
        metric(name, "gauge", help_, [({"vm": n}, (guest.get(n) or {}).get(key)) for n in names])
    return "\n".join(out) + "\n"
