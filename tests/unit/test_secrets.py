"""lib/secrets.py: what the guest receives over its vsock connection."""
import asyncio
import json
import os
import tempfile
import unittest

from helpers import ROOT  # noqa: F401
import secrets as sc


class Encoding(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(sc.encode({"A": "1", "B_2": "two\nlines\n"}), b"A\x001\x00B_2\x00two\nlines\n\x00")

    def test_bad_names_and_nuls_are_dropped(self):
        self.assertEqual(sc.encode({"ok": "v", "no-dash": "v", "": "v", "nul": "a\x00b"}), b"ok\x00v\x00")


class Server(unittest.TestCase):
    def test_serves_the_file_and_rereads_it(self):
        d = tempfile.mkdtemp(prefix="fcvm-secrets-")
        path, uds = os.path.join(d, "secrets.json"), os.path.join(d, "vsock.sock_9000")
        with open(path, "w") as f:
            json.dump({"TOKEN": "t1"}, f)

        async def go():
            server = await sc.serve(uds, path)
            async with server:
                r, w = await asyncio.open_unix_connection(uds)
                first = await r.read()
                w.close()
                with open(path, "w") as f:
                    json.dump({"TOKEN": "t2", "X": "y"}, f)
                r, w = await asyncio.open_unix_connection(uds)
                second = await r.read()
                w.close()
            return first, second
        first, second = asyncio.run(go())
        self.assertEqual(first, b"TOKEN\x00t1\x00")
        self.assertEqual(second, b"TOKEN\x00t2\x00X\x00y\x00")


if __name__ == "__main__":
    unittest.main()
