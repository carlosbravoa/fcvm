"""lib/forward.py: the git credential relay (answered by the host's `git
credential fill`, only ever a lookup) and the ssh-agent relay."""
import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import ROOT  # noqa: F401
import forward as fw


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitCredentials(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fcvm-fwd-")
        creds = os.path.join(self.d, "creds")
        with open(creds, "w") as f:
            f.write("https://alice:s3cret@example.test\n")
        cfg = os.path.join(self.d, "gitconfig")
        subprocess.run(["git", "config", "--file", cfg, "credential.helper", f"store --file={creds}"], check=True)
        self.env = {"GIT_CONFIG_GLOBAL": cfg, "GIT_CONFIG_NOSYSTEM": "1", "HOME": self.d}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.log = []

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)

    def ask(self, request):
        uds = os.path.join(self.d, "vsock.sock_9002")

        async def go():
            server = await fw.listen(uds, lambda r, w: fw.git_session(r, w, self.log.append))
            async with server:
                r, w = await asyncio.open_unix_connection(uds)
                w.write(request)
                w.write_eof()
                reply = await r.read()
                w.close()
            return reply.decode()
        return asyncio.run(go())

    def test_known_host_is_answered(self):
        reply = self.ask(b"protocol=https\nhost=example.test\n\n")
        self.assertIn("username=alice\n", reply)
        self.assertIn("password=s3cret\n", reply)
        self.assertEqual(self.log, ["git credential: https://example.test: found"])

    def test_unknown_host_gets_nothing(self):
        self.assertEqual(self.ask(b"protocol=https\nhost=other.test\n\n"), "")   # and no prompt
        self.assertEqual(self.log, ["git credential: https://other.test: none"])

    def test_garbage_gets_nothing(self):
        self.assertEqual(self.ask(b"\x00\x01 not a request"), "")


class SshAgent(unittest.TestCase):
    def test_relayed_to_the_agent_socket(self):
        d = tempfile.mkdtemp(prefix="fcvm-fwd-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        agent, uds, log = os.path.join(d, "agent.sock"), os.path.join(d, "vsock.sock_9001"), []

        async def fake_agent(r, w):       # echoes with a prefix, then ends
            data = await r.read(100)
            w.write(b"agent:" + data)
            await w.drain()
            w.close()

        async def go():
            a = await asyncio.start_unix_server(fake_agent, agent)
            s = await fw.listen(uds, lambda r, w: fw.ssh_session(r, w, agent, log.append))
            async with a, s:
                r, w = await asyncio.open_unix_connection(uds)
                w.write(b"hello")
                await w.drain()
                reply = await r.read()
                w.close()
            return reply
        self.assertEqual(asyncio.run(go()), b"agent:hello")
        self.assertEqual(log, ["ssh-agent: connection from the guest"])


if __name__ == "__main__":
    unittest.main()
