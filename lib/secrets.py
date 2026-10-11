#!/usr/bin/env python3
"""Secrets for a VM, handed over at boot through vsock (fcvm create --secret).

  secrets.py serve --uds VMDIR/vsock.sock_9000 --file VMDIR/secrets.json --pidfile VMDIR/pid

Firecracker forwards a guest's vsock connection to host port P to the Unix
socket <uds_path>_P. The guest's init connects (fcvm.secrets=P on its command
line) and receives every secret as NAME\\0VALUE\\0, then the connection closes.
In the guest they live in a tmpfs at /run/secrets, never on its disk. The
file is re-read on every connection, so a fork or a restart gets current
values. Exits when the VM's Firecracker process does.
"""
import argparse
import asyncio
import json
import os
import sys

from share9p import watch   # the same "until the VM is gone" loop

NAME_OK = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"


def encode(secrets):
    """NAME\\0VALUE\\0 for every secret; names and values can't hold NULs."""
    out = b""
    for name, value in secrets.items():
        if not name or any(c not in NAME_OK for c in name) or "\0" in str(value):
            continue
        out += name.encode() + b"\0" + str(value).encode() + b"\0"
    return out


def load(path):
    try:
        with open(path) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


async def serve(path, file):
    async def handle(_reader, writer):
        try:
            writer.write(encode(load(file)))
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:
            pass
        finally:
            writer.close()

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old = os.umask(0o007)              # a jailed VM's uid connects through its ACL entry
    try:
        server = await asyncio.start_unix_server(handle, path)
    finally:
        os.umask(old)
    print(f"secrets: serving {len(load(file))} secret(s) on {path}", file=sys.stderr, flush=True)
    return server


async def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--uds", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--pidfile", required=True)
    args = ap.parse_args()
    server = await serve(args.uds, args.file)
    async with server:
        await watch(args.pidfile)


if __name__ == "__main__":
    asyncio.run(main())
