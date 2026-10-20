"""The server (fcvm serve / the fcvm service): the HTTP API with bearer tokens,
and the supervisor's restart policies. Uses the running service when there is
one for this state directory, otherwise starts fcvm serve for these tests."""
import json
import unittest

from fcvmtest import VMTestCase, Server, fcvm, IMAGE, wait_for, unique


class Service(VMTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_api_auth(self):
        code, body = self.server.api("GET", "vms")
        self.assertEqual(code, 200)
        self.assertIsInstance(body, list)
        code, _ = self.server.api("GET", "vms", token="wrong")
        self.assertEqual(code, 401)

    def test_error_codes(self):
        code, body = self.server.api("GET", f"vms/{unique('none')}")
        if code == 400 and "code" not in body:
            self.skipTest("the running fcvm service predates error codes: fcvm service install")
        self.assertEqual((code, body["code"]), (404, "vm_not_found"), body)
        vm = self.vm()
        code, body = self.server.api("POST", f"vms/{vm}/start")
        self.assertEqual((code, body["code"]), (409, "vm_running"), body)
        self.assertIn("already running", body["error"])
        code, body = self.server.api("POST", f"vms/{vm}/update", {"ttl": "soon"})
        self.assertEqual((code, body["code"]), (400, "invalid"), body)
        r = fcvm("inspect", unique("none"), check=False)         # the CLI's line for programs
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: vm_not_found")

    def test_events_api(self):
        code, body = self.server.api("GET", "events?since=0&limit=1")
        if code == 404:
            self.skipTest("the running fcvm service predates events: fcvm service install")
        self.assertEqual(code, 200, body)
        vm = self.vm(start=False)
        code, body = self.server.api("GET", f"events?vm={vm}")
        self.assertEqual([e["type"] for e in body["events"]], ["vm.created"])
        self.assertEqual(body["next"], body["events"][-1]["seq"])
        code, body = self.server.api("GET", f"events?vm={vm}&since={body['next']}")
        self.assertEqual((code, body["events"]), (200, []))
        self.assertEqual(self.server.api("GET", "events?since=1&vm=bad/name")[0], 400)

    def test_api_update_restart_policy(self):
        vm = self.vm(start=False)
        code, body = self.server.api("POST", f"vms/{vm}/update", {"restart": "on-failure"})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.inspect(vm)["restart"], "on-failure")
        code, _ = self.server.api("POST", f"vms/{vm}/update", {"restart": "bogus"})
        self.assertEqual(code, 400)

    def test_on_failure_restarts_until_stopped(self):
        vm = self.vm("--restart", "on-failure", cmd=["sh", "-c", "sleep 1; exit 3"])
        seen = set()

        def restarts():
            le = self.inspect(vm).get("last_exit") or {}
            if le.get("code") == 3:
                seen.add(le["at"])
            return len(seen) >= 2
        wait_for(restarts, timeout=45, what="two failed runs, restarted by the supervisor")
        fcvm("stop", vm)
        at = self.inspect(vm)["last_exit"]["at"]
        wait_for(lambda: self.inspect(vm)["state"] != "running", 10, what="the stop")
        import time
        time.sleep(6)                                     # longer than the supervisor's backoff so far
        info = self.inspect(vm)
        self.assertNotEqual(info["state"], "running")
        self.assertTrue(info["stopped_by_user"])
        self.assertLessEqual(info["last_exit"]["at"], at + 5)

    def test_clean_exit_is_not_restarted(self):
        vm = self.vm("--restart", "on-failure", cmd=["true"])
        wait_for(lambda: (self.inspect(vm).get("last_exit") or {}).get("code") == 0, 20, what="the exit")
        import time
        time.sleep(5)
        self.assertNotEqual(self.inspect(vm)["state"], "running")

    def new_api(self, path):
        code, body = self.server.api("GET", path)
        if code == 404:
            self.skipTest("the running fcvm service predates this code: fcvm service install")
        return code, body

    def test_guest_metrics_and_processes(self):
        vm = self.vm()
        self.new_api("templates")
        body = wait_for(lambda: (lambda r: r[1] if r[0] == 200 else None)(self.server.api("GET", f"vms/{vm}/processes")),
                        30, what="the guest's processes")      # once the exec agent is up
        self.assertTrue(any(p["fcvm"] == "fc-init (init)" for p in body["processes"]))
        self.assertGreater(body["mem_total"], 0)
        # the sampler's guest readings (every 10 s)
        guest = wait_for(lambda: self.server.api("GET", f"vms/{vm}/stats")[1]["guest"], 30, what="guest samples")
        self.assertGreater(guest[-1]["g_mem_total"], 0)
        code, body = self.server.api("GET", f"vms/{vm}/stats?range=1h")
        self.assertEqual((code, body["span"]), (200, 3600))

    def test_prometheus(self):
        vm = self.vm()
        self.new_api("templates")
        text = wait_for(lambda: (lambda r: r[1] if f'fcvm_vm_up{{vm="{vm}"' in r[1] else None)(
            self.server.raw("GET", "/metrics")), 15, what="the VM in /metrics")
        self.assertIn("# TYPE fcvm_vm_cpu_seconds_total counter", text)
        self.assertEqual(self.server.raw("GET", "/metrics", token="wrong")[0], 401)

    def test_templates_api(self):
        self.new_api("templates")
        name = unique("tpl")
        self.addCleanup(fcvm, "template", "rm", name, check=False)
        code, body = self.server.api("POST", "templates", {"name": name, "image": IMAGE, "network": "restricted"})
        self.assertEqual(code, 400)                                   # an allowlist is required
        code, body = self.server.api("POST", "templates", {"name": name, "image": IMAGE, "mem_mib": 256})
        self.assertEqual((code, body["mem_mib"]), (200, 256), body)
        self.assertIn(name, [t["name"] for t in self.server.api("GET", "templates")[1]])
        self.assertEqual(self.server.api("DELETE", "templates/python-sandbox")[0], 400)   # built in
        self.assertEqual(self.server.api("DELETE", f"templates/{name}")[0], 200)

    def test_ttl_stops_the_vm(self):
        vm = self.vm("--ttl", "4s")
        self.new_api("templates")
        info = self.inspect(vm)
        self.assertEqual(info["ttl_s"], 4)
        self.assertGreater(info["expires_at"], 0)
        self.assertIn("ttl:4s", fcvm("ls").stdout)
        wait_for(lambda: self.inspect(vm)["state"] != "running", 30, what="the TTL to stop the VM")
        info = self.inspect(vm)
        self.assertEqual((info["expired"], info["stopped_by_user"]), ("ttl", True))
        fcvm("start", vm)                                     # a new life, the marker gone
        self.assertIsNone(self.inspect(vm)["expired"])
        fcvm("update", vm, "--ttl", "none", "--idle-stop", "1h")
        info = self.inspect(vm)
        self.assertEqual((info.get("ttl_s"), info["idle_stop_s"]), (None, 3600))

    def test_idle_stop_counts_from_the_last_use(self):
        import time
        vm = self.vm("--idle-stop", "5s")
        self.new_api("templates")
        for _ in range(4):                                    # kept alive by use for longer than the idle time
            time.sleep(2)
            self.sh(vm, "true")
        self.assertEqual(self.inspect(vm)["state"], "running")
        wait_for(lambda: self.inspect(vm)["state"] != "running", 30, what="the idle stop")
        self.assertEqual(self.inspect(vm)["expired"], "idle")

    def test_ttl_through_the_api_and_a_template(self):
        self.new_api("templates")
        name = unique("tpl")
        self.addCleanup(fcvm, "template", "rm", name, check=False)
        code, body = self.server.api("POST", "templates", {"name": name, "image": IMAGE, "ttl": "3h", "idle_stop": "20m"})
        self.assertEqual((code, body["ttl"], body["idle_stop"]), (200, "3h", "20m"), body)
        vm = unique("vm")
        self.addCleanup(self.remove_vm, vm)
        code, body = self.server.api("POST", "vms", {"name": vm, "image": IMAGE, "idle": True, "start": False,
                                                      "ttl": "3h", "idle_stop": "20m"})
        self.assertEqual((code, body["ttl_s"], body["idle_stop_s"]), (200, 10800, 1200), body)
        code, body = self.server.api("POST", f"vms/{vm}/update", {"idle_stop": None})
        self.assertEqual((code, body.get("idle_stop_s")), (200, None), body)
        self.assertEqual(self.server.api("POST", f"vms/{vm}/update", {"ttl": "soon"})[0], 400)

    def test_exec_api_and_jobs(self):
        vm = self.vm()
        self.new_api("templates")
        code, body = self.server.api("POST", f"vms/{vm}/exec", {"command": "echo $X; echo e >&2; exit 3", "env": {"X": "hi"}})
        self.assertEqual((code, body["exit_code"], body["stdout"].strip(), body["stderr"].strip()), (200, 3, "hi", "e"), body)
        code, body = self.server.api("POST", f"vms/{vm}/exec", {"argv": ["wc", "-c"], "stdin": "12345"})
        self.assertEqual((code, body["stdout"].strip()), (200, "5"), body)
        self.assertEqual(self.server.api("POST", f"vms/{vm}/exec", {})[0], 400)
        code, body = self.server.api("POST", f"vms/{vm}/exec", {"command": "echo bg; sleep 60", "background": True})
        self.assertEqual(code, 200, body)
        job = body["job"]
        code, body = wait_for(lambda: (lambda r: r if r[1].get("stdout", "").strip() == "bg" else None)(
            self.server.api("GET", f"vms/{vm}/jobs/{job}")), 15, what="the job's output")
        self.assertEqual(body["status"], "running")
        self.assertEqual(self.server.api("DELETE", f"vms/{vm}/jobs/{job}")[0], 200)
        wait_for(lambda: self.server.api("GET", f"vms/{vm}/jobs/{job}")[1]["status"] == "lost", 10, what="the kill")
        self.assertEqual(self.server.api("GET", f"vms/{vm}/jobs")[1][0]["id"], job)

    def test_ports_api(self):
        vm = self.vm()
        self.new_api("templates")
        code, body = self.server.api("POST", f"vms/{vm}/ports", {"port": ":8000"})
        self.assertEqual(code, 200, body)
        spec = body["ports"][-1]
        self.assertRegex(spec, r"^\d+:8000$")
        self.assertEqual(self.server.api("POST", f"vms/{vm}/ports", {"port": "nope"})[0], 400)
        code, body = self.server.api("DELETE", f"vms/{vm}/ports?port={spec.split(':')[0]}")
        self.assertEqual((code, body["ports"]), (200, []), body)

    def test_warm_pool(self):
        self.new_api("pools")
        tpl, pool = unique("tpl"), unique("pool")
        self.addCleanup(fcvm, "template", "rm", tpl, check=False)
        self.addCleanup(fcvm, "pool", "rm", pool, check=False)
        fcvm("template", "save", tpl, IMAGE, "--mem", "256", "--idle")
        code, body = self.server.api("POST", "pools", {"name": pool, "template": tpl, "size": 2})
        self.assertEqual((code, body["size"], body["template"]), (200, 2, tpl), body)
        r = fcvm("pool", "claim", pool, check=False)                           # nothing ready yet
        self.assertIn("no VM ready", r.stderr)

        def ready():
            p = next((p for p in json.loads(fcvm("pool", "ls", "--json").stdout) if p["name"] == pool), None)
            return p and p["ready"] == 2
        wait_for(ready, 60, what="the pool to fill")
        self.assertIn(f"pool:{pool}", fcvm("ls").stdout)
        vm = fcvm("pool", "claim", pool, "--ttl", "1h").stdout.strip()
        self.addCleanup(self.remove_vm, vm)
        info = self.inspect(vm)
        self.assertEqual((info["state"], info["pooled"], info["ttl_s"]), ("running", None, 3600))
        self.assertGreater(info["claimed_at"], 0)
        self.assertEqual(self.sh(vm, "echo ok").stdout.strip(), "ok")
        wait_for(ready, 60, what="the pool to refill")
        code, body = self.server.api("POST", f"pools/{pool}/claim", {})      # the API claims too
        self.assertEqual((code, body["state"]), (200, "running"), body)
        self.addCleanup(self.remove_vm, body["name"])
        self.assertNotEqual(body["name"], vm)
        fcvm("pool", "rm", pool)
        left = [v["name"] for v in json.loads(fcvm("ls", "--json").stdout) if v.get("pooled") == pool]
        self.assertEqual(left, [])
        self.assertEqual(self.inspect(vm)["state"], "running")               # claimed ones stay

    def test_python_sdk(self):
        import os
        import sys
        self.new_api("pools")
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sdk", "python"))
        import fcvm as sdk
        c = sdk.Client(self.server.url, self.server.token)
        self.assertIn("hostname", c.host())
        name = unique("sdk")
        self.addCleanup(self.remove_vm, name)
        with c.create(name, image=IMAGE, idle=True, mem_mib=256, ttl="1h") as sb:
            self.assertEqual(sb.state, "running")
            self.assertEqual(sb.run("echo hi").strip(), "hi")
            r = sb.exec(argv=["sh", "-c", "echo e >&2; exit 2"])
            self.assertEqual((r.exit_code, r.stderr.strip()), (2, "e"))
            sb.write_file("/tmp/f.txt", "payload")
            self.assertEqual(sb.read_text("/tmp/f.txt"), "payload")
            self.assertIn("f.txt", [e["name"] for e in sb.ls("/tmp")])
            job = sb.exec("echo bg; sleep 60", background=True)
            wait_for(lambda: "bg" in job.status()["stdout"], 15, what="the job's output")
            job.kill()
            self.assertEqual(job.wait(timeout=15)["status"], "lost")
            sb.exec('setsid sh -c "echo sdk-port | nc -l -p 8000" >/dev/null 2>&1 </dev/null &')
            port = sb.publish(8000)
            import socket
            import time
            time.sleep(0.5)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                self.assertEqual(s.recv(100).strip(), b"sdk-port")
            self.assertEqual(sb.info()["ttl_s"], 3600)
            sb.update(ttl=None)
            self.assertIsNone(sb.info().get("ttl_s"))
        self.assertNotIn(name, [v.name for v in c.vms()])                 # removed by the context manager

    def test_node_sdk(self):
        import os
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node isn't installed")
        self.new_api("pools")
        sdk_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sdk", "typescript")
        name = unique("sdk")
        self.addCleanup(self.remove_vm, name)
        script = f'''
import {{ Client }} from "{sdk_dir}/fcvm.mjs";
const c = new Client({{ url: process.env.U, token: process.env.T }});
const sb = await c.create("{name}", {{ image: "{IMAGE}", idle: true, mem_mib: 256 }});
const r = await sb.exec("echo node-hi");
await sb.writeFile("/tmp/n.txt", "from-node");
const t = await sb.readText("/tmp/n.txt");
const job = await sb.execBackground("sleep 60");
await job.kill();
const st = await job.wait({{ timeout: 15000 }});
await sb.remove();
console.log(JSON.stringify({{ out: r.stdout.trim(), code: r.exit_code, t, st: st.status, left: (await c.vms()).some((v) => v.name === "{name}") }}));
'''
        r = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=120,
                           env={**os.environ, "U": self.server.url, "T": self.server.token})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                         {"out": "node-hi", "code": 0, "t": "from-node", "st": "lost", "left": False})

    def test_throwaway_vms_take_no_policy(self):
        r = fcvm("run", IMAGE, "--restart", "always", "--", "true", check=False)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
