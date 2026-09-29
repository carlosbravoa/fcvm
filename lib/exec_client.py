#!/usr/bin/env python3
"""Run a command in a VM through the fc-init exec agent (fcvm exec / shell).

  exec_client.py [-i] [-t] VSOCK_UDS [--] [CMD ARGS...]

Talks to Firecracker's vsock Unix socket: "CONNECT <port>\\n" and then the
agent's frames (see agent_session in init/fc-init.c). No CMD means the guest's
default shell. Exits with the command's exit status.
"""
import argparse
import fcntl
import os
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty

AGENT_PORT = 1024


def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def connect(path, timeout=15):
    """Connect to the agent, retrying while the VM is still booting."""
    deadline = time.monotonic() + timeout
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            s.sendall(f"CONNECT {AGENT_PORT}\n".encode())
            line = b""
            while not line.endswith(b"\n"):
                c = s.recv(1)
                if not c:
                    raise ConnectionError("agent not listening")
                line += c
            if line.startswith(b"OK "):
                return s
            raise ConnectionError(line.decode().strip())
        except (ConnectionError, FileNotFoundError, ConnectionRefusedError) as e:
            s.close()
            if time.monotonic() > deadline:
                sys.exit(f"fcvm exec: cannot reach the exec agent ({e}). Is the VM running, and was its image built with this fcvm version?")
            time.sleep(0.2)


def winsize():
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        return rows, cols
    except OSError:
        return 24, 80


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uds")
    ap.add_argument("-i", "--interactive", action="store_true", help="keep stdin open")
    ap.add_argument("-t", "--tty", action="store_true", help="allocate a pseudo-terminal")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    use_tty = args.tty and sys.stdin.isatty()

    sock = connect(args.uds)
    rows, cols = winsize()
    fields = ["1" if use_tty else "0", str(rows), str(cols), os.environ.get("TERM", "xterm")] + cmd
    sock.sendall(frame(b"R", b"\0".join(f.encode() for f in fields) + b"\0"))

    stdin = sys.stdin.fileno() if args.interactive else None
    if stdin is None:
        sock.sendall(frame(b"C"))
    saved = None
    if use_tty:
        saved = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        signal.signal(signal.SIGWINCH, lambda *_: sock.sendall(frame(b"W", struct.pack(">HH", *winsize()))))

    buf, code = b"", 255
    try:
        while True:
            fds = [sock] + ([stdin] if stdin is not None else [])
            try:
                ready, _, _ = select.select(fds, [], [])
            except InterruptedError:
                continue
            if stdin is not None and stdin in ready:
                data = os.read(stdin, 65536)
                if data:
                    sock.sendall(frame(b"D", data))
                else:
                    sock.sendall(frame(b"C"))
                    stdin = None
            if sock in ready:
                data = sock.recv(65536)
                if not data:
                    break
                buf += data
                while len(buf) >= 5:
                    kind, n = buf[:1], struct.unpack(">I", buf[1:5])[0]
                    if len(buf) < 5 + n:
                        break
                    payload, buf = buf[5:5 + n], buf[5 + n:]
                    if kind == b"D":
                        os.write(sys.stdout.fileno(), payload)
                    elif kind == b"E":
                        os.write(sys.stderr.fileno(), payload)
                    elif kind == b"X":
                        code = struct.unpack(">i", payload)[0]
                        return code
    finally:
        if saved:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    return code


if __name__ == "__main__":
    sys.exit(main())
