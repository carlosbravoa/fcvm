"""Project files (lib/project.py): parsing, templates as bases, relative
paths, names, and what each VM's create command becomes."""
import os
import shutil
import tempfile
import unittest

from helpers import ROOT  # noqa: F401
import project as pj


@unittest.skipIf(pj.tomllib is None, "needs Python 3.11+ (tomllib)")
class Load(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-proj-")
        self.sub = os.path.join(self.d, "shop")
        os.makedirs(os.path.join(self.sub, "site"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, text, name="fcvm.toml"):
        p = os.path.join(self.sub, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_a_project(self):
        p = self.write('''
[vms.web]
image = "nginx-latest"
ref = "nginx:latest"
ports = ["8080:80"]
volumes = ["site:/usr/share/nginx/html", "./site:/srv:ro"]
restart = "unless-stopped"

[vms.box]
template = "python-sandbox"
volumes = ["./:/work"]
command = ["python", "/work/app.py"]
ttl = "4h"
''')
        name, vms = pj.load(p)
        self.assertEqual(name, "shop")
        self.assertEqual(sorted(vms), ["shop-box", "shop-web"])
        web = vms["shop-web"]
        self.assertEqual(web["volumes"], ["site:/usr/share/nginx/html", f"{self.sub}/site:/srv:ro"])
        self.assertEqual(pj.create_args("shop-web", web)[:3], ["create", "shop-web", "nginx-latest"])
        self.assertIn("--restart", pj.create_args("shop-web", web))
        box = vms["shop-box"]
        self.assertEqual((box["image"], box["network"], box["allow"]), ("python-3.13-slim", "restricted", ["@pypi", "@github"]))
        self.assertEqual((box["process"], box["command"], box["ttl"]), ("command", ["python", "/work/app.py"], "4h"))
        _, vms = pj.load(self.write('[vms.w]\nimage = "python-3.13-slim"\nidle = true\ncommand = ["sleep", "1"]\n'))
        self.assertEqual(vms["shop-w"]["process"], "command")
        self.assertEqual(box["volumes"], [f"{self.sub}:/work"])
        args = pj.create_args("shop-box", box)
        self.assertEqual(args[-3:], ["--", "python", "/work/app.py"])
        self.assertIn("--ttl", args)

    def test_name_idle_and_additive_lists(self):
        p = self.write('''
name = "proj"
[vms.a]
template = "python-sandbox"
idle = true
allow = ["example.com"]
ports = ["8000:8000"]
''')
        name, vms = pj.load(p)
        a = vms["proj-a"]
        self.assertEqual((a["process"], a["allow"], a["ports"]), ("idle", ["@pypi", "@github", "example.com"], ["8000:8000"]))

    def test_labels_merge_with_the_template(self):
        _, vms = pj.load(self.write('''
[vms.a]
template = "python-sandbox"
labels = {env = "ci"}
'''))
        self.assertEqual(vms["shop-a"]["labels"], {"env": "ci"})
        self.assertIn("--label", pj.create_args("shop-a", vms["shop-a"]))

    def test_errors(self):
        for text, msg in (("", "no VMs"), ("[vms.a]\nvcpus = 2\n", "image is required"),
                          ('[vms.a]\nimage = "x"\ncolour = "red"\n', "unknown field"),
                          ('[vms.a]\nimage = "x"\n[other]\n', "unknown top-level"),
                          ('[vms.a]\ntemplate = "no-such-template"\n', "no template"),
                          ("not toml [", "fcvm.toml")):
            with self.assertRaises(pj.Invalid, msg=text) as cm:
                pj.load(self.write(text))
            self.assertIn(msg, str(cm.exception))

    def test_file_is_found_upwards(self):
        p = self.write('[vms.a]\nimage = "x"\n')
        deep = os.path.join(self.sub, "a", "b")
        os.makedirs(deep)
        cwd = os.getcwd()
        os.chdir(deep)
        try:
            self.assertEqual(pj.find_file(), p)
        finally:
            os.chdir(cwd)
        with self.assertRaises(pj.Invalid):
            pj.find_file(os.path.join(self.d, "nope.toml"))

    def test_pick(self):
        _, vms = pj.load(self.write('[vms.a]\nimage = "x"\n[vms.b]\nimage = "y"\n'))
        self.assertEqual(list(pj.pick("shop", vms, ["b"])), ["shop-b"])
        self.assertEqual(list(pj.pick("shop", vms, ["shop-a"])), ["shop-a"])
        with self.assertRaises(pj.Invalid):
            pj.pick("shop", vms, ["c"])


if __name__ == "__main__":
    unittest.main()
