#!/usr/bin/env python3
"""Per-VM serial console relay: detachable consoles for fcvm.

  console.py serve --sock S --log L --pidfile P [--on-exit CMD] -- FIRECRACKER ARGS...
  console.py attach SOCK [--replay all|tail]

serve: runs Firecracker with its serial console (stdin/stdout) on a pty,
appends all output to L, keeps a scrollback buffer and relays to clients that
attach on the unix socket S. Firecracker's pid goes to P. When Firecracker
exits, runs CMD (fcvm's reaper) and then disconnects the clients, so a client
that sees the connection close knows the VM is gone and reaped.

attach: connects this terminal to a console. Ctrl-] detaches, leaving the VM
running. Exit status: 0 when the VM went away, 2 when detached.
"""
import argparse
import array
import json
import os
import pty
import select
import socket
import subprocess
import sys
import termios
import time
import tty

DETACH = b"\x1d"          # Ctrl-]
SCROLLBACK = 1 << 20
TAIL = 4096


def jail_launch(args, slave):
    """Ask fcvm-jaild to start the VM under the jailer with our pty as its console."""
    with open(args.jail) as f:
        req = json.load(f)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/run/fcvm/jaild.sock")
    s.sendmsg([json.dumps(req).encode() + b"\n"],
              [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [slave]))])
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        reply += chunk
    s.close()
    r = json.loads(reply or b'{"ok": false, "error": "no reply from fcvm-jaild"}')
    if not r.get("ok"):
        sys.exit(f"fcvm-jaild: {r.get('error')}")
    # The VM's sockets and log live in the chroot; link them where fcvm looks.
    for name in ("fc.sock", "vsock.sock", "firecracker.log"):
        link = os.path.join(args.link_dir, name)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.join(r["chroot"], name), link)
    return r["pid"]


def alive(pid):
    return os.path.exists(f"/proc/{pid}")


def serve(args):
    master, slave = pty.openpty()
    tty.setraw(slave)   # the guest already sends \r\n; no output processing
    if args.jail:
        proc, pid = None, jail_launch(args, slave)
    else:
        proc = subprocess.Popen(args.cmd, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        pid = proc.pid
    os.close(slave)
    with open(args.pidfile, "w") as f:
        f.write(f"{pid}\n")

    log = open(args.log, "ab", buffering=0)
    try:
        os.unlink(args.sock)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.sock)
    srv.listen(8)
    scroll = bytearray()
    clients = []

    def drop(c):
        clients.remove(c)
        c.close()

    def output(data):
        nonlocal scroll
        log.write(data)
        scroll += data
        if len(scroll) > SCROLLBACK:
            del scroll[:len(scroll) - SCROLLBACK]
        for c in list(clients):
            try:
                c.sendall(data)
            except OSError:
                drop(c)

    while True:
        ready, _, _ = select.select([master, srv] + clients, [], [], 0.5)
        if master in ready:
            try:
                data = os.read(master, 65536)
            except OSError:   # EIO: Firecracker (the only slave holder) is gone
                data = b""
            if not data:
                break
            output(data)
        if srv in ready:
            c, _ = srv.accept()
            c.settimeout(5)
            try:
                mode = c.recv(1)
                if mode == b"A":
                    c.sendall(bytes(scroll))
                else:   # recent output, from a line start (not mid escape sequence)
                    tail = bytes(scroll[-TAIL:])
                    c.sendall(tail[tail.find(b"\n") + 1:] if len(scroll) > TAIL else tail)
                clients.append(c)
            except OSError:
                c.close()
        for c in [c for c in ready if c in clients]:
            try:
                data = c.recv(65536)
            except OSError:
                data = b""
            if data:
                os.write(master, data)
            else:
                drop(c)
        if (proc.poll() is not None if proc else not alive(pid)) and master not in ready:
            break

    if proc:
        proc.wait()
    else:
        while alive(pid):
            time.sleep(0.05)
    if args.on_exit:   # Firecracker's status, when we launched it (jailed: the helper knows)
        env = {**os.environ, "FCVM_FC_STATUS": str(proc.returncode)} if proc else None
        subprocess.run(args.on_exit, shell=True, env=env)
    for c in list(clients):
        drop(c)
    try:
        os.unlink(args.sock)
    except FileNotFoundError:
        pass


def attach(args):
    deadline = time.monotonic() + 5
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(args.sock)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if time.monotonic() > deadline:
                sys.exit("fcvm: console not available (is the VM running?)")
            time.sleep(0.05)
    s.sendall(b"A" if args.replay == "all" else b"T")

    stdin = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin) if os.isatty(stdin) else None
    if saved:
        tty.setraw(stdin)
    watch_stdin, status = True, 0
    try:
        while True:
            ready, _, _ = select.select([s] + ([stdin] if watch_stdin else []), [], [])
            if stdin in ready:
                data = os.read(stdin, 4096)
                if not data:
                    watch_stdin = False       # stdin closed: keep showing output
                elif DETACH in data:
                    s.sendall(data.split(DETACH, 1)[0])
                    status = 2
                    break
                else:
                    s.sendall(data)
            if s in ready:
                data = s.recv(65536)
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
    finally:
        if saved:
            termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
    return status


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--sock", required=True)
    sv.add_argument("--log", required=True)
    sv.add_argument("--pidfile", required=True)
    sv.add_argument("--on-exit")
    sv.add_argument("--jail", help="launch request for fcvm-jaild (instead of CMD)")
    sv.add_argument("--link-dir", help="where to link the jailed VM's sockets and log")
    sv.add_argument("cmd", nargs=argparse.REMAINDER)
    at = sub.add_parser("attach")
    at.add_argument("sock")
    at.add_argument("--replay", choices=["all", "tail"], default="tail")
    args = ap.parse_args()
    if args.mode == "serve":
        args.cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        serve(args)
        return 0
    return attach(args)


if __name__ == "__main__":
    sys.exit(main())
