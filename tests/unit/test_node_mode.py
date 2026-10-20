"""Node mode (NODE=1, docs/configuration.md): what lib/common.sh pins and
refuses, and the same rule in lib/build.py."""
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, bash
import templates

BUILD_PY = os.path.join(ROOT, "lib", "build.py")


class Settings(unittest.TestCase):
    def test_node_mode_pins_the_settings(self):
        r = bash('echo "$NODE $NODE_ADMIT $JAIL $NET_ISOLATE $NET_HOST_ACCESS $NODE_NETWORKS ${NODE_PATHS#$FCVM_HOME/}"',
                 env={"NODE": "1", "NET_HOST_ACCESS": "1", "NET_ISOLATE": "0", "JAIL": "0", "NODE_ADMIT": "0"})
        self.assertEqual(r.stdout.split(), ["1", "1", "1", "1", "0", "full,restricted,none", "builds"])
        r = bash('echo "$NODE $NODE_ADMIT ${JAIL:-unset} $NET_ISOLATE $NET_HOST_ACCESS"', env={"NET_HOST_ACCESS": "1"})
        self.assertEqual(r.stdout.split(), ["0", "0", "unset", "1", "1"])                 # off: nothing pinned

    def test_refusals(self):
        r = bash('node_refuse "no way"; echo fine', env={"NODE": "1"})
        self.assertEqual((r.returncode, r.stderr.splitlines()[-1]), (1, "fcvm-error: policy"))
        self.assertIn("node mode (NODE=1): no way", r.stderr)
        r = bash('node_refuse "no way"; echo fine')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "fine"))

    def test_host_paths_only_under_node_paths(self):
        home = tempfile.mkdtemp(prefix="fcvm-unit-")
        os.makedirs(os.path.join(home, "builds", "p"))
        os.makedirs(os.path.join(home, "other"))
        os.symlink("/etc", os.path.join(home, "builds", "link"))
        cases = [(os.path.join(home, "builds", "p", "Dockerfile"), 0), (os.path.join(home, "builds"), 0),   # the directory itself too
                 (os.path.join(home, "other", "x"), 1), ("/etc/passwd", 1), (os.path.join(home, "builds", "link", "passwd"), 1),  # resolved
                 (os.path.join(home, "builds", "p", "..", "p", "f"), 0), (os.path.join(home, "buildsX"), 1)]
        for path, rc in cases:
            r = bash(f'host_path_ok "{path}" "a test"; echo ok', home=home, env={"NODE": "1"})
            self.assertEqual(r.returncode, rc, (path, r.stderr))
            if rc:
                self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: policy")
                self.assertIn("is not under NODE_PATHS", r.stderr)
        r = bash(f'host_path_ok "{home}/other/x"; echo ok', home=home, env={"NODE": "1", "NODE_PATHS": f"{home}/builds:{home}/other"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))
        r = bash('host_path_ok /etc/passwd; echo ok', home=home)                        # not in node mode: anything goes
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"))

    def test_build_context_follows_the_same_rule(self):
        home = tempfile.mkdtemp(prefix="fcvm-unit-")
        os.makedirs(os.path.join(home, "builds", "ctx"))
        os.makedirs(os.path.join(home, "elsewhere"))
        env = {**os.environ, "FCVM_HOME": home, "NODE": "1", "NODE_PATHS": os.path.join(home, "builds")}
        r = subprocess.run([sys.executable, BUILD_PY, "-t", "x", os.path.join(home, "elsewhere")], capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stderr.splitlines()[-1]), (1, "fcvm-error: policy"), r.stderr)
        r = subprocess.run([sys.executable, BUILD_PY, "-t", "x", os.path.join(home, "builds", "ctx")], capture_output=True, text=True, env=env)
        self.assertIn("no Fcvmfile or Dockerfile", r.stderr)                            # past the policy: the usual checks
        r = subprocess.run([sys.executable, BUILD_PY, "-t", "x", "-f", "/etc/passwd", os.path.join(home, "builds", "ctx")],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: policy")


class RestrictedWithoutAllowlist(unittest.TestCase):
    def test_template_options(self):
        t = templates.validate({"image": "alpine-latest", "network": "restricted"})
        self.assertEqual(t["allow"], [])
        self.assertEqual(templates.options(t)[-2:], ["--net", "restricted"])
        t = templates.validate({"image": "alpine-latest", "network": "restricted", "allow": ["@pypi"], "cache": True})
        self.assertEqual(templates.options(t)[-5:], ["--net", "restricted", "--allow", "@pypi", "--cache"])


if __name__ == "__main__":
    unittest.main()
