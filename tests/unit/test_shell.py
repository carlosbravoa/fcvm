"""Shell helpers: lib/common.sh, lib/vm.sh's small functions, portfwd specs,
and host-setup --check."""
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, LIB, bash
import portfwd


class Version(unittest.TestCase):
    def test_version_format(self):
        r = bash("fcvm_version")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(ROOT, "VERSION")) as f:
            base = f.read().strip()
        self.assertTrue(r.stdout.strip().startswith(base), r.stdout)

    def test_cli_version(self):
        r = subprocess.run([os.path.join(ROOT, "fcvm"), "-V"], capture_output=True, text=True,
                           env={**os.environ, "FCVM_HOME": tempfile.mkdtemp()})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^fcvm \d+\.\d+\.\d+")


class Locations(unittest.TestCase):
    def test_home_and_state_dirs(self):
        home = tempfile.mkdtemp(prefix="fcvm-home-")
        r = bash('echo "$FCVM_HOME|$VMS_DIR|$IMAGES_DIR|$SSH_DIR"', home=home)
        self.assertEqual(r.stdout.strip(), f"{home}|{home}/vms|{home}/images|{home}/ssh")

    def test_default_home_is_xdg(self):
        xdg = tempfile.mkdtemp(prefix="fcvm-xdg-")
        env = {k: v for k, v in os.environ.items() if k not in ("FCVM_HOME", "FCVM_CONF")}
        env.update(XDG_DATA_HOME=xdg, XDG_CONFIG_HOME=xdg, FCVM_ROOT=tempfile.mkdtemp())
        r = subprocess.run(["bash", "-c", f'. "{LIB}/common.sh"; echo "$FCVM_HOME|$FCVM_CONF"'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.stdout.strip(), f"{xdg}/fcvm|{xdg}/fcvm/fcvm.conf", r.stderr)


def vm_fn(script, home=None):
    """Run SCRIPT with lib/vm.sh's function definitions loaded (not its dispatch)."""
    return bash(f'eval "$(sed -n \'/^vm_dir()/,/^RESTART_POLICIES=/p\' "{LIB}/vm.sh")"; {script}', home=home)


class VmHelpers(unittest.TestCase):
    def test_sock_room(self):
        home = tempfile.mkdtemp(prefix="fh")
        ok = vm_fn("sock_room short && echo ok", home=home)
        self.assertEqual(ok.stdout.strip(), "ok", ok.stderr)
        bad = vm_fn(f"sock_room {'x' * 120}", home=home)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("too long", bad.stderr)

    def test_valid_restart(self):
        r = bash(f'eval "$(grep -E \'^(RESTART_POLICIES|valid_restart)\' "{LIB}/vm.sh")"; '
                 'valid_restart unless-stopped && echo ok; valid_restart bogus')
        self.assertIn("ok", r.stdout)
        self.assertIn("--restart wants one of", r.stderr)


class SshIdentity(unittest.TestCase):
    """A system VM's SSH identity goes into its own writable layer, not the image."""

    def test_written_into_the_layer(self):
        r = bash(f'eval "$(sed -n \'/^make_rw()/,/^# Exit status/p\' "{LIB}/vm.sh")"; '
                 'd=$(mktemp -d); make_rw "$d/rw.ext4" 64M >/dev/null && key=$(ssh_identity "$d/rw.ext4" /upper) && '
                 'echo "$key"; for f in root root/.ssh root/.ssh/authorized_keys etc/ssh/ssh_host_ed25519_key; do '
                 'debugfs -R "stat /upper/$f" "$d/rw.ext4" 2>/dev/null | grep -oE "Mode: +[0-7]+|User: +[0-9]+" | tr -s " " | tr "\n" " "; echo; done; '
                 'debugfs -R "cat /upper/etc/ssh/ssh_host_ed25519_key.pub" "$d/rw.ext4" 2>/dev/null')
        out = r.stdout.splitlines()
        self.assertTrue(out[0].startswith("ssh-ed25519 "), r.stdout + r.stderr)
        self.assertEqual(out[1:5], ["Mode: 0700 User: 0 ", "Mode: 0700 User: 0 ", "Mode: 0600 User: 0 ", "Mode: 0600 User: 0 "])
        self.assertTrue(out[5].startswith(out[0]))                         # vm.json's key is the one in the VM


class PortSpecs(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(portfwd.parse("8080:80"), ("127.0.0.1", 8080, 80))          # local by default
        self.assertEqual(portfwd.parse("0.0.0.0:8080:80"), ("0.0.0.0", 8080, 80))
        self.assertEqual(portfwd.parse("8080:80/tcp"), ("127.0.0.1", 8080, 80))
        with self.assertRaises(SystemExit):
            portfwd.parse("8080:80/udp")
        with self.assertRaises(SystemExit):
            portfwd.parse("nonsense")


class Status(unittest.TestCase):
    def test_runs_to_the_end_with_nothing(self):
        # A fresh state directory, offline, no cached versions: status must
        # still reach its summary (it once died on an empty cache).
        home = tempfile.mkdtemp(prefix="fcvm-st-")
        r = subprocess.run([os.path.join(ROOT, "fcvm"), "status", "--offline"], capture_output=True, text=True,
                           env={**os.environ, "FCVM_HOME": home, "FCVM_CONF": os.path.join(home, "none")})
        self.assertRegex(r.stdout, r"To do:|All good", r.stdout + r.stderr)
        self.assertIn("kernel", r.stdout)


class HostSetupCheck(unittest.TestCase):
    def test_check_runs_without_sudo(self):
        r = subprocess.run([os.path.join(LIB, "host-setup.sh"), "--check"], capture_output=True, text=True,
                           env={**os.environ, "FCVM_HOME": tempfile.mkdtemp(), "SUDO_ASKPASS": "/bin/false"})
        self.assertIn(r.returncode, (0, 1))
        if r.returncode:
            self.assertTrue(r.stdout.strip(), "a failing check must say what's missing")


if __name__ == "__main__":
    unittest.main()
