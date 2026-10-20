"""Error codes (docs/errors.md): die_code in lib/common.sh, and how the
server maps a failed CLI call to a status."""
import unittest

from helpers import bash
import server


class DieCode(unittest.TestCase):
    def test_code_line_follows_the_error_line(self):
        r = bash("die_code vm_not_found \"no VM 'x'\"")
        self.assertEqual(r.returncode, 1)
        self.assertEqual([l for l in r.stderr.splitlines() if "no VM 'x'" in l][0].endswith("no VM 'x'"), True)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: vm_not_found")   # stderr is a pipe here

    def test_plain_die_has_the_generic_code(self):
        r = bash('die "something broke"')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.splitlines()[-1], "fcvm-error: error")


class ServerMapping(unittest.TestCase):
    def test_status_from_code(self):
        e = server.cli_error("error: no VM 'x'\nfcvm-error: vm_not_found\n", 1)   # fcvm() strips colours before this
        self.assertEqual((e.status, e.code, e.message), (404, "vm_not_found", "no VM 'x'"))
        e = server.cli_error("==> starting\nerror: VM 'x' is already running (pid 1)\nfcvm-error: vm_running\n", 1)
        self.assertEqual((e.status, e.code), (409, "vm_running"))
        e = server.cli_error("error: --vcpus and --mem want positive numbers\nfcvm-error: invalid\n", 1)
        self.assertEqual((e.status, e.code), (400, "invalid"))

    def test_uncoded_failures_stay_400(self):
        e = server.cli_error("error: resize failed\n", 1)
        self.assertEqual((e.status, e.code, e.message), (400, "error", "resize failed"))
        e = server.cli_error("", 3)
        self.assertEqual((e.status, e.message), (400, "exit 3"))
        e = server.cli_error("error: boom\nfcvm-error: never_heard_of_it\n", 1)
        self.assertEqual((e.status, e.code), (400, "never_heard_of_it"))


if __name__ == "__main__":
    unittest.main()
