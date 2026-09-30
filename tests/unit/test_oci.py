"""Image references, names, layer flattening and USER resolution
(lib/oci_import.py)."""
import io
import os
import tarfile
import tempfile
import unittest

from helpers import ROOT  # noqa: F401
import oci_import as oi


class Refs(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(oi.parse_ref("nginx"), ("registry-1.docker.io", "library/nginx", "latest"))
        self.assertEqual(oi.parse_ref("nginx:1.27"), ("registry-1.docker.io", "library/nginx", "1.27"))
        self.assertEqual(oi.parse_ref("docker.io/org/app:v1"), ("registry-1.docker.io", "org/app", "v1"))
        self.assertEqual(oi.parse_ref("ghcr.io/org/app:v1.2"), ("ghcr.io", "org/app", "v1.2"))
        self.assertEqual(oi.parse_ref("localhost:5000/app"), ("localhost:5000", "app", "latest"))
        self.assertEqual(oi.parse_ref("redis@sha256:abc"), ("registry-1.docker.io", "library/redis", "sha256:abc"))

    def test_default_name(self):
        self.assertEqual(oi.default_name("nginx:latest"), "nginx-latest")
        self.assertEqual(oi.default_name("ghcr.io/org/app:v1.2"), "app-v1.2")
        self.assertEqual(oi.default_name("alpine"), "alpine")


def layer(path, entries):
    """entries: [(name, kind, data)] with kind 'f' (file), 'd' (dir), 'l' (symlink)."""
    with tarfile.open(path, "w") as tf:
        for name, kind, data in entries:
            ti = tarfile.TarInfo(name)
            if kind == "d":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return path


class Flatten(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-oci-")

    def flat(self, *layers):
        paths = [layer(os.path.join(self.d, f"l{i}.tar"), e) for i, e in enumerate(layers)]
        return {name: idx for name, (idx, _) in oi.flatten(paths).items()}

    def test_upper_wins(self):
        got = self.flat([("etc", "d", b""), ("etc/a", "f", b"1")], [("etc/a", "f", b"2")])
        self.assertEqual(got["etc/a"], 1)

    def test_whiteout_removes_file(self):
        got = self.flat([("etc", "d", b""), ("etc/a", "f", b"1"), ("etc/b", "f", b"1")],
                        [("etc/.wh.a", "f", b"")])
        self.assertNotIn("etc/a", got)
        self.assertIn("etc/b", got)

    def test_whiteout_of_directory_hides_its_contents(self):
        got = self.flat([("opt", "d", b""), ("opt/x", "d", b""), ("opt/x/f", "f", b"1")],
                        [("opt/.wh.x", "f", b"")])
        self.assertNotIn("opt/x", got)
        self.assertNotIn("opt/x/f", got)

    def test_opaque_directory(self):
        got = self.flat([("var", "d", b""), ("var/old", "f", b"1")],
                        [("var", "d", b""), ("var/.wh..wh..opq", "f", b""), ("var/new", "f", b"2")])
        self.assertNotIn("var/old", got)
        self.assertIn("var/new", got)

    def test_directory_replaced_by_file(self):
        got = self.flat([("data", "d", b""), ("data/f", "f", b"1")], [("data", "f", b"now a file")])
        self.assertEqual(got["data"], 1)
        self.assertNotIn("data/f", got)

    def test_paths_are_normalized(self):
        got = self.flat([("./usr/", "d", b""), ("./usr/bin/../lib", "d", b"")])
        self.assertIn("usr", got)
        self.assertIn("usr/lib", got)


class Users(unittest.TestCase):
    passwd = "root:x:0:0::/root:/bin/sh\napp:x:1000:1000::/home/app:/bin/sh\n"
    group = "root:x:0:\nwheel:x:10:app\napp:x:1000:\ndocker:x:999:app\n"

    def test_forms(self):
        r = lambda spec: oi.resolve_user(spec, self.passwd, self.group)
        self.assertEqual(r(""), "0:0")
        self.assertEqual(r("app"), "1000:1000:10,999")
        self.assertEqual(r("1000"), "1000:1000:10,999")
        self.assertEqual(r("app:wheel"), "1000:10:999")
        self.assertEqual(r("4242"), "4242:0")          # a uid not in passwd, as Docker allows

    def test_unknown_user_fails(self):
        with self.assertRaises(SystemExit):
            oi.resolve_user("nobody-here", self.passwd, self.group)


if __name__ == "__main__":
    unittest.main()
