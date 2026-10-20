"""VM supervisor for fcvm serve: recovery after a reboot, and restart policies.

Restart policies (vm.json "restart", set with `fcvm create --restart` or
`fcvm update`) follow Docker's:

  no              never restarted (the default)
  on-failure      restarted when it fails: an app exits non-zero, or Firecracker
                  dies (killed, crashed) instead of the guest shutting down
  unless-stopped  restarted whenever it exits, unless you stopped it
  always          as unless-stopped, and also started at every host boot even
                  if you had stopped it

"You stopped it" is the `stopped` marker `fcvm stop` leaves (and `start`
removes). Exits are recorded in `last-exit` by the reaper. Restarts back off
from 1 s, doubling up to 60 s, and the count resets once a VM has run for a
minute.

Warm pools (vms/.pools/NAME.json, from `fcvm pool set`) are kept at their
size here: a VM is booted (or forked) per tick while one is missing, and
pooled VMs that died are removed.

Lifetimes (vm.json "ttl_s" and "idle_stop_s", from `--ttl` / `--idle-stop`)
are applied here too: a running VM is stopped once its TTL since boot, or its
time since the last exec/shell/cp/console operation, runs out.

At startup the supervisor also cleans up after a crash or reboot: a VM whose
pid file doesn't match a live process of its own is reaped as stale, and VMs
that were running then (stale, or marked `resume` by `fcvm _shutdown`) come
back if their policy isn't "no".

Everything goes through the fcvm CLI, like the rest of fcvm serve.
"""
import asyncio
import json
import os
import re
import sys
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

BACKOFF_MAX = 60
STABLE_AFTER = 60     # seconds of uptime that reset the backoff
REAP_GRACE = 10       # a dead VM's relay gets this long to reap it before we do
POLL = 2.0


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def boot_id():
    return read("/proc/sys/kernel/random/boot_id").strip()


def proc_start(pid):
    stat = read(f"/proc/{pid}/stat")
    return stat[stat.rfind(")") + 2:].split()[19] if stat else ""


def vm_state(d):
    """running | exiting (the relay is reaping it) | stale (left by a crash or
    reboot) | stopped. Mirrors vm_running in lib/vm.sh."""
    pid = read(os.path.join(d, "pid")).strip()
    if not pid:
        return "stopped"
    ident = read(os.path.join(d, "pid.id")).split()
    if os.path.exists(f"/proc/{pid}"):
        if ident:
            if ident == [boot_id(), proc_start(pid)]:
                return "running"
        elif read(f"/proc/{pid}/comm").strip() == "firecracker":
            return "running"
        return "stale"                          # the pid now belongs to something else
    if ident and ident[0] != boot_id():
        return "stale"                          # the host rebooted
    try:
        age = time.time() - os.stat(os.path.join(d, "pid")).st_mtime
    except OSError:
        return "stopped"
    return "stale" if age > REAP_GRACE else "exiting"


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def expiry(cfg, booted, active, now):
    """Why a running VM should stop now: "ttl" (vm.json ttl_s since its pid
    file was written) or "idle" (idle_stop_s since its last activity, which
    exec/shell/cp and the console's agent operations record), else None."""
    ttl, idle = cfg.get("ttl_s"), cfg.get("idle_stop_s")
    if ttl and booted and now - booted >= ttl:
        return "ttl"
    if idle and (active or booted) and now - (active or booted) >= idle:
        return "idle"
    return None


def failed(last):
    """Did this exit (a last-exit record) end in failure?"""
    if last.get("stale"):
        return True                             # it was running when the host went down
    if last.get("code") not in (None, 0):
        return True                             # an app VM's process exited non-zero
    return last.get("fc_status") not in (None, 0)   # Firecracker killed or crashed


class Supervisor:
    def __init__(self, fcvm_bin, vms_dir):
        self.fcvm, self.vms = fcvm_bin, vms_dir
        self.pending = {}     # vm -> time of the next start attempt
        self.tries = {}       # vm -> consecutive restarts without a stable run
        self.started = {}     # vm -> when we last started it
        self.handled = {}     # vm -> "at" of the last exit we acted on
        self.errors = {}      # vm -> the last failed start's message
        self.pool_retry = {}  # pool -> when to try filling it again after a failure

    def log(self, msg):
        print(f"supervisor: {msg}", file=sys.stderr, flush=True)

    def vm_dirs(self):
        for name in sorted(os.listdir(self.vms)):
            d = os.path.join(self.vms, name)
            if not name.startswith(".") and os.path.isfile(os.path.join(d, "vm.json")):
                yield name, d

    @staticmethod
    def config(d):
        try:
            with open(os.path.join(d, "vm.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def last_exit(d):
        try:
            return json.loads(read(os.path.join(d, "last-exit")) or "null") or {}
        except ValueError:
            return {}

    def status(self, name):
        """What the web console shows for a VM: pending restart, attempts, last error."""
        if name not in self.pending and name not in self.errors:
            return None
        return {"restart_at": self.pending.get(name), "attempts": self.tries.get(name, 0),
                "error": self.errors.get(name)}

    async def cli(self, *args, env=None):
        p = await asyncio.create_subprocess_exec(
            self.fcvm, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**os.environ, "FCVM_ACTOR": "supervisor", **(env or {})})
        out, _ = await p.communicate()
        return p.returncode, ANSI.sub("", out.decode(errors="replace"))

    async def reap_stale(self, name):
        self.log(f"{name}: reaping (it was running when the host crashed or rebooted)")
        await self.cli("_reap", name, env={"FCVM_STALE": "1"})

    def schedule(self, name, delay):
        self.pending[name] = time.time() + delay

    async def start(self, name):
        self.pending.pop(name, None)
        code, out = await self.cli("start", name)
        if code == 0:
            self.started[name] = time.time()
            self.errors.pop(name, None)
            self.log(f"{name}: started")
            return
        n = self.tries.get(name, 0) + 1
        self.tries[name] = n
        err = [l for l in out.splitlines() if "error" in l.lower() and not l.startswith("fcvm-error:")]
        self.errors[name] = (err[-1] if err else out.strip()[-300:]).replace("error: ", "")
        if "already running" in out:
            self.errors.pop(name, None)
            return
        delay = min(BACKOFF_MAX, 2 ** n)
        self.log(f"{name}: start failed ({self.errors[name]}); retrying in {delay} s")
        self.schedule(name, delay)

    # --- at startup ---------------------------------------------------------------
    async def recover(self):
        marker = os.path.join(self.vms, ".supervisor-boot")
        first_since_boot = read(marker).strip() != boot_id()
        with open(marker, "w") as f:
            f.write(boot_id() + "\n")
        todo = []
        for name, d in list(self.vm_dirs()):
            state = vm_state(d)
            was_running = state == "stale" or os.path.exists(os.path.join(d, "resume"))
            if state == "stale":
                await self.reap_stale(name)
                if not os.path.isdir(d):
                    continue                    # a throwaway VM: the reap deleted it
            self.handled[name] = self.last_exit(d).get("at", 0)
            if state in ("running", "exiting"):
                continue
            policy = self.config(d).get("restart", "no")
            stopped = os.path.exists(os.path.join(d, "stopped"))
            if policy == "always" and first_since_boot:
                todo.append(name)
            elif policy != "no" and was_running and not stopped:
                todo.append(name)
        if todo:
            self.log(f"bringing back: {', '.join(todo)}")
        sem = asyncio.Semaphore(4)

        async def one(n):
            async with sem:
                await self.start(n)
        await asyncio.gather(*(one(n) for n in todo))

    # --- while running ---------------------------------------------------------------
    async def expire(self, name, d, cfg, now):
        """Stop a running VM whose --ttl or --idle-stop ran out. The stop
        counts as the user's (restart policies then leave it alone) and says
        why in `expired`; a throwaway VM is deleted by its reap. A VM waiting
        in a pool doesn't age; a claimed one counts from its claim."""
        if os.path.exists(os.path.join(d, "pooled")):
            return False
        booted = max(filter(None, (mtime(os.path.join(d, "pid")), mtime(os.path.join(d, "claimed")))), default=None)
        why = expiry(cfg, booted, mtime(os.path.join(d, "activity")), now)
        if not why:
            return False
        self.log(f"{name}: stopping ({'ttl' if why == 'ttl' else 'idle time'} ran out)")
        code, out = await self.cli("stop", name, env={"FCVM_EXPIRED": why, "QUIET_STOP": "1"})
        if code != 0:
            self.log(f"{name}: stop failed: {out.strip()[-300:]}")
        return True

    # --- warm pools ---------------------------------------------------------------------
    async def pools(self, now):
        """Keep every pool (vms/.pools/NAME.json) at its size: one new VM per
        tick, dead ones removed; a failing fill is retried after a while."""
        pools_dir = os.path.join(self.vms, ".pools")
        for f in sorted(os.listdir(pools_dir)) if os.path.isdir(pools_dir) else []:
            if not f.endswith(".json"):
                continue
            pool = f[:-5]
            try:
                with open(os.path.join(pools_dir, f)) as fh:
                    size = int(json.load(fh).get("size", 0))
            except (OSError, ValueError):
                continue
            members = [(n, d) for n, d in self.vm_dirs() if read(os.path.join(d, "pooled")).strip() == pool]
            ready = 0
            for n, d in members:
                state = vm_state(d)
                if state == "running":
                    ready += 1
                elif state == "stopped" or state == "stale":
                    self.log(f"pool {pool}: {n} is not running any more; removing it")
                    await self.cli("rm", n)
            if ready < size and now >= self.pool_retry.get(pool, 0):
                code, out = await self.cli("pool", "_fill", pool)
                if code == 0:
                    self.log(f"pool {pool}: {out.strip().splitlines()[-1] if out.strip() else 'filled'} ready ({ready + 1}/{size})")
                else:
                    self.log(f"pool {pool}: fill failed: {out.strip()[-300:]}; retrying in 30 s")
                    self.pool_retry[pool] = now + 30

    async def tick(self):
        now = time.time()
        await self.pools(now)
        for name, d in list(self.vm_dirs()):
            cfg = self.config(d)
            policy = cfg.get("restart", "no")
            state = vm_state(d)
            if state == "running" and await self.expire(name, d, cfg, now):
                continue
            if policy == "no":
                self.pending.pop(name, None)
                continue
            if state == "running":
                if now - self.started.get(name, now) > STABLE_AFTER:
                    self.tries.pop(name, None)
                continue
            if state == "exiting":
                continue
            if state == "stale":
                await self.reap_stale(name)
            if os.path.exists(os.path.join(d, "stopped")):
                self.pending.pop(name, None)    # you stopped it: hands off
                self.tries.pop(name, None)
                self.errors.pop(name, None)
                continue
            last = self.last_exit(d)
            if last and last.get("at", 0) > self.handled.get(name, 0):
                self.handled[name] = last["at"]
                if policy in ("always", "unless-stopped") or (policy == "on-failure" and failed(last)):
                    n = self.tries.get(name, 0)
                    self.tries[name] = n + 1
                    delay = min(BACKOFF_MAX, 2 ** n)
                    why = f"exit code {last['code']}" if last.get("code") is not None else \
                        f"firecracker status {last.get('fc_status')}"
                    self.log(f"{name}: exited ({why}); restarting in {delay} s (policy {policy})")
                    self.schedule(name, delay)
                    await self.cli("_event", "vm.restarting", name, f"policy:{policy}",
                                   json.dumps({"in": delay, "attempt": n + 1}))
            if name in self.pending and now >= self.pending[name]:
                await self.start(name)

    async def run(self):
        try:
            await self.recover()
        except Exception as e:                  # never take the console down
            self.log(f"recovery failed: {e}")
        while True:
            await asyncio.sleep(POLL)
            try:
                await self.tick()
            except Exception as e:
                self.log(f"error: {e}")
