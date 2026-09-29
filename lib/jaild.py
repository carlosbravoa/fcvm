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
  - its own network namespace holding only tap0 (owned by the jail uid) and a
    bridge, joined to the host bridge by a veth pair (fcv<i> / fcrv<i>)
  - a default ACL on the chroot so the owner can reach the VM's sockets
  - jailer --> firecracker with the console pty the caller passed over
    SCM_RIGHTS as stdin/stdout/stderr

Restores also work: with "restore": SNAPSHOT the chroot gets the snapshot's
vmstate/mem instead of a config, and fcvm loads it through the API. Paths
inside a jail are the same for every VM (/drive0.ext4, /vsock.sock, tap0),
so a snapshot of one jailed VM restores into another's jail unchanged.

No separate PID namespace: fcvm needs the VMM's real pid (liveness, stats,
stop), and the unique uid already keeps it from signalling or ptracing any
other process; the chroot has no /proc.

When Firecracker exits: unmount, drop ACLs, delete the namespace and chroot.
On startup: sweep whatever a previous run left (restart or crash).
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
        self.sweep_stale()

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
        allowed_ro = [os.path.join(self.root, d) for d in ("images", "kernels", "build", "volumes", "snapshots", f"vms/{vm}")]
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

        # Network: a namespace of its own with tap0 + br0, and a veth pair to the
        # host bridge. The pool tap only lends its index (and so the VM's IP).
        net, netns = [], None
        if req.get("tap"):
            m = TAP.match(req["tap"])
            if not m:
                raise Refused("invalid tap")
            netns = self.netns_setup(jid, m.group(1), int(m.group(2)), uid)
            state["netns"] = netns
            net = [{"iface_id": "eth0", "guest_mac": req["mac"], "host_dev_name": "tap0"}]

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
        restore = req.get("restore")
        if restore:
            if not NAME.match(restore):
                raise Refused("invalid snapshot name")
            for name, fname in (("snap.vmstate", "vmstate"), ("snap.mem", "mem")):
                src = self.checked_file(os.path.join(self.root, "snapshots", restore, fname), vm, False)
                dst = os.path.join(chroot, name)
                open(dst, "w").close()
                run("mount", "--bind", src, dst)
                run("mount", "-o", "remount,bind,ro", dst)
                state["mounts"].append(dst)
                run("setfacl", "-m", f"u:{uid}:r", src)   # 0600: guest memory; read access while it runs
                state["acls"].append(src)

        vcpus, mem = int(req["vcpus"]), int(req["mem_mib"])
        cmd = [f"{LIB}/jailer", "--id", jid, "--exec-file", f"{LIB}/firecracker", "--uid", str(uid), "--gid", str(gid),
               "--chroot-base-dir", self.base, "--cgroup-version", "2", "--parent-cgroup", self.cgroup_parent,
               "--cgroup", f"cpu.max={vcpus * 100000} 100000",
               "--cgroup", f"memory.max={(mem + VMM_OVERHEAD_MIB) << 20}",
               "--cgroup", "pids.max=128",
               "--resource-limit", "no-file=4096"]
        if netns:
            cmd += ["--netns", netns]
        cmd += ["--", "--api-sock", "/fc.sock", "--log-path", "/firecracker.log", "--level", "Warning"]
        if not restore:      # a restore is configured by fcvm through the API (snapshot load)
            cmd += ["--config-file", "/fc.json"]
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
        def fix_masks():
            pending = {os.path.join(chroot, n) for n in ("fc.sock", "vsock.sock")}
            for _ in range(3000):                   # up to a minute: restores create vsock.sock on load
                for sock in [p for p in pending if os.path.exists(p)]:
                    subprocess.run(["setfacl", "-m", "m::rwx", sock], capture_output=True)
                    pending.discard(sock)
                if not pending or proc.poll() is not None:
                    return
                time.sleep(0.02)
        fixer = threading.Thread(target=fix_masks, daemon=True)
        fixer.start()
        api = os.path.join(chroot, "fc.sock")
        for _ in range(250):                        # the API socket is usable before we reply
            if os.path.exists(api):
                subprocess.run(["setfacl", "-m", "m::rwx", api], capture_output=True)
                break
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        threading.Thread(target=self.watch, args=(vm, proc), daemon=True).start()
        log(f"launched {vm} (jail {jid}, uid {uid}, pid {proc.pid})")
        return {"pid": proc.pid, "uid": uid, "chroot": chroot}

    def netns_setup(self, jid, prefix, idx, uid):
        """netns fcvm-<id>: lo, tap0 (owned by the VM uid) and veth0 on br0; the
        veth's host end (fcv<i> / fcrv<i>) joins the pool's bridge."""
        ns = f"fcvm-{jid}"
        veth = ("fcv" if prefix == "fctap" else "fcrv") + str(idx)
        subprocess.run(["ip", "netns", "del", ns], capture_output=True)
        subprocess.run(["ip", "link", "del", veth], capture_output=True)
        run("ip", "netns", "add", ns)
        run("ip", "link", "add", veth, "type", "veth", "peer", "name", "veth0", "netns", ns)
        with open(f"/proc/sys/net/ipv6/conf/{veth}/disable_ipv6", "w") as f:   # guests are IPv4-only
            f.write("1")
        run("ip", "link", "set", veth, "master", self.bridges[prefix], "up")
        run("bridge", "link", "set", "dev", veth, "isolated", "on" if self.isolate.get(prefix) else "off")
        inside = ["ip", "netns", "exec", ns]
        for conf in ("all", "default"):
            run(*inside, "sysctl", "-q", "-w", f"net.ipv6.conf.{conf}.disable_ipv6=1")
        run(*inside, "ip", "link", "set", "lo", "up")
        run(*inside, "ip", "link", "add", "br0", "type", "bridge")
        run(*inside, "ip", "tuntap", "add", "tap0", "mode", "tap", "user", str(uid))
        for dev in ("veth0", "tap0"):
            run(*inside, "ip", "link", "set", dev, "master", "br0", "up")
        run(*inside, "ip", "link", "set", "br0", "up")
        return f"/run/netns/{ns}"

    # --- teardown --------------------------------------------------------------------
    def sweep_stale(self):
        """Jails left by a previous run (a restart or crash kills the VMs before
        their teardown): chroots, network namespaces, ACLs for jail uids."""
        jails = os.path.join(self.base, "firecracker")
        for jid in os.listdir(jails) if os.path.isdir(jails) else []:
            self.teardown_dir(os.path.join(jails, jid))
        for ns in os.listdir("/run/netns") if os.path.isdir("/run/netns") else []:
            if ns.startswith("fcvm-"):
                subprocess.run(["ip", "netns", "del", ns], capture_output=True)
        for sub in ("vms", "snapshots"):
            for dirpath, _, files in os.walk(os.path.join(self.root, sub)):
                for f in files:
                    path = os.path.join(dirpath, f)
                    if os.path.islink(path) or not os.path.isfile(path):
                        continue
                    acl = subprocess.run(["getfacl", "-n", "--omit-header", path],
                                         capture_output=True, text=True).stdout
                    for line in acl.splitlines():
                        uid = line.split(":")[1] if line.startswith("user:") else ""
                        if uid.isdigit() and self.uid_base <= int(uid) < self.uid_base + self.slots:
                            subprocess.run(["setfacl", "-x", f"u:{uid}", path], capture_output=True)

    def watch(self, vm, proc):
        proc.wait()
        state = self.running.get(vm, {})
        log(f"{vm} exited ({proc.returncode})")
        if state.get("netns"):   # deleting it takes tap0, br0 and the veth pair along
            subprocess.run(["ip", "netns", "del", os.path.basename(state["netns"])], capture_output=True)
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

    def snapshot_collect(self, req):
        """Move a jailed VM's snapshot files (written by Firecracker inside the
        chroot, owned by the VM uid) into the owner's snapshots/<name>/."""
        state = self.running.get(req["vm"])
        name = req.get("name", "")
        if not state or not NAME.match(name):
            raise Refused("unknown VM or bad snapshot name")
        dst = os.path.realpath(os.path.join(self.root, "snapshots", name))
        if not dst.startswith(os.path.join(self.root, "snapshots") + os.sep) or not os.path.isdir(dst) \
                or os.lstat(dst).st_uid != self.owner:
            raise Refused("the snapshot directory must exist and belong to the fcvm owner")
        for src, fname in (("snap.vmstate", "vmstate"), ("snap.mem", "mem")):
            s_path, d_path = os.path.join(state["chroot"], src), os.path.join(dst, fname)
            run("cp", "--sparse=always", "--no-preserve=all", s_path, d_path)
            os.chown(d_path, self.owner, self.owner)
            os.chmod(d_path, 0o600)
            os.unlink(s_path)
        return {"collected": name}

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
                elif op == "snapshot_collect":
                    reply = jd.snapshot_collect(req)
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
