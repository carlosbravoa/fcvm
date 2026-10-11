"""Builds, commit, the MCP server, status."""
import json
import os
import subprocess
import tempfile
import unittest

from fcvmtest import VMTestCase, fcvm, FCVM, IMAGE, PREFIX, jail_usable, strip_ansi


class Build(VMTestCase):
    def test_dockerfile_build_and_run(self):
        ctx = tempfile.mkdtemp(prefix="fcvmtest-ctx-")
        with open(os.path.join(ctx, "Dockerfile"), "w") as f:
            f.write(f'FROM {IMAGE}\nENV GREETING=built\nRUN echo "$GREETING" > /built\nCMD ["cat", "/built"]\n')
        img = self.image_name()
        fcvm("build", "-t", img, "--net", "none", ctx, timeout=600)
        r = fcvm("run", img)
        self.assertIn("built", r.stdout)

    def test_export_and_import_back(self):
        """A committed image, exported as an OCI archive, imports back with its
        content, its config and its owners."""
        import tarfile
        vm = self.vm()
        self.sh(vm, "echo exported > /root/e; adduser -D -u 1234 bob; echo mine > /home/bob/f; chown bob:bob /home/bob/f; "
                    "mkdir -p /opt/x; ln /root/e /opt/x/hard; ln -s /root/e /opt/x/soft")
        fcvm("stop", vm)
        img = self.image_name()
        fcvm("commit", vm, img)
        d = tempfile.mkdtemp(prefix="fcvmtest-export-")
        out = os.path.join(d, "img.tar")
        fcvm("export", img, out, timeout=600)
        with tarfile.open(out) as tf:
            names = tf.getnames()
        self.assertIn("./index.json", names)
        self.assertIn("./oci-layout", names)
        back = self.image_name()
        fcvm("import", f"oci-archive:{out}", back, timeout=600)
        info = next(i for i in json.loads(fcvm("images", "--json").stdout) if i["name"] == back)
        self.assertEqual(info["argv"], ["/bin/sh"])         # the image's own command, not the VM's --idle
        self.assertIsNone(info["parent"])                   # one flat layer
        vm2 = self.vm("--idle", image=back)
        out2 = self.sh(vm2, "cat /root/e /opt/x/hard /opt/x/soft; stat -c '%u %U' /home/bob/f; "
                            "stat -c '%i' /root/e /opt/x/hard").stdout.split("\n")
        self.assertEqual(out2[:4], ["exported", "exported", "exported", "1234 bob"])
        self.assertEqual(out2[4], out2[5])                  # still one inode: the hard link survived

    def test_commit(self):
        vm = self.vm()
        self.sh(vm, "echo committed > /root/c")
        fcvm("stop", vm)
        img = self.image_name()
        fcvm("commit", vm, img)
        r = fcvm("run", img, "--", "cat", "/root/c")
        self.assertIn("committed", r.stdout)


class Mcp(VMTestCase):
    def call(self, proc, i, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def test_sandbox_via_mcp(self):
        env = {**os.environ, "FCVM_MCP_JAIL": "1" if jail_usable() else "0"}
        proc = subprocess.Popen([FCVM, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=env)
        self.addCleanup(proc.kill)
        init = self.call(proc, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "fcvmtest", "version": "0"}})
        self.assertIn("result", init)
        tools = {t["name"] for t in self.call(proc, 2, "tools/list")["result"]["tools"]}
        self.assertTrue({"create_sandbox", "exec", "remove_vm"} <= tools)
        name = f"{PREFIX}mcp-{os.getpid()}"
        self.addCleanup(self.remove_vm, name)
        r = self.call(proc, 3, "tools/call", {"name": "create_sandbox",
                                              "arguments": {"image": IMAGE, "name": name, "network": "none"}})
        self.assertNotIn("error", r, r)
        r = self.call(proc, 4, "tools/call", {"name": "exec", "arguments": {"vm": name, "command": "echo via-mcp"}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("via-mcp", text)
        r = self.call(proc, 5, "tools/call", {"name": "remove_vm", "arguments": {"vm": name}})
        self.assertNotIn("error", r, r)


class Status(VMTestCase):
    def test_status_is_healthy(self):
        r = fcvm("status", "--offline", check=False)
        self.assertEqual(r.returncode, 0, strip_ansi(r.stdout))
        self.assertIn("fcvm ", r.stdout)

    def test_version(self):
        self.assertRegex(fcvm("-V").stdout, r"^fcvm \d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
