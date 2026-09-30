"""Host-directory confinement in the 9P server (lib/share9p.py): nothing
outside the shared directory is reachable, including through symlinks."""
import errno
import os
import tempfile
import unittest

from helpers import ROOT  # noqa: F401
import share9p


class Confinement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fcvm-9p-")
        self.root = os.path.join(self.tmp, "share")
        os.makedirs(os.path.join(self.root, "sub"))
        os.makedirs(os.path.join(self.tmp, "outside"))
        os.symlink(os.path.join(self.tmp, "outside"), os.path.join(self.root, "escape"))
        os.symlink("sub", os.path.join(self.root, "inner"))
        self.share = share9p.Share(self.root, readonly=False)

    def err(self, rel, name):
        with self.assertRaises(share9p.P9Error) as cm:
            self.share.child(rel, name)
        return cm.exception.args[0] if cm.exception.args else getattr(cm.exception, "code", None)

    def test_normal_walks(self):
        self.assertEqual(self.share.child("", "sub"), "sub")
        self.assertEqual(self.share.child("sub", "file"), "sub/file")
        self.assertEqual(self.share.child("sub", "."), "sub")

    def test_dotdot_stops_at_root(self):
        self.assertEqual(self.share.child("sub", ".."), "")
        self.assertEqual(self.share.child("", ".."), "")

    def test_bad_names(self):
        for name in ("", "a/b"):
            with self.assertRaises(share9p.P9Error):
                self.share.child("", name)

    def test_symlinked_parent_pointing_outside_is_refused(self):
        with self.assertRaises(share9p.P9Error):
            self.share.child("escape", "anything")

    def test_symlink_inside_is_fine(self):
        self.assertEqual(self.share.child("inner", "x"), "inner/x")

    def test_readonly(self):
        ro = share9p.Share(self.root, readonly=True)
        with self.assertRaises(share9p.P9Error):
            ro.writable()
        self.share.writable()   # no error


if __name__ == "__main__":
    unittest.main()
