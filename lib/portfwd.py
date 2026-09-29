#!/usr/bin/env python3
"""Publish VM ports on the host without root (fcvm -p).

  portfwd.py --watch PID --target IP [BIND:]HOSTPORT:GUESTPORT ...

Listens on each host port and relays TCP connections to TARGET:GUESTPORT.
Exits when process PID (the VM's firecracker) is gone. The guest sees
connections coming from the host bridge address, not the original client.
"""
import argparse
import asyncio
import os
import sys


def parse(spec):
    spec = spec.removesuffix("/tcp")
    if spec.endswith("/udp"):
        sys.exit(f"portfwd: {spec}: only TCP is supported")
    parts = spec.split(":")
    if len(parts) == 2:
        parts.insert(0, "0.0.0.0")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        sys.exit(f"portfwd: bad port spec '{spec}' (want [BIND:]HOSTPORT:GUESTPORT)")
    return parts[0], int(parts[1]), int(parts[2])


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()   # half-close: the other direction keeps flowing
    except OSError:
        pass


async def relay(client_r, client_w, target, port):
    try:
        guest_r, guest_w = await asyncio.wait_for(asyncio.open_connection(target, port), 5)
    except (OSError, asyncio.TimeoutError) as e:
        print(f"portfwd: {target}:{port}: {e or 'timeout'}", file=sys.stderr, flush=True)
        client_w.close()
        return
    await asyncio.gather(pipe(client_r, guest_w), pipe(guest_r, client_w))
    for w in (client_w, guest_w):
        w.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("ports", nargs="+")
    args = ap.parse_args()

    servers = []
    for bind, hport, gport in map(parse, args.ports):
        try:
            servers.append(await asyncio.start_server(
                lambda r, w, gp=gport: relay(r, w, args.target, gp), bind, hport, reuse_address=True))
        except OSError as e:
            sys.exit(f"portfwd: cannot listen on {bind}:{hport}: {e.strerror}")
        print(f"portfwd: {bind}:{hport} -> {args.target}:{gport}", file=sys.stderr, flush=True)

    while True:
        try:
            os.kill(args.watch, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
