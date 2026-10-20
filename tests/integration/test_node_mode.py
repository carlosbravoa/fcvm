"""Node mode (NODE=1, docs/configuration.md): what the CLI refuses and pins
on a host that serves tenants it doesn't trust with the host. NODE comes
from the environment here (this checkout has no fcvm.conf), and VMs start
from this process so the environment applies (FCVM_DAEMON=1)."""
import json
import os
import tempfile
import unittest

from fcvmtest import VMTestCase, fcvm, IMAGE, unique, STATE, jail_usable

NODE = {"NODE": "1", "FCVM_DAEMON": "1"}


def refused(*args, env=None):
    r = fcvm(*args, check=False, env={**NODE, **(env or {})})
    return r.returncode != 0 and r.stderr.splitlines()[-1] == "fcvm-error: policy", r.stderr


class NodeMode(VMTestCase):
    def test_refusals(self):
        vm = unique("vm")
        self.addCleanup(fcvm, "rm", vm, check=False)
        host_dir = tempfile.mkdtemp(prefix="fcvm-nm-")
        for args in (("-v", f"{host_dir}:/x"), ("--forward", "git"), ("--no-jail"), ("--secret", "A=@/etc/hostname"),
                     ("--net", "full", "--tenant", "x")):
            args = (args,) if isinstance(args, str) else args
            ok, err = refused("create", vm, IMAGE, "--idle", *args, env={"NODE_NETWORKS": "restricted,none"} if "full" in args else None)
            self.assertTrue(ok, (args, err))
            self.assertFalse(os.path.isdir(os.path.join(STATE, "vms", vm)), args)
        # an unjailed VM from before node mode neither starts nor mounts host paths
        old = self.vm("--mem", "256", start=False)
        for args in (("start", old), ("mount", old, f"{host_dir}:/x"), ("cp", "/etc/hostname", f"{old}:/x"), ("update", old, "--forward", "git")):
            ok, err = refused(*args)
            self.assertTrue(ok, (args, err))
        fcvm("update", old, "--forward", "none", env=NODE)                         # clearing is fine
        # host paths outside NODE_PATHS: export, snapshot import, a local image, a build context
        ok, err = refused("export", IMAGE, os.path.join(host_dir, "x.tar"))
        self.assertTrue(ok, err)
        ok, err = refused("snapshot", "import", os.path.join(host_dir, "none.tar"))
        self.assertTrue(ok, err)
        ok, err = refused("import", f"oci-archive:{host_dir}/none.tar")
        self.assertTrue(ok, err)
        with open(os.path.join(host_dir, "Dockerfile"), "w") as f:
            f.write(f"FROM {IMAGE}\n")
        ok, err = refused("build", "-t", unique("img"), host_dir)
        self.assertTrue(ok, err)
        ok, err = refused("build", "-t", unique("img"), host_dir, env={"NODE_PATHS": f"{STATE}/builds:{host_dir}"})
        self.assertFalse(ok, "under NODE_PATHS the build goes on")                  # (and fails later or succeeds; not our concern)

    def test_defaults_and_pinned_settings(self):
        vm = unique("vm")
        self.addCleanup(fcvm, "rm", vm, check=False)
        fcvm("create", vm, IMAGE, "--idle", "--mem", "256", env=NODE)
        info = self.inspect(vm)
        self.assertEqual((info["jail"], info["net"]["mode"], info["net"]["allow"]), (True, "restricted", []))
        self.assertEqual((info["spec"]["network"], info["spec"]["allow"], info["spec"]["jail"]), ("restricted", [], None))
        c = json.loads(fcvm("config", "--json", env={**NODE, "NET_HOST_ACCESS": "1", "JAIL": "0", "NODE_ADMIT": "0"}).stdout)["settings"]
        self.assertEqual({k: c[k] for k in ("NODE", "NODE_ADMIT", "JAIL", "NET_ISOLATE", "NET_HOST_ACCESS")},
                         {"NODE": "1", "NODE_ADMIT": "1", "JAIL": "1", "NET_ISOLATE": "1", "NET_HOST_ACCESS": "0"})
        self.assertEqual(c["NODE_PATHS"], os.path.join(STATE, "builds"))
        s = json.loads(fcvm("status", "--json", env=NODE).stdout)
        self.assertEqual((s["node_mode"], s["admission"]), (True, True))
        self.assertFalse(json.loads(fcvm("status", "--json").stdout)["node_mode"])
        self.assertIn("node mode", fcvm("status", "--offline", env=NODE, check=False).stdout)
        # an explicit network is kept; a template with a bare restricted network works anywhere
        other = unique("vm")
        self.addCleanup(fcvm, "rm", other, check=False)
        fcvm("create", other, IMAGE, "--idle", "--mem", "256", "--net", "full", env=NODE)
        self.assertEqual(self.inspect(other)["net"]["mode"], "full")
        tpl = unique("tpl")
        self.addCleanup(fcvm, "template", "rm", tpl, check=False)
        fcvm("template", "save", tpl, IMAGE, "--idle", "--mem", "256", "--net", "restricted")
        self.assertEqual(json.loads(fcvm("template", "show", tpl).stdout)["network"], "restricted")
        third = unique("vm")
        self.addCleanup(fcvm, "rm", third, check=False)
        fcvm("create", third, "--template", tpl)
        self.assertEqual((self.inspect(third)["net"]["mode"], self.inspect(third)["net"]["allow"]), ("restricted", []))

    @unittest.skipUnless(jail_usable(), "fcvm-jaild isn't set up for this state directory")
    def test_a_node_vm_runs_jailed_with_nothing_out(self):
        vm = unique("vm")
        self.addCleanup(self.remove_vm, vm)
        fcvm("create", vm, IMAGE, "--idle", "--mem", "256", env=NODE)
        fcvm("start", vm, env=NODE)
        self.assertEqual(self.inspect(vm)["state"], "running")
        r = self.sh(vm, "wget -qO- -T 5 http://example.com/ && echo reached || echo blocked", check=False)
        self.assertIn("blocked", r.stdout)
        fcvm("egress", vm, "--allow", "example.com")
        self.assertIn("example.com", fcvm("inspect", vm).stdout)


if __name__ == "__main__":
    unittest.main()
