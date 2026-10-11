"""Jailed VMs: Firecracker under its own uid, in a chroot and network namespace."""
import os
import unittest

from fcvmtest import VMTestCase, fcvm, VMS, jail_usable


@unittest.skipUnless(jail_usable(), "fcvm-jaild isn't set up for this state directory")
class Jail(VMTestCase):
    def test_jailed_vm(self):
        vm = self.vm("--jail")
        pid = self.inspect(vm)["pid"]
        with open(f"/proc/{pid}/status") as f:
            uid = int(next(l for l in f if l.startswith("Uid:")).split()[1])
        self.assertGreaterEqual(uid, 900000)
        with open(f"/proc/{pid}/net/dev") as f:           # its own network namespace
            ifaces = {l.split(":")[0].strip() for l in f.readlines()[2:]}
        self.assertEqual(ifaces, {"lo", "veth0", "br0", "tap0"})
        self.assertEqual(fcvm("exec", vm, "--", "echo", "inside").stdout.strip(), "inside")
        self.assertTrue(os.path.islink(os.path.join(VMS, vm, "fc.sock")))   # links into the chroot

    def test_secrets_and_live_ports_in_a_jail(self):
        """The secrets server and the port forwarder work through the chroot's sockets."""
        import socket
        import time
        vm = self.vm("--jail", "--secret", "TOKEN=jailed-secret")
        self.assertEqual(fcvm("exec", vm, "--", "sh", "-c", "cat /run/secrets/TOKEN; echo; echo $TOKEN").stdout.split(),
                         ["jailed-secret", "jailed-secret"])
        fcvm("exec", vm, "--", "sh", "-c", 'setsid sh -c "echo from-jail | nc -l -p 8000" >/dev/null 2>&1 </dev/null &')
        spec = fcvm("publish", vm, ":8000").stdout.strip()
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", int(spec.split(":")[0])), timeout=5) as s:
            self.assertEqual(s.recv(100).strip(), b"from-jail")

    def test_forwarding_and_balloon_in_a_jail(self):
        """The git credential relay (vsock 9002) and the balloon device, through the chroot."""
        import json
        import subprocess
        import tempfile
        from fcvmtest import wait_for
        d = tempfile.mkdtemp(prefix="fcvmtest-jfwd-")
        with open(os.path.join(d, "creds"), "w") as f:
            f.write("https://jo:sesame@example.test\n")
        subprocess.run(["git", "config", "--file", os.path.join(d, "gitconfig"), "credential.helper",
                        f"store --file={d}/creds"], check=True)
        vm = self.vm("--jail", "--mem", "512", "--forward", "git", start=False)
        fcvm("start", vm, env={"GIT_CONFIG_GLOBAL": f"{d}/gitconfig", "GIT_CONFIG_NOSYSTEM": "1", "FCVM_DAEMON": "1"})
        out = self.sh(vm, 'printf "protocol=https\\nhost=example.test\\n\\n" | /.fcvm/bin/fc-init --git-credential get').stdout
        self.assertIn("username=jo\npassword=sesame\n", out)
        fcvm("balloon", vm, "128")
        wait_for(lambda: json.loads(fcvm("balloon", vm).stdout)["balloon_mib"] == 128, 20, what="the jailed balloon")
        self.assertEqual(self.inspect(vm)["balloon_mib"], 128)

    def test_mirror_in_a_jail(self):
        """A jailed restricted VM reaches the mirror on the host side of its veth."""
        import json
        from fcvmtest import strip_ansi
        if not any(i["name"] == "python-3.13-slim" for i in json.loads(fcvm("images", "--json").stdout)):
            self.skipTest("needs the python-3.13-slim image (pip)")
        vm = self.vm("--jail", "--allow", "@pypi", "--cache", image="python-3.13-slim")
        self.sh(vm, "pip download -q --no-deps -d /tmp/dl six", timeout=300)
        log = strip_ansi(fcvm("egress", vm, "-n", "50").stdout)
        self.assertTrue("CACHE-MISS" in log or "CACHE-HIT" in log, log)
        self.assertIn("six", self.sh(vm, "ls /tmp/dl").stdout)

    def test_jailed_pool(self):
        """A pool of jailed VMs: filled by the supervisor, claimed with the jail intact."""
        import json
        from fcvmtest import Server, unique, wait_for
        server = Server()
        self.addCleanup(server.close)
        tpl, pool = unique("tpl"), unique("pool")
        self.addCleanup(fcvm, "template", "rm", tpl, check=False)
        self.addCleanup(fcvm, "pool", "rm", pool, check=False)
        fcvm("template", "save", tpl, "alpine-latest", "--mem", "256", "--idle")
        fcvm("pool", "set", pool, "--template", tpl, "--size", "1", "--jail")
        wait_for(lambda: next((p["ready"] for p in json.loads(fcvm("pool", "ls", "--json").stdout) if p["name"] == pool), 0) == 1,
                 60, what="the jailed pool to fill")
        vm = fcvm("pool", "claim", pool).stdout.strip()
        self.addCleanup(self.remove_vm, vm)
        info = self.inspect(vm)
        self.assertTrue(info.get("jail"), info)
        pid = info["pid"]
        with open(f"/proc/{pid}/status") as f:
            uid = int(next(l for l in f if l.startswith("Uid:")).split()[1])
        self.assertGreaterEqual(uid, 900000)
        self.assertEqual(self.sh(vm, "echo claimed").stdout.strip(), "claimed")

    def test_cleanup_after_stop(self):
        vm = self.vm("--jail")
        fcvm("stop", vm)
        self.assertFalse(os.path.exists(f"/srv/jailer/firecracker/{vm}"))


if __name__ == "__main__":
    unittest.main()
