"""Egress allowlist matching (lib/egress_proxy.py)."""
import unittest

from helpers import ROOT  # noqa: F401  (sets up sys.path)
import egress_proxy as ep


class Allowed(unittest.TestCase):
    def test_exact_host_default_ports(self):
        self.assertTrue(ep.allowed("example.com", 443, ["example.com"]))
        self.assertTrue(ep.allowed("example.com", 80, ["example.com"]))
        self.assertFalse(ep.allowed("example.com", 22, ["example.com"]))

    def test_case_and_trailing_dot(self):
        self.assertTrue(ep.allowed("Example.COM.", 443, ["example.com"]))

    def test_wildcard_is_subdomains_only(self):
        self.assertTrue(ep.allowed("api.github.com", 443, ["*.github.com"]))
        self.assertTrue(ep.allowed("a.b.github.com", 443, ["*.github.com"]))
        self.assertFalse(ep.allowed("github.com", 443, ["*.github.com"]))
        self.assertFalse(ep.allowed("evilgithub.com", 443, ["*.github.com"]))

    def test_explicit_port(self):
        self.assertTrue(ep.allowed("corp.example", 8443, ["corp.example:8443"]))
        self.assertFalse(ep.allowed("corp.example", 443, ["corp.example:8443"]))

    def test_suffix_is_not_a_match(self):
        self.assertFalse(ep.allowed("pypi.org.evil.com", 443, ["pypi.org"]))
        self.assertFalse(ep.allowed("notpypi.org", 443, ["pypi.org"]))

    def test_empty_allowlist(self):
        self.assertFalse(ep.allowed("example.com", 443, []))


class HostPort(unittest.TestCase):
    def test_split(self):
        self.assertEqual(ep.split_hostport("example.com:8443", 443), ("example.com", 8443))
        self.assertEqual(ep.split_hostport("example.com", 443), ("example.com", 443))
        self.assertEqual(ep.split_hostport("[::1]:8080", 80), ("::1", 8080))
        self.assertEqual(ep.split_hostport("[::1]", 80), ("::1", 80))


if __name__ == "__main__":
    unittest.main()
