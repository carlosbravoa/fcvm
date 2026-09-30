"""The installer and upgrades (install.sh, lib/upgrade.sh), with releases
made from this tree and everything in temporary directories."""
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import ROOT


def release(dest, version, extra_init_line=""):
    """A release directory of this tree (tracked and new files), as VERSION."""
    files = subprocess.run(["git", "-C", ROOT, "ls-files", "-co", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.split()
    d = os.path.join(dest, f"fcvm-{version}")
    for f in files:
        src = os.path.join(ROOT, f)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
            shutil.copy2(src, os.path.join(d, f))
    with open(os.path.join(d, "VERSION"), "w") as f:
        f.write(version + "\n")
    if extra_init_line:
        with open(os.path.join(d, "init/fc-init.c"), "a") as f:
            f.write(extra_init_line + "\n")
    return d


@unittest.skipUnless(os.path.isdir(os.path.join(ROOT, ".git")), "needs a git checkout")
class Install(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="fcvm-inst-")
        self.lib, self.bin = os.path.join(self.t, "lib"), os.path.join(self.t, "bin")
        self.env = {**os.environ, "FCVM_LIB_DIR": self.lib, "FCVM_BIN_DIR": self.bin,
                    "XDG_DATA_HOME": os.path.join(self.t, "data"), "XDG_CONFIG_HOME": os.path.join(self.t, "cfg")}
        self.env.pop("FCVM_HOME", None)
        self.env.pop("FCVM_CONF", None)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_(self, *cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def fcvm(self, *args):
        return self.run_(os.path.join(self.bin, "fcvm"), *args).stdout.strip()

    def test_install_upgrade_rollback(self):
        rels = {v: release(self.t, v) for v in ("0.9.0", "0.9.1", "0.10.0")}
        self.run_("sh", os.path.join(ROOT, "install.sh"), "--source", rels["0.9.0"])
        self.assertEqual(os.readlink(os.path.join(self.lib, "current")), "0.9.0")
        self.assertEqual(self.fcvm("-V"), "fcvm 0.9.0")
        info = self.fcvm("version")
        self.assertIn(f"state  {self.t}/data/fcvm", info)     # installed: state in XDG, not with the code

        self.fcvm("upgrade", "--source", rels["0.9.1"])
        self.fcvm("upgrade", "--source", rels["0.10.0"])
        self.assertEqual(self.fcvm("-V"), "fcvm 0.10.0")
        self.assertEqual(sorted(os.listdir(self.lib)), ["0.10.0", "0.9.1", "current"])   # keeps two

        self.fcvm("upgrade", "--source", rels["0.9.1"])        # back, to a kept release
        self.assertEqual(self.fcvm("-V"), "fcvm 0.9.1")
        self.assertEqual(os.readlink(os.path.join(self.bin, "fcvm")), os.path.join(self.lib, "current/fcvm"))

    def test_upgrade_refuses_in_a_checkout(self):
        r = subprocess.run([os.path.join(ROOT, "fcvm"), "upgrade"], capture_output=True, text=True,
                           env={**os.environ, "FCVM_HOME": os.path.join(self.t, "h")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git checkout", r.stderr)


if __name__ == "__main__":
    unittest.main()
