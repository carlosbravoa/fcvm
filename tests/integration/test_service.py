"""The server (fcvm serve / the fcvm service): the HTTP API with bearer tokens,
and the supervisor's restart policies. Uses the running service when there is
one for this state directory, otherwise starts fcvm serve for these tests."""
import unittest

from fcvmtest import VMTestCase, Server, fcvm, IMAGE, wait_for, unique


class Service(VMTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_api_auth(self):
        code, body = self.server.api("GET", "vms")
        self.assertEqual(code, 200)
        self.assertIsInstance(body, list)
        code, _ = self.server.api("GET", "vms", token="wrong")
        self.assertEqual(code, 401)

    def test_api_update_restart_policy(self):
        vm = self.vm(start=False)
        code, body = self.server.api("POST", f"vms/{vm}/update", {"restart": "on-failure"})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.inspect(vm)["restart"], "on-failure")
        code, _ = self.server.api("POST", f"vms/{vm}/update", {"restart": "bogus"})
        self.assertEqual(code, 400)

    def test_on_failure_restarts_until_stopped(self):
        vm = self.vm("--restart", "on-failure", cmd=["sh", "-c", "sleep 1; exit 3"])
        seen = set()

        def restarts():
            le = self.inspect(vm).get("last_exit") or {}
            if le.get("code") == 3:
                seen.add(le["at"])
            return len(seen) >= 2
        wait_for(restarts, timeout=45, what="two failed runs, restarted by the supervisor")
        fcvm("stop", vm)
        at = self.inspect(vm)["last_exit"]["at"]
        wait_for(lambda: self.inspect(vm)["state"] != "running", 10, what="the stop")
        import time
        time.sleep(6)                                     # longer than the supervisor's backoff so far
        info = self.inspect(vm)
        self.assertNotEqual(info["state"], "running")
        self.assertTrue(info["stopped_by_user"])
        self.assertLessEqual(info["last_exit"]["at"], at + 5)

    def test_clean_exit_is_not_restarted(self):
        vm = self.vm("--restart", "on-failure", cmd=["true"])
        wait_for(lambda: (self.inspect(vm).get("last_exit") or {}).get("code") == 0, 20, what="the exit")
        import time
        time.sleep(5)
        self.assertNotEqual(self.inspect(vm)["state"], "running")

    def test_throwaway_vms_take_no_policy(self):
        r = fcvm("run", IMAGE, "--restart", "always", "--", "true", check=False)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
