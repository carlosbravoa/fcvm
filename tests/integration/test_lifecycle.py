"""VM lifecycle: run, create/start/stop/rm, exec, cp, inspect."""
import json
import os
import re
import subprocess
import tempfile
import unittest

from fcvmtest import VMTestCase, fcvm, IMAGE, unique, wait_for, VMS, STATE


class Run(VMTestCase):
    def test_exit_code_and_output(self):
        r = fcvm("run", IMAGE, "--", "sh", "-c", "echo out-marker; exit 3", check=False)
        self.assertEqual(r.returncode, 3)
        self.assertIn("out-marker", r.stdout)

    def test_throwaway_vm_is_deleted(self):
        before = {v["name"] for v in json.loads(fcvm("ls", "--json").stdout)}
        fcvm("run", IMAGE, "--", "true")
        after = {v["name"] for v in json.loads(fcvm("ls", "--json").stdout)}
        self.assertEqual(before, after)


class Lifecycle(VMTestCase):
    def test_create_start_exec_stop_rm(self):
        vm = self.vm()
        info = self.inspect(vm)
        self.assertEqual(info["state"], "running")
        self.assertTrue(info["ip"])
        self.assertEqual(fcvm("exec", vm, "--", "echo", "hi").stdout.strip(), "hi")
        again = fcvm("start", vm, check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already running", again.stderr)
        fcvm("stop", vm)
        info = self.inspect(vm)
        self.assertIn(info["state"], ("stopped", "exited"))   # an app VM shows its exit, like docker stop
        self.assertTrue(info["stopped_by_user"])
        fcvm("rm", vm)
        self.assertNotEqual(fcvm("inspect", vm, check=False).returncode, 0)

    def test_restart_keeps_the_disk(self):
        vm = self.vm()
        self.sh(vm, "echo kept > /root/f")
        fcvm("stop", vm)
        fcvm("start", vm)
        self.assertEqual(self.sh(vm, "cat /root/f").stdout.strip(), "kept")


class Exec(VMTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.name = unique("exec")
        fcvm("create", cls.name, IMAGE, "--idle")
        fcvm("start", cls.name)

    @classmethod
    def tearDownClass(cls):
        fcvm("stop", cls.name, check=False)
        fcvm("rm", cls.name, check=False)

    def test_separate_streams_and_status(self):
        r = fcvm("exec", self.name, "--", "sh", "-c", "echo o; echo e >&2; exit 7", check=False)
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (7, "o", "e"))

    def test_user_workdir_env(self):
        out = fcvm("exec", "-u", "nobody", "-w", "/tmp", "-e", "X=42", self.name, "--",
                   "sh", "-c", 'echo "$(id -un) $(pwd) $X"').stdout.strip()
        self.assertEqual(out, "nobody /tmp 42")

    def test_timeout(self):
        r = fcvm("exec", "--timeout", "1", self.name, "--", "sleep", "30", check=False)
        self.assertEqual(r.returncode, 124)

    def test_stdin(self):
        r = fcvm("exec", "-i", self.name, "--", "wc", "-c", input="12345")
        self.assertEqual(r.stdout.strip(), "5")

    def test_background_jobs(self):
        import time
        job = fcvm("exec", "-d", self.name, "--", "sh", "-c", "echo started; sleep 1; echo err >&2; exit 4").stdout.strip()
        self.assertRegex(job, r"^[0-9a-f]{6}$")
        jobs = json.loads(fcvm("jobs", self.name, "--json").stdout)
        mine = next(j for j in jobs if j["id"] == job)
        self.assertEqual((mine["status"], mine["command"][-1]), ("running", "echo started; sleep 1; echo err >&2; exit 4"))
        wait_for(lambda: next(j for j in json.loads(fcvm("jobs", self.name, "--json").stdout) if j["id"] == job)["status"] == "exited",
                 20, what="the job to end")
        r = fcvm("jobs", self.name, job, check=False)                     # its output, and its status as the exit code
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip()), (4, "started", "err"))
        self.assertIn("exited(4)", fcvm("jobs", self.name).stdout)
        # a long one, killed
        job = fcvm("exec", "-d", self.name, "--", "sh", "-c", "sleep 300").stdout.strip()
        time.sleep(0.5)
        fcvm("jobs", self.name, job, "--kill")
        wait_for(lambda: next(j for j in json.loads(fcvm("jobs", self.name, "--json").stdout) if j["id"] == job)["status"] == "lost",
                 10, what="the killed job to be lost")
        self.assertNotIn("sleep 300", self.sh(self.name, "ps").stdout)        # its process group went with it
        fcvm("jobs", self.name, "--clear")
        self.assertEqual(json.loads(fcvm("jobs", self.name, "--json").stdout), [])

    def test_cp_roundtrip(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-cp-")
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("payload")
        fcvm("cp", os.path.join(d, "a.txt"), f"{self.name}:/tmp/a.txt")
        out = os.path.join(d, "back.txt")
        fcvm("cp", f"{self.name}:/tmp/a.txt", out)
        with open(out) as f:
            self.assertEqual(f.read(), "payload")


class Secrets(VMTestCase):
    def test_secrets_reach_the_guest_and_not_its_disk(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-secret-")
        with open(os.path.join(d, "key.pem"), "w") as f:
            f.write("---KEY---\n")
        vm = self.vm("--secret", "API_KEY=sk-123", "--secret", f"TLS_KEY=@{d}/key.pem", "--secret", "FROM_ENV",
                     start=False)   # (create with FROM_ENV from the environment)
        self.assertEqual(sorted(self.inspect(vm)["secrets"]), ["API_KEY", "FROM_ENV", "TLS_KEY"])
        self.assertEqual(oct(os.stat(os.path.join(VMS, vm, "secrets.json")).st_mode & 0o777), "0o600")
        fcvm("start", vm)
        out = self.sh(vm, 'cat /run/secrets/API_KEY; echo; cat /run/secrets/TLS_KEY; echo "$API_KEY $TLS_KEY $FROM_ENV"; '
                          'stat -c "%a %U" /run/secrets/API_KEY; grep secrets /proc/mounts | cut -d" " -f3').stdout
        self.assertEqual(out.splitlines(), ["sk-123", "---KEY---", "sk-123 ---KEY--- env-value", "400 root", "tmpfs"])
        # a non-root exec session can't read another user's secrets... but as root it can; the files aren't on disk
        fcvm("update", vm, "--unset-secret", "TLS_KEY", "--secret", "API_KEY=sk-456")
        self.assertEqual(sorted(self.inspect(vm)["secrets"]), ["API_KEY", "FROM_ENV"])
        fcvm("stop", vm)
        img = self.image_name()
        fcvm("commit", vm, img)
        r = subprocess.run(["debugfs", "-R", "ls -p /upper/run/secrets", os.path.join(STATE, "images", f"{img}.ext4")],
                           capture_output=True, text=True)
        entries = [l.split("/")[5] for l in r.stdout.splitlines() if l.count("/") >= 6]
        self.assertEqual([e for e in entries if e not in (".", "..")], [])       # the mount point only, no files
        fcvm("start", vm)
        out = self.sh(vm, 'echo "$API_KEY"; ls /run/secrets').stdout.split()
        self.assertEqual(out, ["sk-456", "API_KEY", "FROM_ENV"])

    def vm(self, *opts, **kw):
        os.environ["FROM_ENV"] = "env-value"
        try:
            return super().vm(*opts, **kw)
        finally:
            del os.environ["FROM_ENV"]

    def test_bad_specs(self):
        for spec in ("1BAD=x", "A-B=x", "NOT_SET_ANYWHERE_XYZ", "F=@/nonexistent"):
            r = fcvm("create", unique("vm"), IMAGE, "--idle", "--secret", spec, check=False)
            self.assertNotEqual(r.returncode, 0, spec)


class Forward(VMTestCase):
    """--forward: the host's ssh-agent and git credentials, used from the guest."""

    def test_git_credentials(self):
        d = tempfile.mkdtemp(prefix="fcvmtest-fwd-")
        with open(os.path.join(d, "creds"), "w") as f:
            f.write("https://bob:hunter2@example.test\n")
        subprocess.run(["git", "config", "--file", os.path.join(d, "gitconfig"), "credential.helper",
                        f"store --file={d}/creds"], check=True)
        name = unique("vm")
        fcvm("create", name, IMAGE, "--idle", "--forward", "git")
        self.addCleanup(self.remove_vm, name)
        self.assertEqual(self.inspect(name)["forward"], ["git"])
        self.assertIn("forward:git", fcvm("ls").stdout)
        # the relay inherits the environment of whoever starts the VM: start it here, not through the service
        fcvm("start", name, env={"GIT_CONFIG_GLOBAL": f"{d}/gitconfig", "GIT_CONFIG_NOSYSTEM": "1", "FCVM_DAEMON": "1"})
        # what the guest's git would run, and the variables that make it run it
        out = self.sh(name, 'printf "protocol=https\\nhost=example.test\\n\\n" | /.fcvm/bin/fc-init --git-credential get; '
                            'echo "$GIT_CONFIG_KEY_0=$GIT_CONFIG_VALUE_0"').stdout
        self.assertIn("username=bob\npassword=hunter2\n", out)
        self.assertIn("credential.helper=/.fcvm/bin/fc-init --git-credential", out)
        out = self.sh(name, 'printf "protocol=https\\nhost=nowhere.test\\n\\n" | /.fcvm/bin/fc-init --git-credential get').stdout
        self.assertEqual(out.strip(), "")
        self.sh(name, 'printf "protocol=https\\nhost=example.test\\n\\n" | /.fcvm/bin/fc-init --git-credential store')
        with open(os.path.join(VMS, name, "forward.log")) as f:
            log = f.read()
        self.assertIn("https://example.test: found", log)
        self.assertIn("https://nowhere.test: none", log)
        self.assertNotIn("store", log)

    def test_ssh_agent(self):
        if not any(i["name"] == "python-3.13-slim" for i in json.loads(fcvm("images", "--json").stdout)):
            self.skipTest("needs the python-3.13-slim image (to talk the agent protocol from the guest)")
        d = tempfile.mkdtemp(prefix="fcvmtest-agent-")
        agent = subprocess.run(["ssh-agent", "-s", "-a", f"{d}/agent.sock"], capture_output=True, text=True, check=True)
        pid = int(re.search(r"SSH_AGENT_PID=(\d+)", agent.stdout).group(1))
        self.addCleanup(os.kill, pid, 15)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fcvmtest-key", "-f", f"{d}/key"], check=True)
        subprocess.run(["ssh-add", "-q", f"{d}/key"], env={**os.environ, "SSH_AUTH_SOCK": f"{d}/agent.sock"}, check=True)
        name = unique("vm")
        fcvm("create", name, "python-3.13-slim", "--idle", "--forward", "ssh-agent",
             env={"SSH_AUTH_SOCK": f"{d}/agent.sock"})
        self.addCleanup(self.remove_vm, name)
        fcvm("start", name, env={"SSH_AUTH_SOCK": f"{d}/agent.sock", "FCVM_DAEMON": "1"})
        script = ("import os, socket, struct\n"
                  "s = socket.socket(socket.AF_UNIX); s.connect(os.environ['SSH_AUTH_SOCK'])\n"
                  "s.sendall(struct.pack('>IB', 1, 11))\n"                     # SSH_AGENTC_REQUEST_IDENTITIES
                  "n = struct.unpack('>I', s.recv(4))[0]; d = b''\n"
                  "while len(d) < n: d += s.recv(n - len(d))\n"
                  "print(d[0], struct.unpack('>I', d[1:5])[0], b'fcvmtest-key' in d)\n")
        out = fcvm("exec", "-i", name, "--", "python3", "-", input=script).stdout.strip()
        self.assertEqual(out, "12 1 True")                                   # SSH_AGENT_IDENTITIES_ANSWER, our one key


class Names(VMTestCase):
    def test_too_long_for_sockets(self):
        r = fcvm("create", "fcvmtest-" + "x" * 110, IMAGE, "--idle", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("too long", r.stderr)


class NoAgent(VMTestCase):
    """create --no-agent: asks first, boots without the exec agent, and the
    commands that need it refuse clearly."""

    def test_needs_confirmation(self):
        r = fcvm("create", unique("vm"), IMAGE, "--no-agent", "--idle", check=False, input="")   # no terminal to ask
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("You lose", r.stderr)
        self.assertIn("needs confirmation", r.stderr)

    def test_runs_without_the_agent(self):
        name = unique("vm")
        fcvm("create", name, IMAGE, "--no-agent", "--", "sh", "-c", "echo no-agent-ran; sleep 60",
             env={"FCVM_NO_AGENT_OK": "1"})
        self.addCleanup(self.remove_vm, name)
        fcvm("start", name)
        self.assertFalse(self.inspect(name)["agent"])
        self.assertIn("no-agent", fcvm("ls").stdout)
        wait_for(lambda: "no-agent-ran" in fcvm("logs", name).stdout, 20, what="the app's output")
        for args in (["exec", name, "true"], ["cp", f"{name}:/etc/hostname", "/tmp/x"],
                     ["snapshot", name, unique("snap")]):
            r = fcvm(*args, check=False)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("--no-agent", r.stderr)
        self.assertIn("fcvm.agent=0", open(os.path.join(VMS, name, "fc.json")).read())


if __name__ == "__main__":
    unittest.main()
