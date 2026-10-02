"""The vendored web console files match the hashes in vendor/SHA256SUMS, and
every file there is listed (lib/web/static/vendor)."""
import hashlib
import os
import unittest

from helpers import LIB

VENDOR = os.path.join(LIB, "web", "static", "vendor")
NOT_HASHED = {"SHA256SUMS", "README.md", "LICENSE.xterm"}


class Vendor(unittest.TestCase):
    def sums(self):
        with open(os.path.join(VENDOR, "SHA256SUMS")) as f:
            return {name: digest for digest, name in (line.split() for line in f if line.strip())}

    def test_hashes_match(self):
        for name, digest in self.sums().items():
            with open(os.path.join(VENDOR, name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest, name)

    def test_every_file_is_listed(self):
        self.assertEqual(set(os.listdir(VENDOR)) - NOT_HASHED, set(self.sums()))
