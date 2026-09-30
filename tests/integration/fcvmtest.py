"""Helpers for integration tests: real VMs through the real fcvm CLI.

Everything a test creates is named fcvmtest-* and removed afterwards, and
leftovers from an interrupted run are swept first, so a developer's own VMs
and images are never touched. Tests run against the user's normal fcvm state
(FCVM_HOME), because the network, the jailer helper and the service all serve
one state directory.
"""
import json
import os
import re
import secrets
import socket
import subprocess
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FCVM = os.path.join(ROOT, "fcvm")
PREFIX = "fcvmtest-"
IMAGE = "alpine-latest"


def fcvm(*args, check=True, timeout=240, input=None, env=None):
    r = subprocess.run([FCVM, *map(str, args)], capture_output=True, text=True, timeout=timeout,
                       input=input, env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise AssertionError(f"fcvm {' '.join(map(str, args))} failed ({r.returncode}):\n{r.stdout}{r.stderr}")
    return r


def setting(name):
    """An fcvm setting as fcvm sees it (fcvm.conf, environment, default)."""
    r = subprocess.run(["bash", "-c", f'. "{ROOT}/lib/common.sh"; echo "${name}"'],
                       capture_output=True, text=True)
    return r.stdout.strip()


STATE = setting("FCVM_HOME")
VMS = os.path.join(STATE, "vms")


def unique(kind):
    return f"{PREFIX}{kind}-{secrets.token_hex(3)}"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout=30, every=0.5, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout} s waiting for {what}")


# --- requirements and sweeping ---------------------------------------------------------

_checked = None


def require_host():
    """Skip (not fail) when this host can't run VMs: tests/run integration on a
    laptop without setup should say what's missing, not explode."""
    global _checked
    if _checked is None:
        why = []
        if not (os.access("/dev/kvm", os.R_OK | os.W_OK)):
            why.append("no access to /dev/kvm")
        if not os.path.exists(os.path.join(STATE, "kernels", "vmlinux")):
            why.append("no guest kernel (fcvm kernel)")
        if not os.path.exists(os.path.join(STATE, "bin", "firecracker")):
            why.append("no firecracker (fcvm firecracker)")
        if subprocess.run(["ip", "link", "show", setting("NET_BRIDGE")], capture_output=True).returncode:
            why.append("bridges down (fcvm net-up)")
        _checked = "; ".join(why)
        if not _checked:
            sweep()
            if not os.path.exists(os.path.join(STATE, "images", f"{IMAGE}.json")):
                fcvm("import", "alpine:latest", timeout=600)
    if _checked:
        raise unittest.SkipTest(f"host not set up for integration tests: {_checked} (see: fcvm setup)")


def sweep():
    """Remove fcvmtest-* leftovers from an interrupted run."""
    for vm in json.loads(fcvm("ls", "--all", "--json").stdout or "[]"):
        if vm["name"].startswith(PREFIX):
            fcvm("stop", vm["name"], check=False, env={"FCVM_SYSTEM_STOP": "1"})
            fcvm("rm", vm["name"], check=False)
    for s in json.loads(fcvm("snapshot", "ls", "--json").stdout or "[]"):
        if s.get("name", "").startswith(PREFIX):
            fcvm("snapshot", "rm", s["name"], check=False)
    for i in json.loads(fcvm("images", "--json").stdout or "[]"):
        if i["name"].startswith(PREFIX):
            fcvm("rmi", i["name"], check=False)
    for v in json.loads(fcvm("volume", "ls", "--json").stdout or "[]"):
        if v.get("name", "").startswith(PREFIX):
            fcvm("volume", "rm", v["name"], check=False)
    for t in json.loads(fcvm("template", "ls", "--json").stdout or "[]"):
        if t["name"].startswith(PREFIX):
            fcvm("template", "rm", t["name"], check=False)


def jail_usable():
    """fcvm-jaild is running and serves this state directory."""
    try:
        with open("/etc/fcvm/jaild.json") as f:
            root = json.load(f)["fcvm_root"]
    except (OSError, ValueError, KeyError):
        return False
    return os.path.exists("/run/fcvm/jaild.sock") and os.path.realpath(root) == os.path.realpath(STATE)


# --- the test base class -------------------------------------------------------------------

class VMTestCase(unittest.TestCase):
    """Creates things through helpers that register their cleanup."""

    @classmethod
    def setUpClass(cls):
        require_host()

    def vm(self, *opts, image=IMAGE, start=True, cmd=None, kind="vm"):
        name = unique(kind)
        args = ["create", name, image, *opts]
        if cmd is not None:
            args += ["--", *cmd]
        elif image == IMAGE and "--idle" not in opts:
            args.append("--idle")
        fcvm(*args)
        self.addCleanup(self.remove_vm, name)
        if start:
            fcvm("start", name)
        return name

    @staticmethod
    def remove_vm(name):
        fcvm("stop", name, check=False)
        fcvm("rm", name, check=False)

    def snapshot(self, vm):
        name = unique("snap")
        fcvm("snapshot", vm, name)
        self.addCleanup(fcvm, "snapshot", "rm", name, check=False)
        return name

    def volume(self):
        name = unique("vol")
        self.addCleanup(fcvm, "volume", "rm", name, check=False)
        return name

    def image_name(self):
        name = unique("img")
        self.addCleanup(fcvm, "rmi", name, check=False)
        return name

    def sh(self, vm, script, check=True, timeout=120):
        """Run a shell snippet in VM; returns the CompletedProcess."""
        return fcvm("exec", vm, "--", "sh", "-c", script, check=check, timeout=timeout)

    def inspect(self, vm):
        return json.loads(fcvm("inspect", vm).stdout)


# --- a running fcvm serve (the service's, or one started for the tests) ------------------------

class Server:
    """The console/API server and its supervisor for this state directory."""

    def __init__(self):
        self.proc = None
        state = os.path.join(VMS, ".serve.json")
        try:
            with open(state) as f:
                s = json.load(f)
            if os.path.exists(f"/proc/{s['pid']}"):
                self.url, self.token = s["url"].split("/?")[0], s["token"]
                return
        except (OSError, ValueError, KeyError):
            pass
        port = free_port()
        self.proc = subprocess.Popen([FCVM, "serve", "--port", str(port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        s = wait_for(lambda: self._read(state, port), 15, what="fcvm serve")
        self.url, self.token = f"http://127.0.0.1:{port}", s["token"]

    @staticmethod
    def _read(path, port):
        try:
            with open(path) as f:
                s = json.load(f)
            return s if s.get("port") == port else None
        except (OSError, ValueError):
            return None

    def api(self, method, path, body=None, token=None):
        req = urllib.request.Request(f"{self.url}/api/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token or self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def raw(self, method, path, token=None):
        """A non-JSON endpoint (e.g. /metrics): (status, text)."""
        req = urllib.request.Request(f"{self.url}{path}", method=method,
                                     headers={"Authorization": f"Bearer {token or self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            try:
                os.unlink(os.path.join(VMS, ".serve.json"))   # ours: don't leave CLI delegation pointing at it
            except OSError:
                pass


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)
