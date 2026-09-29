#!/usr/bin/env python3
"""fcvm-jaild: root helper that launches fcvm VMs under the Firecracker jailer.

Installed root-owned by `sudo fcvm jail-setup` (copies of this file, jailer and
firecracker go to /usr/local/lib/fcvm) and run by systemd with a delegated
cgroup and a private mount namespace. Speaks JSON lines on a Unix socket that
only the configured owner can use.

A launch request names a VM and the files it needs; nothing is taken on
trust. Every file must resolve inside the owner's fcvm tree (images/,
vms/<vm>/, volumes/, kernels/, build/), be a regular file owned by the owner,
and only the VM's own disk and volumes may be writable. Then, per VM:

  - a chroot /srv/jailer/firecracker/<id>/root with the files bind-mounted in
    (read-only where they must be)
  - its own uid/gid (uid_base + slot), given access to its writable files by
    ACL, not ownership, so the owner's tools keep working
  - cgroup v2 limits: cpu.max from vCPUs, memory.max from memory + VMM
    overhead, pids.max
  - its tap re-created owned by the jail uid while it runs
  - a default ACL on the chroot so the owner can reach the VM's sockets
  - jailer --> firecracker with the console pty the caller passed over
    SCM_RIGHTS as stdin/stdout/stderr

When Firecracker exits: unmount, drop ACLs, give the tap back, delete the chroot.
"""
import argparse
import array
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

LIB = "/usr/local/lib/fcvm"
SOCKET = "/run/fcvm/jaild.sock"
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
TAP = re.compile(r"^(fctap|fcrtap)(\d+)$")
VMM_OVERHEAD_MIB = 256


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class Refused(Exception):
    pass


def run(*cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise Refused(f"{' '.join(cmd[:3])}: {p.stderr.strip() or p.returncode}")


def jail_id(vm):
    """Jailer ids allow [A-Za-z0-9-]; keep them unique for names with _ or ."""
    clean = re.sub(r"[^A-Za-z0-9-]", "-", vm)[:48]
    return clean if clean == vm else f"{clean}-{hashlib.sha1(vm.encode()).hexdigest()[:8]}"


class Jaild:
    def __init__(self, cfg):
        self.cfg = cfg
        self.owner = cfg["owner_uid"]
        self.root = os.path.realpath(cfg["fcvm_root"])
        self.base = cfg.get("jail_base", "/srv/jailer")
        self.uid_base = cfg.get("uid_base", 900000)
        self.slots = cfg.get("slots", 256)
        self.bridges = cfg.get("bridges", {"fctap": "fcbr0", "fcrtap": "fcbr1"})
        self.isolate = cfg.get("isolate", {"fctap": False, "fcrtap": True})
        self.running = {}            # vm -> {pid, slot, id, mounts, acls, tap}
        self.lock = threading.Lock()
        self.cgroup_parent = self.setup_cgroup()

    # --- setup ------------------------------------------------------------------------
    def setup_cgroup(self):
        """Move ourselves into <service>/helper so the service cgroup can hand
        cpu/memory/pids controllers down to per-VM cgroups (cgroup v2 rule)."""
        with open("/proc/self/cgroup") as f:
            own = f.read().strip().split("::", 1)[1]
        svc = "/sys/fs/cgroup" + own
        if os.path.basename(own) == "helper":
            svc = os.path.dirname(svc)
        helper = os.path.join(svc, "helper")
        os.makedirs(helper, exist_ok=True)
        with open(os.path.join(helper, "cgroup.procs"), "w") as f:
            f.write(str(os.getpid()))
        with open(os.path.join(svc, "cgroup.subtree_control"), "w") as f:
            f.write("+cpu +memory +pids +io")
        return os.path.relpath(svc, "/sys/fs/cgroup")

    # --- validation -------------------------------------------------------------------
    def checked_file(self, path, vm, writable):
        real = os.path.realpath(path)
        allowed_ro = [os.path.join(self.root, d) for d in ("images", "kernels", "build", "volumes", f"vms/{vm}")]
        allowed_rw = [os.path.join(self.root, d) for d in ("volumes", f"vms/{vm}")]
        if not any(real.startswith(d + os.sep) for d in (allowed_rw if writable else allowed_ro)):
            raise Refused(f"not an fcvm file this VM may use{' writably' if writable else ''}: {path}")
        st = os.lstat(real)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != self.owner:
            raise Refused(f"not a regular file owned by the fcvm owner: {path}")
        return real

    # --- launch -----------------------------------------------------------------------
    def launch(self, req, fd):
        vm = req["vm"]
        if not NAME.match(vm):
            raise Refused("invalid VM name")
        with self.lock:
            if vm in self.running:
                raise Refused(f"VM '{vm}' is already running in a jail")
            used = {r["slot"] for r in self.running.values()}
            slot = next((i for i in range(self.slots) if i not in used), None)
            if slot is None:
                raise Refused("no free jail slots")
            self.running[vm] = {"slot": slot, "pid": None}
        try:
            return self._launch(vm, slot, req, fd)
        except Exception:
            self.running.pop(vm, None)
            raise

    def _launch(self, vm, slot, req, fd):
        uid = gid = self.uid_base + slot
        jid = jail_id(vm)
        chroot = os.path.join(self.base, "firecracker", jid, "root")
        if os.path.exists(os.path.dirname(chroot)):
            self.teardown_dir(os.path.dirname(chroot))
        os.makedirs(chroot, mode=0o755)
        state = self.running[vm]
        state.update({"id": jid, "chroot": chroot, "mounts": [], "acls": [], "tap": None})

        # Files: bind-mounted into the chroot under fixed names.
        kernel = self.checked_file(req["kernel"], vm, False)
        initrd = self.checked_file(req["initrd"], vm, False)
        files = [("vmlinux", kernel, False), ("initramfs.cpio", initrd, False)]
        drives = []
        for i, d in enumerate(req["drives"]):
            writable = not d["read_only"]
            src = self.checked_file(d["path"], vm, writable)
            name = f"drive{i}.ext4"
            files.append((name, src, writable))
            drives.append({"drive_id": d["drive_id"], "path_on_host": f"/{name}",
                           "is_root_device": False, "is_read_only": not writable})
        for name, src, writable in files:
            dst = os.path.join(chroot, name)
            open(dst, "w").close()
            run("mount", "--bind", src, dst)
            state["mounts"].append(dst)
            if not writable:
                run("mount", "-o", "remount,bind,ro", dst)
            else:
                run("setfacl", "-m", f"u:{uid}:rw", src)
                state["acls"].append(src)

        # The owner reaches the VM's sockets (api, vsock, console) and creates
        # share sockets here; the jailed uid creates its own. Default ACLs make
        # every new socket usable by both.
        os.chown(chroot, 0, 0)
        run("setfacl", "-m", f"u:{uid}:rwx,u:{self.owner}:rwx,d:u:{uid}:rwx,d:u:{self.owner}:rwx", chroot)
        for p in (os.path.dirname(chroot), os.path.dirname(os.path.dirname(chroot)), self.base):
            os.chmod(p, 0o755)
        for name in ("firecracker.log",):
            path = os.path.join(chroot, name)
            open(path, "w").close()
            os.chown(path, uid, gid)
            run("setfacl", "-m", f"u:{self.owner}:r", path)

        # Network: the pool tap, re-created owned by the jail uid for the VM's lifetime.
        net = []
        if req.get("tap"):
            m = TAP.match(req["tap"])
            if not m:
                raise Refused("invalid tap")
            self.own_tap(req["tap"], uid)
            state["tap"] = req["tap"]
            net = [{"iface_id": "eth0", "guest_mac": req["mac"], "host_dev_name": req["tap"]}]

        config = {
            "boot-source": {"kernel_image_path": "/vmlinux", "initrd_path": "/initramfs.cpio",
                            "boot_args": req["boot_args"]},
            "drives": drives,
            "machine-config": {"vcpu_count": int(req["vcpus"]), "mem_size_mib": int(req["mem_mib"])},
            "network-interfaces": net,
            "vsock": {"guest_cid": 3, "uds_path": "/vsock.sock"},
            "entropy": {},
        }
        with open(os.path.join(chroot, "fc.json"), "w") as f:
            json.dump(config, f)

        vcpus, mem = int(req["vcpus"]), int(req["mem_mib"])
        cmd = [f"{LIB}/jailer", "--id", jid, "--exec-file", f"{LIB}/firecracker", "--uid", str(uid), "--gid", str(gid),
               "--chroot-base-dir", self.base, "--cgroup-version", "2", "--parent-cgroup", self.cgroup_parent,
               "--cgroup", f"cpu.max={vcpus * 100000} 100000",
               "--cgroup", f"memory.max={(mem + VMM_OVERHEAD_MIB) << 20}",
               "--cgroup", "pids.max=128",
               "--resource-limit", "no-file=4096", "--",
               "--api-sock", "/fc.sock", "--config-file", "/fc.json", "--log-path", "/firecracker.log", "--level", "Warning"]
        proc = subprocess.Popen(cmd, stdin=fd, stdout=fd, stderr=fd, start_new_session=True, close_fds=True)
        os.close(fd)
        state["pid"] = proc.pid
        # The jailer chowns the chroot to the VM uid and chmods it 0700, and on a
        # directory with ACLs chmod also rewrites the ACL mask to ---, disabling
        # the owner's and the VM's entries. Restore the mask once that happened.
        for _ in range(150):
            st = os.stat(chroot)
            if st.st_uid == uid and st.st_mode & 0o070 == 0:
                break
            time.sleep(0.02)
        for delay in (0, 0.3):
            time.sleep(delay)
            run("setfacl", "-m", "m::rwx", chroot)
        # Firecracker creates its API and vsock sockets with mode 0755, which on
        # a file with ACLs caps the mask at r-x; connecting needs w. Fix both
        # once they exist (within milliseconds of start).
        pending = {os.path.join(chroot, n) for n in ("fc.sock", "vsock.sock")}
        for _ in range(250):
            for sock in [p for p in pending if os.path.exists(p)]:
                run("setfacl", "-m", "m::rwx", sock)
                pending.discard(sock)
            if not pending or proc.poll() is not None:
                break
            time.sleep(0.02)
        threading.Thread(target=self.watch, args=(vm, proc), daemon=True).start()
        log(f"launched {vm} (jail {jid}, uid {uid}, pid {proc.pid})")
        return {"pid": proc.pid, "uid": uid, "chroot": chroot}

    def own_tap(self, tap, uid):
        prefix = TAP.match(tap).group(1)
        run("ip", "tuntap", "del", tap, "mode", "tap")
        run("ip", "tuntap", "add", tap, "mode", "tap", "user", str(uid))
        run("ip", "link", "set", tap, "master", self.bridges[prefix], "up")
        run("bridge", "link", "set", "dev", tap, "isolated", "on" if self.isolate.get(prefix) else "off")

    # --- teardown --------------------------------------------------------------------
    def watch(self, vm, proc):
        proc.wait()
        state = self.running.get(vm, {})
        log(f"{vm} exited ({proc.returncode})")
        try:
            if state.get("tap"):
                self.own_tap(state["tap"], self.owner)
        except Refused as e:
            log(f"{vm}: giving the tap back failed: {e}")
        for src in state.get("acls", []):
            subprocess.run(["setfacl", "-x", f"u:{self.uid_base + state['slot']}", src], capture_output=True)
        if state.get("chroot"):
            self.teardown_dir(os.path.dirname(state["chroot"]))
        with self.lock:
            self.running.pop(vm, None)

    def teardown_dir(self, jail_dir):
        root = os.path.join(jail_dir, "root")
        if os.path.isdir(root):
            for name in os.listdir(root):
                p = os.path.join(root, name)
                subprocess.run(["umount", "-l", p], capture_output=True)
        shutil.rmtree(jail_dir, ignore_errors=True)

    def kill(self, req):
        state = self.running.get(req["vm"])
        if not state or not state.get("pid"):
            raise Refused("that VM is not running in a jail")
        # jailer execs firecracker, so the launch pid is Firecracker's
        os.kill(state["pid"], signal.SIGKILL)
        return {"killed": req["vm"]}

    def status(self, _req):
        return {vm: {k: v for k, v in s.items() if k in ("pid", "slot", "id", "tap")} for vm, s in self.running.items()}


# --- the socket server ---------------------------------------------------------------

def recv_request(conn):
    """One JSON line, plus an optional fd sent with SCM_RIGHTS."""
    fds = array.array("i")
    data, anc, _flags, _addr = conn.recvmsg(65536, socket.CMSG_SPACE(fds.itemsize))
    for level, typ, payload in anc:
        if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
            fds.frombytes(payload[:len(payload) - len(payload) % fds.itemsize])
    while not data.endswith(b"\n"):
        more = conn.recv(65536)
        if not more:
            break
        data += more
    return json.loads(data), (fds[0] if fds else None)


def serve(jd):
    os.makedirs(os.path.dirname(SOCKET), exist_ok=True)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET)
    os.chown(SOCKET, jd.owner, 0)
    os.chmod(SOCKET, 0o600)
    srv.listen(16)
    log(f"fcvm-jaild: listening on {SOCKET} for uid {jd.owner}; cgroup parent {jd.cgroup_parent}")

    def handle(conn):
        with conn:
            fd = None
            try:
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                _pid, uid, _gid = array.array("i", creds)
                if uid != jd.owner:
                    raise Refused("not the fcvm owner")
                req, fd = recv_request(conn)
                op = req.get("op")
                if op == "launch":
                    if fd is None:
                        raise Refused("launch needs the console pty")
                    reply = jd.launch(req, fd)
                    fd = None
                elif op == "kill":
                    reply = jd.kill(req)
                elif op == "status":
                    reply = jd.status(req)
                else:
                    raise Refused(f"unknown op {op!r}")
                conn.sendall(json.dumps({"ok": True, **reply}).encode() + b"\n")
            except (Refused, OSError, ValueError, KeyError, TypeError) as e:
                conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
            finally:
                if fd is not None:
                    os.close(fd)

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/fcvm/jaild.json")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("fcvm-jaild must run as root (sudo fcvm jail-setup installs it as a service)")
    with open(args.config) as f:
        cfg = json.load(f)
    jd = Jaild(cfg)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(jd)


if __name__ == "__main__":
    main()
