#!/usr/bin/env python3
"""9P2000.L file server for live host directories in fcvm VMs (-v /host/dir:/path).

  share9p.py --uds-prefix VMDIR/vsock.sock --pidfile VMDIR/pid PORT=DIR[:ro] ...

Firecracker forwards a guest's vsock connection to host port P to the Unix
socket <uds_path>_P. This server listens on one such socket per shared
directory. At boot, fc-init connects and mounts the connection with the
kernel's own 9P client (mount -t 9p -o trans=fd), so the guest gets a live,
two-way view of the directory: no network, FUSE or extra guest binaries.

Operations run as the user running fcvm and are confined to the shared
directory. Every file is reported as owned by the uid:gid the guest passes in
the attach name (the image's user), so non-root images can write. Ownership
changes are accepted and ignored. Exits when the VM's Firecracker process
does.

Standard library only; one asyncio task per connection.
"""
import argparse
import asyncio
import errno
import os
import stat
import struct
import sys
import time

# 9P2000.L message types
Tlerror, Rlerror = 6, 7
Tstatfs, Rstatfs = 8, 9
Tlopen, Rlopen = 12, 13
Tlcreate, Rlcreate = 14, 15
Tsymlink, Rsymlink = 16, 17
Tmknod, Rmknod = 18, 19
Trename, Rrename = 20, 21
Treadlink, Rreadlink = 22, 23
Tgetattr, Rgetattr = 24, 25
Tsetattr, Rsetattr = 26, 27
Txattrwalk, Rxattrwalk = 30, 31
Txattrcreate, Rxattrcreate = 32, 33
Treaddir, Rreaddir = 40, 41
Tfsync, Rfsync = 50, 51
Tlock, Rlock = 52, 53
Tgetlock, Rgetlock = 54, 55
Tlink, Rlink = 70, 71
Tmkdir, Rmkdir = 72, 73
Trenameat, Rrenameat = 74, 75
Tunlinkat, Runlinkat = 76, 77
Tversion, Rversion = 100, 101
Tauth = 102
Tattach, Rattach = 104, 105
Tflush, Rflush = 108, 109
Twalk, Rwalk = 110, 111
Tread, Rread = 116, 117
Twrite, Rwrite = 118, 119
Tclunk, Rclunk = 120, 121
Tremove, Rremove = 122, 123

QTDIR, QTSYMLINK, QTFILE = 0x80, 0x02, 0x00
GETATTR_BASIC = 0x7FF
SETATTR_MODE, SETATTR_UID, SETATTR_GID, SETATTR_SIZE = 0x1, 0x2, 0x4, 0x8
SETATTR_ATIME, SETATTR_MTIME, SETATTR_ATIME_SET, SETATTR_MTIME_SET = 0x10, 0x20, 0x80, 0x100
OPEN_MASK = (os.O_ACCMODE | os.O_TRUNC | os.O_APPEND | os.O_NONBLOCK | os.O_DSYNC | os.O_SYNC
             | os.O_DIRECTORY | os.O_NOATIME)
AT_REMOVEDIR = 0x200
IOUNIT = 0          # 0: let the client use msize


class P9Error(Exception):
    def __init__(self, code):
        self.code = code


class Reader:
    def __init__(self, data):
        self.d, self.o = data, 0

    def take(self, fmt):
        v = struct.unpack_from("<" + fmt, self.d, self.o)
        self.o += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def str(self):
        n = self.take("H")
        s = self.d[self.o:self.o + n].decode("utf-8", "surrogateescape")
        self.o += n
        return s

    def rest(self, n):
        b = self.d[self.o:self.o + n]
        self.o += n
        return b


def pstr(s):
    b = s.encode("utf-8", "surrogateescape")
    return struct.pack("<H", len(b)) + b


def qid(st):
    t = QTDIR if stat.S_ISDIR(st.st_mode) else QTSYMLINK if stat.S_ISLNK(st.st_mode) else QTFILE
    return struct.pack("<BIQ", t, int(st.st_mtime) & 0xFFFFFFFF, st.st_ino)


class Fid:
    __slots__ = ("path", "fd", "listing", "xattr")

    def __init__(self, path):
        self.path, self.fd, self.listing, self.xattr = path, None, None, None


class Share:
    def __init__(self, root, readonly):
        self.root = os.path.realpath(root)
        self.ro = readonly

    def full(self, rel):
        return os.path.join(self.root, rel) if rel else self.root

    def child(self, rel, name):
        if not name or "/" in name:
            raise P9Error(errno.EINVAL)
        if name == ".":
            return rel
        if name == "..":
            return os.path.dirname(rel) if rel else ""    # never above the share root
        new = os.path.join(rel, name) if rel else name
        real = os.path.realpath(self.full(os.path.dirname(new)))
        if real != self.root and not real.startswith(self.root + os.sep):
            raise P9Error(errno.EACCES)                   # a symlinked parent pointing outside
        return new

    def writable(self):
        if self.ro:
            raise P9Error(errno.EROFS)


class Session:
    def __init__(self, share, reader, writer):
        self.share, self.r, self.w = share, reader, writer
        self.fids = {}
        self.msize = 8192
        self.owner = (0, 0)

    def fid(self, n):
        f = self.fids.get(n)
        if f is None:
            raise P9Error(errno.EBADF)
        return f

    def lstat(self, rel):
        return os.lstat(self.share.full(rel))

    def attr(self, st):
        uid, gid = self.owner
        return struct.pack("<Q", GETATTR_BASIC) + qid(st) + struct.pack(
            "<IIIQQQQQ", st.st_mode, uid, gid, st.st_nlink, st.st_rdev, st.st_size, st.st_blksize, st.st_blocks
        ) + struct.pack(
            "<QQQQQQQQQQ", int(st.st_atime), st.st_atime_ns % 10**9, int(st.st_mtime), st.st_mtime_ns % 10**9,
            int(st.st_ctime), st.st_ctime_ns % 10**9, 0, 0, 0, 0)

    # --- message handlers: return the reply body ------------------------------------
    def version(self, m):
        msize, ver = m.take("I"), m.str()
        self.msize = min(msize, 1 << 20)
        if ver != "9P2000.L":
            return struct.pack("<I", self.msize) + pstr("unknown")
        return struct.pack("<I", self.msize) + pstr("9P2000.L")

    def attach(self, m):
        fid, _afid, _uname, aname, _nuname = m.take("I"), m.take("I"), m.str(), m.str(), m.take("I")
        try:
            uid, gid = (int(x) for x in aname.split(":")[:2])
            self.owner = (uid, gid)
        except ValueError:
            pass
        self.fids[fid] = Fid("")
        return qid(self.lstat(""))

    def walk(self, m):
        fid, newfid, n = m.take("I"), m.take("I"), m.take("H")
        names = [m.str() for _ in range(n)]
        rel = self.fid(fid).path
        qids = []
        for i, name in enumerate(names):
            try:
                rel = self.share.child(rel, name)
                st = self.lstat(rel)
            except (OSError, P9Error) as e:
                if i == 0:
                    raise
                break
            qids.append(qid(st))
        if len(qids) == len(names):
            self.fids[newfid] = Fid(rel)
        return struct.pack("<H", len(qids)) + b"".join(qids)

    def getattr(self, m):
        f = self.fid(m.take("I"))
        return self.attr(os.fstat(f.fd) if f.fd is not None else self.lstat(f.path))

    def setattr(self, m):
        f = self.fid(m.take("I"))
        valid, mode, _uid, _gid, size, asec, ansec, msec, mnsec = m.take("IIIIQQQQQ")
        self.share.writable()
        p = self.share.full(f.path)
        if valid & SETATTR_MODE and not os.path.islink(p):
            os.chmod(p, stat.S_IMODE(mode))
        if valid & SETATTR_SIZE:
            os.truncate(f.fd if f.fd is not None else p, size)
        if valid & (SETATTR_ATIME | SETATTR_MTIME):
            st = os.lstat(p)
            at = (asec * 10**9 + ansec) if valid & SETATTR_ATIME_SET else (st.st_atime_ns if not valid & SETATTR_ATIME else None)
            mt = (msec * 10**9 + mnsec) if valid & SETATTR_MTIME_SET else (st.st_mtime_ns if not valid & SETATTR_MTIME else None)
            now = time.time_ns()
            os.utime(p, ns=(at if at is not None else now, mt if mt is not None else now), follow_symlinks=False)
        # uid/gid: accepted and ignored (files belong to the host user)
        return b""

    def lopen(self, m):
        f = self.fid(m.take("I"))
        flags = m.take("I")
        p = self.share.full(f.path)
        st = os.lstat(p)
        if stat.S_ISDIR(st.st_mode):
            f.listing = None
        else:
            if flags & os.O_ACCMODE != os.O_RDONLY or flags & os.O_TRUNC:
                self.share.writable()
            f.fd = os.open(p, (flags & OPEN_MASK) | os.O_NOFOLLOW | os.O_CLOEXEC)
        return qid(st) + struct.pack("<I", IOUNIT)

    def lcreate(self, m):
        f = self.fid(m.take("I"))
        name, flags, mode, _gid = m.str(), m.take("I"), m.take("I"), m.take("I")
        self.share.writable()
        rel = self.share.child(f.path, name)
        fd = os.open(self.share.full(rel), (flags & OPEN_MASK) | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     stat.S_IMODE(mode))
        f.path, f.fd = rel, fd
        return qid(os.fstat(fd)) + struct.pack("<I", IOUNIT)

    def read(self, m):
        f = self.fid(m.take("I"))
        offset, count = m.take("Q"), m.take("I")
        count = min(count, self.msize - 11)
        if f.fd is None:
            raise P9Error(errno.EBADF)
        data = os.pread(f.fd, count, offset)
        return struct.pack("<I", len(data)) + data

    def write(self, m):
        f = self.fid(m.take("I"))
        offset, count = m.take("Q"), m.take("I")
        data = m.rest(count)
        self.share.writable()
        if f.fd is None:
            raise P9Error(errno.EBADF)
        n = os.pwrite(f.fd, data, offset)
        return struct.pack("<I", n)

    def clunk(self, m):
        f = self.fids.pop(m.take("I"), None)
        if f and f.fd is not None:
            os.close(f.fd)
        return b""

    def remove(self, m):
        n = m.take("I")
        f = self.fid(n)
        self.share.writable()
        p = self.share.full(f.path)
        os.rmdir(p) if stat.S_ISDIR(os.lstat(p).st_mode) else os.unlink(p)
        self.clunk(Reader(struct.pack("<I", n)))
        return b""

    def statfs(self, m):
        self.fid(m.take("I"))
        s = os.statvfs(self.share.root)
        return struct.pack("<IIQQQQQQI", 0x01021997, s.f_bsize, s.f_blocks, s.f_bfree, s.f_bavail,
                           s.f_files, s.f_ffree, 0, s.f_namemax)

    def readdir(self, m):
        f = self.fid(m.take("I"))
        offset, count = m.take("Q"), m.take("I")
        if f.listing is None or offset == 0:
            p = self.share.full(f.path)
            entries = [(".", os.lstat(p)), ("..", os.lstat(self.share.full(os.path.dirname(f.path)) if f.path else p))]
            with os.scandir(p) as it:
                for e in sorted(it, key=lambda e: e.name):
                    try:
                        entries.append((e.name, e.stat(follow_symlinks=False)))
                    except OSError:
                        pass
            f.listing = entries
        out = b""
        limit = min(count, self.msize - 11)
        for i in range(offset, len(f.listing)):
            name, st = f.listing[i]
            dtype = stat.S_IFMT(st.st_mode) >> 12
            ent = qid(st) + struct.pack("<QB", i + 1, dtype) + pstr(name)
            if len(out) + len(ent) > limit:
                break
            out += ent
        return struct.pack("<I", len(out)) + out

    def mkdir(self, m):
        f = self.fid(m.take("I"))
        name, mode, _gid = m.str(), m.take("I"), m.take("I")
        self.share.writable()
        rel = self.share.child(f.path, name)
        os.mkdir(self.share.full(rel), stat.S_IMODE(mode))
        return qid(self.lstat(rel))

    def symlink(self, m):
        f = self.fid(m.take("I"))
        name, target, _gid = m.str(), m.str(), m.take("I")
        self.share.writable()
        rel = self.share.child(f.path, name)
        os.symlink(target, self.share.full(rel))
        return qid(self.lstat(rel))

    def mknod(self, m):
        f = self.fid(m.take("I"))
        name, mode, _maj, _min, _gid = m.str(), m.take("I"), m.take("I"), m.take("I"), m.take("I")
        self.share.writable()
        rel = self.share.child(f.path, name)
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise P9Error(errno.EPERM)                   # no device nodes on the host
        os.mknod(self.share.full(rel), mode)
        return qid(self.lstat(rel))

    def readlink(self, m):
        f = self.fid(m.take("I"))
        return pstr(os.readlink(self.share.full(f.path)))

    def link(self, m):
        dfid, fid, name = m.take("I"), m.take("I"), m.str()
        self.share.writable()
        rel = self.share.child(self.fid(dfid).path, name)
        os.link(self.share.full(self.fid(fid).path), self.share.full(rel), follow_symlinks=False)
        return b""

    def rename(self, m):
        fid, dfid, name = m.take("I"), m.take("I"), m.str()
        self.share.writable()
        f = self.fid(fid)
        rel = self.share.child(self.fid(dfid).path, name)
        os.rename(self.share.full(f.path), self.share.full(rel))
        f.path = rel
        return b""

    def renameat(self, m):
        odfid, oname, ndfid, nname = m.take("I"), m.str(), m.take("I"), m.str()
        self.share.writable()
        old = self.share.child(self.fid(odfid).path, oname)
        new = self.share.child(self.fid(ndfid).path, nname)
        os.rename(self.share.full(old), self.share.full(new))
        return b""

    def unlinkat(self, m):
        dfid, name, flags = m.take("I"), m.str(), m.take("I")
        self.share.writable()
        p = self.share.full(self.share.child(self.fid(dfid).path, name))
        os.rmdir(p) if flags & AT_REMOVEDIR else os.unlink(p)
        return b""

    def fsync(self, m):
        f = self.fid(m.take("I"))
        if f.fd is not None:
            os.fsync(f.fd)
        return b""

    def xattrwalk(self, m):
        fid, newfid, name = m.take("I"), m.take("I"), m.str()
        self.fid(fid)
        raise P9Error(errno.ENODATA if name else errno.EOPNOTSUPP)   # no xattrs exposed

    def xattrcreate(self, m):
        raise P9Error(errno.EOPNOTSUPP)

    def lock(self, m):
        self.fid(m.take("I"))
        return struct.pack("<B", 0)                        # P9_LOCK_SUCCESS (advisory, not enforced)

    def getlock(self, m):
        fid, _typ, start, length, proc_id, client_id = m.take("I"), m.take("B"), m.take("Q"), m.take("Q"), m.take("I"), m.str()
        self.fid(fid)
        return struct.pack("<BQQI", 2, start, length, proc_id) + pstr(client_id)   # F_UNLCK: no conflicts

    def flush(self, m):
        return b""

    HANDLERS = {
        Tversion: ("version", Rversion), Tattach: ("attach", Rattach), Twalk: ("walk", Rwalk),
        Tgetattr: ("getattr", Rgetattr), Tsetattr: ("setattr", Rsetattr), Tlopen: ("lopen", Rlopen),
        Tlcreate: ("lcreate", Rlcreate), Tread: ("read", Rread), Twrite: ("write", Rwrite),
        Tclunk: ("clunk", Rclunk), Tremove: ("remove", Rremove), Tstatfs: ("statfs", Rstatfs),
        Treaddir: ("readdir", Rreaddir), Tmkdir: ("mkdir", Rmkdir), Tsymlink: ("symlink", Rsymlink),
        Tmknod: ("mknod", Rmknod), Treadlink: ("readlink", Rreadlink), Tlink: ("link", Rlink),
        Trename: ("rename", Rrename), Trenameat: ("renameat", Rrenameat), Tunlinkat: ("unlinkat", Runlinkat),
        Tfsync: ("fsync", Rfsync), Txattrwalk: ("xattrwalk", Rxattrwalk), Txattrcreate: ("xattrcreate", Rxattrcreate),
        Tlock: ("lock", Rlock), Tgetlock: ("getlock", Rgetlock), Tflush: ("flush", Rflush),
    }

    def handle(self, typ, tag, body):
        h = self.HANDLERS.get(typ)
        try:
            if not h:
                raise P9Error(errno.EOPNOTSUPP)
            reply = getattr(self, h[0])(Reader(body))
            rtype = h[1]
        except P9Error as e:
            rtype, reply = Rlerror, struct.pack("<I", e.code)
        except OSError as e:
            rtype, reply = Rlerror, struct.pack("<I", e.errno or errno.EIO)
        except (struct.error, ValueError, UnicodeError):
            rtype, reply = Rlerror, struct.pack("<I", errno.EINVAL)
        return struct.pack("<IBH", 7 + len(reply), rtype, tag) + reply

    async def run(self):
        try:
            while True:
                hdr = await self.r.readexactly(7)
                size, typ, tag = struct.unpack("<IBH", hdr)
                body = await self.r.readexactly(size - 7)
                self.w.write(self.handle(typ, tag, body))
                await self.w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for f in self.fids.values():
                if f.fd is not None:
                    os.close(f.fd)
            self.w.close()


async def watch(pidfile):
    """Exit once the VM's Firecracker process is gone (after it has started)."""
    seen = False
    for _ in range(3000):
        try:
            pid = int(open(pidfile).read().strip())
            os.kill(pid, 0)
            seen = True
        except (OSError, ValueError):
            if seen:
                return
        await asyncio.sleep(0.2 if not seen else 1)
    if not seen:
        return


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uds-prefix", required=True)
    ap.add_argument("--pidfile", required=True)
    ap.add_argument("exports", nargs="+", help="PORT=DIR[:ro]")
    args = ap.parse_args()
    servers = []
    for spec in args.exports:
        port, _, d = spec.partition("=")
        ro = d.endswith(":ro")
        d = d[:-3] if ro else d
        if not os.path.isdir(d):
            sys.exit(f"share9p: not a directory: {d}")
        share = Share(d, ro)
        path = f"{args.uds_prefix}_{port}"
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        servers.append(await asyncio.start_unix_server(
            lambda r, w, s=share: Session(s, r, w).run(), path, limit=1 << 21))
        print(f"share9p: port {port} -> {d}{' (read-only)' if ro else ''}", file=sys.stderr, flush=True)
    await watch(args.pidfile)


if __name__ == "__main__":
    asyncio.run(main())
