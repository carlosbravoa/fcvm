"""Launch templates from the command line: save one, create from it with
overrides, and capture a VM's configuration as a template."""
import json
import unittest

from fcvmtest import VMTestCase, fcvm, unique, IMAGE


class Templates(VMTestCase):
    def template(self):
        name = unique("tpl")
        self.addCleanup(fcvm, "template", "rm", name, check=False)
        return name

    def create(self, *args):
        vm = unique("vm")
        fcvm("create", vm, *args)
        self.addCleanup(self.remove_vm, vm)
        return vm

    def test_save_and_create_with_overrides(self):
        t = self.template()
        fcvm("template", "save", t, "-d", "a test", IMAGE, "--mem", "300", "--net", "none",
             "-v", f"{unique('vol')}:/data", "--", "sh", "-c", "echo from-template")
        saved = json.loads(fcvm("template", "show", t).stdout)
        self.assertEqual((saved["mem_mib"], saved["vcpus"], saved["network"], saved["description"]),
                         (300, None, "none", "a test"))
        self.assertEqual(saved["command"], ["sh", "-c", "echo from-template"])   # -c kept as an argument
        vol = saved["volumes"][0].split(":")[0]
        self.addCleanup(fcvm, "volume", "rm", vol, check=False)
        self.assertNotIn(vol, fcvm("volume", "ls").stdout)                        # saving created nothing
        vm = self.create("--template", t, "--mem", "256", "--idle")               # later options win
        v = self.inspect(vm)
        self.assertEqual((v["mem_mib"], v["net"]["mode"]), (256, "none"))
        fcvm("start", vm)
        self.assertEqual(self.sh(vm, "echo idle").stdout.strip(), "idle")         # --idle replaced the command

    def test_template_command_runs(self):
        t = self.template()
        fcvm("template", "save", t, IMAGE, "--", "sh", "-c", "echo ran-$((6*7))")
        out = fcvm("run", "--template", t).stdout
        self.assertIn("ran-42", out)

    def test_from_vm(self):
        vm = self.vm("--mem", "320", "--allow", "@pypi", start=False)
        t = self.template()
        fcvm("template", "save", t, "--from", vm, "-d", "copied")
        saved = json.loads(fcvm("template", "show", t).stdout)
        self.assertEqual((saved["image"], saved["mem_mib"], saved["allow"], saved["process"]),
                         (IMAGE, 320, ["@pypi"], "idle"))

    def test_bad_template_is_refused(self):
        r = fcvm("template", "save", self.template(), IMAGE, "--vcpus", "many", check=False)
        self.assertNotEqual(r.returncode, 0)
        r = fcvm("create", unique("vm"), "--template", "fcvmtest-no-such-template", check=False)
        self.assertIn("no template", r.stderr)

    def test_builtins_listed(self):
        names = [t["name"] for t in json.loads(fcvm("template", "ls", "--json").stdout)]
        self.assertIn("python-sandbox", names)


if __name__ == "__main__":
    unittest.main()
