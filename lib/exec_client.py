#!/usr/bin/env python3
"""Run a command in a VM through the fc-init exec agent (fcvm exec / shell).

  exec_client.py [-i] [-t] [-u USER] [-w DIR] [-e K=V]... [--timeout S] VSOCK_UDS [--] [CMD...]

Talks to Firecracker's vsock Unix socket: "CONNECT <port>\\n" and then the
agent's frames (see agent_session in init/fc-init.c). No CMD means the guest's
default shell. Exits with the command's exit status, or 124 on --timeout
(the guest-side process group is killed when the connection drops).
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


def fileop(sock, fields):
    """Run one agent file operation; data goes to stdout, errors to stderr."""
    sock.sendall(frame(b"F", b"\0".join(f.encode() for f in ["fcvm2", *fields]) + b"\0"))
    if fields[:1] == ["write"]:
        while chunk := sys.stdin.buffer.read(65536):
            sock.sendall(frame(b"D", chunk))
        sock.sendall(frame(b"C"))
    buf, out = b"", sys.stdout.buffer
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return 1
        buf += chunk
        while len(buf) >= 5:
            kind, n = buf[:1], struct.unpack(">I", buf[1:5])[0]
            if len(buf) < 5 + n:
                break
            payload, buf = buf[5:5 + n], buf[5 + n:]
            if kind == b"D":
                out.write(payload)
            elif kind == b"E":
                sys.stderr.write(payload.decode(errors="replace"))
            elif kind == b"X":
                out.flush()
                return min(struct.unpack(">i", payload)[0], 255)


def winsize():
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        return (rows, cols) if rows and cols else (24, 80)   # unset (0x0) on some ptys
    except OSError:
        return 24, 80


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uds")
    ap.add_argument("-i", "--interactive", action="store_true", help="keep stdin open")
    ap.add_argument("-t", "--tty", action="store_true", help="allocate a pseudo-terminal")
    ap.add_argument("-u", "--user", default="", help="name|uid[:group|gid], resolved in the guest")
    ap.add_argument("-w", "--workdir", default="", help="working directory (default: the image's)")
    ap.add_argument("-e", "--env", action="append", default=[], help="extra KEY=VALUE (repeatable)")
    ap.add_argument("--timeout", type=float, help="kill the command after this many seconds (exit 124)")
    ap.add_argument("--fileop", action="store_true",
                    help="CMD is a file operation (list|stat|read|write|mkdir|remove|rename|mount|umount ARGS); "
                         "write takes the content on stdin")
    ap.add_argument("--netconf", metavar="IP/PREFIX,GW,MAC,HOSTNAME",
                    help="re-identify a VM restored from a snapshot instead of running a command")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    use_tty = args.tty and sys.stdin.isatty()

    sock = connect(args.uds)
    if args.fileop:
        return fileop(sock, cmd)
    if args.netconf:
        addr, gw, mac, host = (args.netconf.split(",") + ["", "", "", ""])[:4]
        ip, _, prefix = addr.partition("/")
        sock.sendall(frame(b"N", b"\0".join(f.encode() for f in ["fcvm2", ip, prefix or "24", gw, mac, host]) + b"\0"))
        buf = b""
        while chunk := sock.recv(4096):
            buf += chunk
        code = 1
        while len(buf) >= 5:
            kind, n = buf[:1], struct.unpack(">I", buf[1:5])[0]
            payload, buf = buf[5:5 + n], buf[5 + n:]
            if kind == b"E":
                sys.stderr.write(payload.decode(errors="replace"))
            elif kind == b"X":
                code = struct.unpack(">i", payload)[0]
        return code
    rows, cols = winsize()
    fields = (["fcvm2", "1" if use_tty else "0", str(rows), str(cols), os.environ.get("TERM", "xterm"),
               args.user, args.workdir, str(len(args.env))] + args.env + cmd)
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
    deadline = time.monotonic() + args.timeout if args.timeout else None
    try:
        while True:
            fds = [sock] + ([stdin] if stdin is not None else [])
            wait = max(0, deadline - time.monotonic()) if deadline else None
            try:
                ready, _, _ = select.select(fds, [], [], wait)
            except InterruptedError:
                continue
            if deadline and not ready and time.monotonic() >= deadline:
                sock.close()   # the agent kills the command's process group
                print(f"fcvm exec: timed out after {args.timeout:g}s", file=sys.stderr)
                return 124
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
