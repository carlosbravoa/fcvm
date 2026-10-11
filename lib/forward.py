#!/usr/bin/env python3
"""Credential forwarding into a VM (fcvm create --forward): the host's
ssh-agent and `git credential fill`, over vsock.

  forward.py serve --uds-prefix VMDIR/vsock.sock --pidfile VMDIR/pid --log FILE [--ssh-agent SOCK] [--git]

Firecracker forwards a guest's vsock connection to host port P to the Unix
socket <uds_path>_P. This serves:
  - <prefix>_9001 (ssh-agent): every connection is joined to SOCK, the host's
    agent, so the guest can sign with your keys but never sees them;
  - <prefix>_9002 (git): a credential request in git's key=value form,
    answered with `git credential fill` on the host, so your helpers and
    stored logins work in the guest. Only "get" is served: the guest can
    never store or erase anything here.
Every request is logged to FILE. Exits when the VM's Firecracker does.
"""
import argparse
import asyncio
import os
import sys
import time

from share9p import watch   # the same "until the VM is gone" loop

SSH_PORT, GIT_PORT = 9001, 9002


class Log:
    def __init__(self, path):
        self.path = path

    def __call__(self, msg):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
        try:
            with open(self.path, "a") as f:
                f.write(line)
        except OSError:
            print(line, end="", file=sys.stderr, flush=True)


async def splice(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        pass


async def ssh_session(reader, writer, sock, log):
    try:
        ur, uw = await asyncio.open_unix_connection(sock)
    except OSError as e:
        log(f"ssh-agent: cannot reach the host's agent at {sock}: {e.strerror or e}")
        writer.close()
        return
    log("ssh-agent: connection from the guest")
    await asyncio.gather(splice(reader, uw), splice(ur, writer))
    for w in (writer, uw):
        w.close()


def parse_request(data):
    req = {}
    for line in data.decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            req[k] = v
    return req


async def credential_fill(data):
    """`git credential fill` on the host, never prompting; empty when nothing matched."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}
    try:
        p = await asyncio.create_subprocess_exec("git", "credential", "fill", stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
        out, _ = await asyncio.wait_for(p.communicate(data), 30)
    except (OSError, asyncio.TimeoutError):
        return b""
    return out if p.returncode == 0 else b""


async def git_session(reader, writer, log):
    try:
        data = await asyncio.wait_for(reader.read(65536), 30)
    except (asyncio.TimeoutError, OSError):
        writer.close()
        return
    req = parse_request(data)
    where = req.get("url") or f"{req.get('protocol', '?')}://{req.get('host', '?')}"
    reply = await credential_fill(data) if req else b""
    log(f"git credential: {where}: {'found' if b'password=' in reply else 'none'}")
    try:
        writer.write(reply)
        await writer.drain()
    except OSError:
        pass
    writer.close()


async def listen(path, handler):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old = os.umask(0o007)              # a jailed VM's uid connects through its ACL entry
    try:
        return await asyncio.start_unix_server(handler, path)
    finally:
        os.umask(old)


async def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--uds-prefix", required=True)
    s.add_argument("--pidfile", required=True)
    s.add_argument("--log", required=True)
    s.add_argument("--ssh-agent", metavar="SOCK", help="the host's SSH_AUTH_SOCK")
    s.add_argument("--git", action="store_true")
    args = ap.parse_args()
    log = Log(args.log)
    servers = []
    if args.ssh_agent:
        servers.append(await listen(f"{args.uds_prefix}_{SSH_PORT}", lambda r, w: ssh_session(r, w, args.ssh_agent, log)))
    if args.git:
        servers.append(await listen(f"{args.uds_prefix}_{GIT_PORT}", lambda r, w: git_session(r, w, log)))
    if not servers:
        sys.exit("forward: nothing to forward (--ssh-agent SOCK and/or --git)")
    print(f"forward: {'ssh-agent ' if args.ssh_agent else ''}{'git' if args.git else ''}".strip(), file=sys.stderr, flush=True)
    await watch(args.pidfile)


if __name__ == "__main__":
    asyncio.run(main())
