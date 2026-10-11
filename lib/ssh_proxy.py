#!/usr/bin/env python3
"""ssh's ProxyCommand for `fcvm ssh-config`: this stdin/stdout <-> IP:PORT.

  ssh_proxy.py IP [PORT]

Retries the connection for a few seconds, so an ssh right after `fcvm start`
waits for sshd instead of failing.
"""
import os
import select
import socket
import sys
import time


def main():
    ip, port = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 22
    deadline = time.monotonic() + 15
    while True:
        try:
            s = socket.create_connection((ip, port), timeout=5)
            break
        except OSError as e:
            if time.monotonic() > deadline:
                sys.exit(f"fcvm: cannot reach port {port} of the VM at {ip}: {e}")
            time.sleep(0.3)
    s.settimeout(None)
    stdin_open = True
    while True:
        ready, _, _ = select.select(([0] if stdin_open else []) + [s], [], [])
        if 0 in ready:
            data = os.read(0, 65536)
            if data:
                s.sendall(data)
            else:
                s.shutdown(socket.SHUT_WR)
                stdin_open = False
        if s in ready:
            data = s.recv(65536)
            if not data:
                break
            os.write(1, data)


if __name__ == "__main__":
    main()
