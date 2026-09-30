#!/usr/bin/env python3
"""userns.py CMD [ARGS...]: run CMD as root inside a new user namespace where
uid/gid 0 is you and 1..N are your subordinate ranges (/etc/subuid,
/etc/subgid). There, CMD can create files owned by any of those ids, which
appear on the host as your subordinate ids; this is how mmdebstrap and
rootless containers work. Needs newuidmap/newgidmap (the uidmap package).

The maps are written explicitly with newuidmap/newgidmap rather than through
`unshare --map-auto`, whose combination with --map-root-user differs across
util-linux versions (it maps only root on Ubuntu 24.04's 2.39).
"""
import ctypes
import os
import pwd
import subprocess
import sys

CLONE_NEWUSER = 0x10000000


def unshare_user():
    if hasattr(os, "unshare"):      # Python 3.12+
        os.unshare(os.CLONE_NEWUSER)
    elif ctypes.CDLL(None, use_errno=True).unshare(CLONE_NEWUSER) != 0:
        raise OSError(ctypes.get_errno(), "unshare(CLONE_NEWUSER)")


def subrange(path, user, uid):
    with open(path) as f:
        for line in f:
            name, start, count = (line.strip().split(":") + ["", "", ""])[:3]
            if name in (user, str(uid)) and start.isdigit() and count.isdigit():
                return start, count
    sys.exit(f"userns: no range for {user} in {path} (run: fcvm host-setup)")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: userns.py CMD [ARGS...]")
    uid, gid = os.getuid(), os.getgid()
    user = pwd.getpwuid(uid).pw_name
    su, sn = subrange("/etc/subuid", user, uid)
    sg, gn = subrange("/etc/subgid", user, uid)

    go_r, go_w = os.pipe()        # parent -> child: the maps are written
    ready_r, ready_w = os.pipe()  # child -> parent: the namespace exists
    pid = os.fork()
    if pid == 0:
        try:
            os.close(go_w)
            os.close(ready_r)
            unshare_user()
            os.write(ready_w, b"x")
            if os.read(go_r, 1) != b"x":
                os._exit(1)
            os.setgid(0)
            os.setgroups([])
            os.setuid(0)
            os.execvp(sys.argv[1], sys.argv[1:])
        except OSError as e:
            print(f"userns: {e}", file=sys.stderr)
            os._exit(1)
    os.close(go_r)
    os.close(ready_w)
    os.read(ready_r, 1)
    try:
        subprocess.run(["newuidmap", str(pid), "0", str(uid), "1", "1", su, sn], check=True)
        subprocess.run(["newgidmap", str(pid), "0", str(gid), "1", "1", sg, gn], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        os.kill(pid, 9)
        sys.exit(f"userns: couldn't map ids ({e}); needs the uidmap package and subuid/subgid ranges")
    os.write(go_w, b"x")
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
