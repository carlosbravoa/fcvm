"""Networking and isolation: published ports, VMs vs host services and each
other, anti-spoofing, egress allowlists, --net none. Some tests reach
example.com, so they need internet access."""
import http.server
import socket
import threading
import time
import unittest

from fcvmtest import VMTestCase, fcvm, setting, free_port, strip_ansi


class Ports(VMTestCase):
    def test_published_port_is_local_only(self):
        port = free_port()
        vm = self.vm("-p", f"{port}:8080")
        self.sh(vm, 'setsid sh -c "echo hello-port | nc -l -p 8080" >/dev/null 2>&1 </dev/null &')
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            self.assertEqual(s.recv(100).strip(), b"hello-port")
        # not on other addresses
        with open("/proc/net/tcp") as f:
            listeners = [l.split()[1] for l in f.readlines()[1:] if l.split()[3] == "0A"]
        self.assertIn(f"0100007F:{port:04X}", listeners)
        self.assertNotIn(f"00000000:{port:04X}", listeners)

    def test_publish_and_unpublish_live(self):
        vm = self.vm()
        self.sh(vm, 'setsid sh -c "while :; do echo hello-live | nc -l -p 8000; done" >/dev/null 2>&1 </dev/null &')
        spec = fcvm("publish", vm, ":8000").stdout.strip()          # a free host port, picked by fcvm
        self.assertRegex(spec, r"^\d+:8000$")
        port = int(spec.split(":")[0])
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            self.assertEqual(s.recv(100).strip(), b"hello-live")
        self.assertEqual(self.inspect(vm)["ports"], [spec])
        self.assertNotEqual(fcvm("publish", vm, spec, check=False).returncode, 0)   # twice: refused
        fcvm("unpublish", vm, str(port))
        self.assertEqual(self.inspect(vm)["ports"], [])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2)
        # on a stopped VM it's kept for the next start
        fcvm("stop", vm)
        port2 = free_port()
        fcvm("publish", vm, f"{port2}:8000")
        fcvm("start", vm)
        self.sh(vm, 'setsid sh -c "echo hello-again | nc -l -p 8000" >/dev/null 2>&1 </dev/null &')
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", port2), timeout=5) as s:
            self.assertEqual(s.recv(100).strip(), b"hello-again")


class Isolation(VMTestCase):
    def test_host_services_are_unreachable(self):
        if setting("NET_HOST_ACCESS") == "1":
            self.skipTest("NET_HOST_ACCESS=1")
        port = free_port()
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        vm = self.vm()
        gw = setting("NET_PREFIX") + ".1"
        r = self.sh(vm, f"nc -z -w 3 {gw} {port} && echo REACHED || echo blocked")
        self.assertEqual(r.stdout.strip(), "blocked")
        self.assertIn("ok", self.sh(vm, f"ping -c1 -W2 {gw} >/dev/null && echo ok").stdout)   # ping is allowed

    def test_vms_cannot_reach_each_other(self):
        if setting("NET_ISOLATE") != "1":
            self.skipTest("NET_ISOLATE=0")
        a, b = self.vm(kind="a"), self.vm(kind="b")
        ip_b = self.inspect(b)["ip"]
        r = self.sh(a, f"ping -c1 -W2 {ip_b} >/dev/null && echo REACHED || echo blocked")
        self.assertEqual(r.stdout.strip(), "blocked")
        # nor routed through the host
        gw = setting("NET_PREFIX") + ".1"
        r = self.sh(a, f"ip route add {ip_b}/32 via {gw}; ping -c1 -W2 {ip_b} >/dev/null && echo REACHED || echo blocked")
        self.assertEqual(r.stdout.strip().splitlines()[-1], "blocked")

    def test_spoofed_source_address_is_dropped(self):
        vm = self.vm()
        spoof = setting("NET_PREFIX") + ".250"
        r = self.sh(vm, f"ip addr add {spoof}/24 dev eth0; "
                        f"nc -s {spoof} -w 3 example.com 80 </dev/null && echo SPOOF-CONNECTED || echo dropped; "
                        "nc -w 3 example.com 80 </dev/null && echo normal-ok")
        self.assertIn("dropped", r.stdout)
        self.assertIn("normal-ok", r.stdout, "the control connection (no spoofing) must work: internet access?")


class Egress(VMTestCase):
    def test_allowlist(self):
        vm = self.vm("--allow", "example.com")
        ok = self.sh(vm, "wget -q -O /dev/null -T 10 https://example.com && echo allowed", check=False)
        self.assertIn("allowed", ok.stdout, ok.stderr)
        denied = self.sh(vm, "wget -q -O /dev/null -T 10 https://www.iana.org && echo ALLOWED || echo denied", check=False)
        self.assertIn("denied", denied.stdout)
        log = strip_ansi(fcvm("egress", vm).stdout + fcvm("egress", vm).stderr)
        self.assertIn("DENY", log)
        self.assertIn("iana.org", log)

    def test_live_allowlist_change(self):
        vm = self.vm("--allow", "example.com")
        fcvm("egress", vm, "--allow", "www.iana.org")
        r = self.sh(vm, "wget -q -O /dev/null -T 10 https://www.iana.org && echo allowed", check=False)
        self.assertIn("allowed", r.stdout)

    def test_mirror_caches_packages(self):
        import json
        if not any(i["name"] == "python-3.13-slim" for i in json.loads(fcvm("images", "--json").stdout)):
            self.skipTest("needs the python-3.13-slim image (pip)")
        r = fcvm("create", "fcvmtest-nocache", "alpine-latest", "--idle", "--cache", check=False)
        self.assertIn("needs --allow", r.stderr)
        vm = self.vm("--allow", "@pypi", "--cache", image="python-3.13-slim", start=True)
        self.assertIn("cache", fcvm("ls").stdout)
        env = self.sh(vm, 'echo "$PIP_INDEX_URL $PIP_TRUSTED_HOST $npm_config_registry"').stdout.split()
        self.assertEqual(env[0], f"http://{setting('NET_R_PREFIX')}.1:{setting('EGRESS_PORT')}/cache/pypi.org/simple/")
        self.assertEqual(env[2], f"http://{setting('NET_R_PREFIX')}.1:{setting('EGRESS_PORT')}/cache/registry.npmjs.org/")
        self.sh(vm, "pip download -q --no-deps -d /tmp/dl six", timeout=300)
        log = strip_ansi(fcvm("egress", vm, "-n", "50").stdout)
        self.assertIn("CACHE-MISS", log)
        self.assertIn("files.pythonhosted.org", log)
        self.sh(vm, "rm -rf /tmp/dl && pip download -q --no-deps -d /tmp/dl six", timeout=300)
        log = strip_ansi(fcvm("egress", vm, "-n", "50").stdout)
        self.assertIn("CACHE-HIT", log)
        self.assertIn("six", self.sh(vm, "ls /tmp/dl").stdout)

    def test_no_network(self):
        vm = self.vm("--net", "none")
        r = self.sh(vm, "ls /sys/class/net")
        self.assertNotIn("eth0", r.stdout.split())        # (lo, and the kernel's inert sit0 stub)


if __name__ == "__main__":
    unittest.main()
