"""Leases: the supervisor's decision (lib/web/supervisor.py) and the CLI's
parsing of --lease (parse_until in lib/common.sh)."""
import time
import unittest

from helpers import bash
import supervisor as sv


class LeaseEnded(unittest.TestCase):
    def test_cases(self):
        now = 1000.0
        self.assertFalse(sv.lease_ended({}, now))
        self.assertFalse(sv.lease_ended({"lease_until": now + 10}, now, grace=0))
        self.assertTrue(sv.lease_ended({"lease_until": now - 1}, now, grace=0))
        self.assertFalse(sv.lease_ended({"lease_until": now - 1}, now, grace=300))      # within the grace period
        self.assertTrue(sv.lease_ended({"lease_until": now - 301}, now, grace=300))
        self.assertIsInstance(sv.LEASE_GRACE, int)


class ParseUntil(unittest.TestCase):
    def until(self, v):
        r = bash(f"parse_until --lease '{v}'")
        return r.returncode, r.stdout.strip(), r.stderr

    def test_forms(self):
        self.assertEqual(self.until("1792497600"), (0, "1792497600", ""))
        self.assertEqual(self.until("2026-10-20T12:00:00Z")[1], "1792497600")
        code, out, _ = self.until("2h")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(int(out), time.time() + 7200, delta=5)
        self.assertEqual(self.until("none"), (0, "", ""))
        code, _, err = self.until("soon")
        self.assertEqual((code, err.splitlines()[-1]), (1, "fcvm-error: invalid"))


if __name__ == "__main__":
    unittest.main()
