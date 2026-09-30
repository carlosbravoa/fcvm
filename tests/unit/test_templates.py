"""Launch templates (lib/templates.py): validation, the create arguments a
template becomes, and saving over built-in ones."""
import os
import tempfile
import unittest

from helpers import ROOT  # noqa: F401
import templates as tp


class Validate(unittest.TestCase):
    def test_defaults_fill_in(self):
        t = tp.validate({"image": "alpine-latest"})
        self.assertEqual((t["network"], t["process"], t["jail"], t["restart"]), ("full", "image", None, "no"))

    def test_rejects(self):
        for bad in ({}, {"image": "../x"}, {"image": "a", "vcpus": 0}, {"image": "a", "network": "open"},
                    {"image": "a", "network": "restricted"}, {"image": "a", "process": "command"},
                    {"image": "a", "restart": "sometimes"}, {"image": "a", "jail": "yes"},
                    {"image": "a", "ports": "8080:80"}, {"image": "a", "colour": "red"}):
            with self.assertRaises(tp.Invalid, msg=bad):
                tp.validate(bad)

    def test_builtins_are_valid(self):
        for f in os.listdir(tp.BUILTIN_DIR):
            with open(os.path.join(tp.BUILTIN_DIR, f)) as fh:
                tp.validate(__import__("json").load(fh))


class Arguments(unittest.TestCase):
    def test_options(self):
        t = tp.validate({"image": "a", "vcpus": 2, "mem_mib": 2048, "disk": "4G", "copy": True, "ports": ["8080:80"],
                         "volumes": ["cache:/c", "~/src"], "network": "restricted", "allow": ["@pypi", "x.org"],
                         "jail": False, "restart": "always"})
        self.assertEqual(tp.options(t), ["--vcpus", "2", "--mem", "2048", "--disk", "4G", "--copy", "-p", "8080:80",
                                         "-v", "cache:/c", "-v", "~/src", "--allow", "@pypi,x.org", "--no-jail",
                                         "--restart", "always"])
        self.assertEqual(tp.options(tp.validate({"image": "a", "network": "none"})), ["--net", "none"])

    def test_process(self):
        self.assertEqual(tp.process(tp.validate({"image": "a"})), [])
        self.assertEqual(tp.process(tp.validate({"image": "a", "process": "idle"})), ["--idle"])
        self.assertEqual(tp.process(tp.validate({"image": "a", "process": "command", "command": ["sh", "-c", "x"],
                                                 "entrypoint": ""})),
                         ["--entrypoint", "", "--", "sh", "-c", "x"])


class Store(unittest.TestCase):
    def setUp(self):
        self.old = tp.USER_DIR
        tp.USER_DIR = tempfile.mkdtemp(prefix="fcvm-tpl-")

    def tearDown(self):
        tp.USER_DIR = self.old

    def test_save_get_remove(self):
        tp.save("mine", {"image": "alpine-latest", "description": "d"})
        self.assertEqual(tp.get("mine")["description"], "d")
        self.assertFalse(tp.get("mine")["builtin"])
        tp.remove("mine")
        with self.assertRaises(tp.Invalid):
            tp.get("mine")

    def test_user_template_replaces_builtin(self):
        self.assertTrue(tp.get("offline-shell")["builtin"])
        with self.assertRaises(tp.Invalid):          # built-in ones can't be removed
            tp.remove("offline-shell")
        tp.save("offline-shell", {"image": "alpine-latest", "mem_mib": 128, "network": "none"})
        self.assertEqual(tp.get("offline-shell")["mem_mib"], 128)
        tp.remove("offline-shell")                   # and the built-in one is back
        self.assertTrue(tp.get("offline-shell")["builtin"])

    def test_bad_names(self):
        for n in ("", "../x", "-x", "a b"):
            with self.assertRaises(tp.Invalid):
                tp.save(n, {"image": "a"})

    def test_broken_files_are_skipped(self):
        with open(os.path.join(tp.USER_DIR, "broken.json"), "w") as f:
            f.write("{not json")
        self.assertNotIn("broken", tp.all_templates())


if __name__ == "__main__":
    unittest.main()
