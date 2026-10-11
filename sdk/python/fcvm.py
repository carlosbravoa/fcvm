"""fcvm: a client for the fcvm HTTP API (fcvm serve, or the fcvm service).

    from fcvm import Client

    c = Client()                      # the local server: vms/.serve.json, or FCVM_URL + FCVM_TOKEN
    with c.create("box", template="python-sandbox", ttl="1h") as sb:
        r = sb.exec("pip install -q requests && python -c 'import requests; print(requests.__version__)'")
        print(r.exit_code, r.stdout)
        sb.write_file("/work/app.py", "print('hi')\\n")
        job = sb.exec("python -m http.server 8000", background=True)
        port = sb.publish(8000)       # http://127.0.0.1:PORT on the host
    # the sandbox is removed when the block ends

Everything maps onto the API documented in docs/api.md, which maps onto the
CLI. Standard library only; Python 3.9+.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["Client", "Sandbox", "Snapshot", "Job", "ServerJob", "ExecResult", "FcvmError"]
__version__ = "0.7.0"


class FcvmError(Exception):
    """An error from the server: `status` is the HTTP status, the message fcvm's."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ExecResult:
    def __init__(self, d):
        self.exit_code = d.get("exit_code")
        self.stdout = d.get("stdout", "")
        self.stderr = d.get("stderr", "")
        self.timed_out = bool(d.get("timed_out"))

    @property
    def ok(self):
        return self.exit_code == 0

    def check(self):
        """self, or FcvmError when the command failed."""
        if not self.ok:
            raise FcvmError(0, f"command exited {self.exit_code}: {self.stderr.strip() or self.stdout.strip()}")
        return self

    def __repr__(self):
        return f"ExecResult(exit_code={self.exit_code}, stdout={self.stdout!r}, stderr={self.stderr!r})"


def discover():
    """(url, token) of the local server, from the environment or vms/.serve.json."""
    if os.environ.get("FCVM_URL") and os.environ.get("FCVM_TOKEN"):
        return os.environ["FCVM_URL"].rstrip("/"), os.environ["FCVM_TOKEN"]
    homes = [os.environ.get("FCVM_HOME"), os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "fcvm")]
    if shutil.which("fcvm"):        # an fcvm run from a checkout keeps its state in the checkout
        try:
            homes.append(subprocess.run(["fcvm", "_home"], capture_output=True, text=True, timeout=10).stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    for home in filter(None, homes):
        try:
            with open(os.path.join(home, "vms", ".serve.json")) as f:
                s = json.load(f)
            return s["url"].split("/?")[0], s["token"]
        except (OSError, ValueError, KeyError):
            continue
    raise FcvmError(0, "no fcvm server found: run `fcvm serve` (or `fcvm service install`), or set FCVM_URL and FCVM_TOKEN")


class Client:
    def __init__(self, url=None, token=None, timeout=600):
        if not (url and token):
            url, token = discover()
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    # --- plumbing ---------------------------------------------------------------------
    def request(self, method, path, body=None, params=None, data=None, raw=False):
        url = f"{self.url}/api/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = data
        if body is not None:
            payload, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                content = r.read()
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read().decode())["error"]
            except (ValueError, KeyError):
                message = f"HTTP {e.code}"
            raise FcvmError(e.code, message) from None
        except urllib.error.URLError as e:
            raise FcvmError(0, f"cannot reach {self.url}: {e.reason}") from None
        if raw:
            return content
        return json.loads(content) if content else None

    def get(self, path, /, **params):            # positional-only: a query parameter may be named path
        return self.request("GET", path, params=params or None)

    def post(self, path, /, body=None):
        return self.request("POST", path, body=body or {})

    def delete(self, path, /, **params):
        return self.request("DELETE", path, params=params or None)

    # --- the host -------------------------------------------------------------------------
    def host(self):
        return self.get("host")

    def vms(self):
        return [Sandbox(self, v["name"], v) for v in self.get("vms")]

    def sandbox(self, name):
        """An existing VM by name (checked)."""
        return Sandbox(self, name, self.get(f"vms/{name}"))

    def images(self):
        return self.get("images")

    def templates(self):
        return self.get("templates")

    def snapshots(self):
        return [Snapshot(self, s["name"], s) for s in self.get("snapshots")]

    def volumes(self):
        return self.get("volumes")

    def pools(self):
        return self.get("pools")

    def import_image(self, ref, name=None):
        """Start an import; returns a ServerJob to wait on."""
        return ServerJob(self, self.post("images", {"ref": ref, **({"name": name} if name else {})}))

    def create(self, name, image=None, template=None, start=True, **opts):
        """Create (and start) a VM. opts are the fields of POST /vms: vcpus, mem_mib,
        network ("full"|"none"|"restricted"), allow, ports, volumes, idle, command,
        jail, restart, ttl, idle_stop, secrets ({NAME: value}), forward, cache, disk,
        copy, entrypoint. With a template, its fields come first and opts override."""
        body = {}
        if template:
            t = next((t for t in self.templates() if t["name"] == template), None)
            if not t:
                raise FcvmError(404, f"no template '{template}'")
            body = {k: v for k, v in t.items() if k not in ("name", "builtin", "description", "process", "command", "secrets")}
            if t["process"] == "idle":
                body["idle"] = True
            elif t["process"] == "command":
                body["command"] = " ".join(_shell_quote(a) for a in t["command"])
            secrets = {n: os.environ[n] for n in t.get("secrets", []) if n in os.environ}
            if secrets:
                body["secrets"] = secrets
        if image:
            body["image"] = image
        if "image" not in body:
            raise FcvmError(400, "give an image or a template")
        if "command" in opts and isinstance(opts["command"], (list, tuple)):
            opts["command"] = " ".join(_shell_quote(a) for a in opts["command"])
        body.update({k: v for k, v in opts.items() if v is not None})
        body.update({"name": name, "start": start})
        return Sandbox(self, name, self.post("vms", body))

    def claim(self, pool, ttl=None, idle_stop=None):
        """A ready VM out of a warm pool."""
        info = self.post(f"pools/{pool}/claim", {k: v for k, v in (("ttl", ttl), ("idle_stop", idle_stop)) if v})
        return Sandbox(self, info["name"], info)


def _shell_quote(a):
    import re
    return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'"


class Sandbox:
    """A VM. Methods map onto the API's /vms/{name}/... endpoints."""

    def __init__(self, client, name, info=None):
        self.client, self.name, self._info = client, name, info

    def __repr__(self):
        return f"Sandbox({self.name!r})"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.remove()

    def info(self):
        self._info = self.client.get(f"vms/{self.name}")
        return self._info

    @property
    def state(self):
        return self.info()["state"]

    @property
    def ip(self):
        return self.info().get("ip")

    # commands ----------------------------------------------------------------------------
    def exec(self, command=None, argv=None, workdir=None, env=None, user=None, stdin=None, timeout=300, background=False):
        """Run a shell command (or an exact argv). Returns an ExecResult, or a Job with background=True."""
        body = {"command": command, "argv": argv, "workdir": workdir, "env": env, "user": user,
                "stdin": stdin, "timeout": timeout, "background": background}
        r = self.client.post(f"vms/{self.name}/exec", {k: v for k, v in body.items() if v is not None and v is not False})
        return Job(self, r["job"]) if background else ExecResult(r)

    def run(self, command, **kw):
        """exec, raising when the command fails; returns its stdout."""
        return self.exec(command, **kw).check().stdout

    def jobs(self):
        return [Job(self, j["id"], j) for j in self.client.get(f"vms/{self.name}/jobs")]

    def job(self, job_id):
        return Job(self, job_id)

    # files -------------------------------------------------------------------------------
    def read_file(self, path):
        return self.client.request("GET", f"vms/{self.name}/file", params={"path": path}, raw=True)

    def read_text(self, path, encoding="utf-8"):
        return self.read_file(path).decode(encoding)

    def write_file(self, path, data, mode=None):
        if isinstance(data, str):
            data = data.encode()
        return self.client.request("PUT", f"vms/{self.name}/file", params={"path": path, "mode": mode}, data=data)

    def ls(self, path="/"):
        return self.client.get(f"vms/{self.name}/files", path=path)["entries"]

    def mkdir(self, path):
        return self.client.post(f"vms/{self.name}/files", {"op": "mkdir", "path": path})

    def remove_path(self, path):
        return self.client.post(f"vms/{self.name}/files", {"op": "remove", "path": path})

    def rename(self, path, to):
        return self.client.post(f"vms/{self.name}/files", {"op": "rename", "path": path, "to": to})

    def mount(self, host, path=None, ro=False):
        return self.client.post(f"vms/{self.name}/mounts", {"host": host, "path": path or "", "ro": ro})

    def umount(self, path):
        return self.client.delete(f"vms/{self.name}/mounts", path=path)

    # ports -------------------------------------------------------------------------------
    def publish(self, guest_port, host_port=None, bind=None):
        """Publish a port; returns the host port (a free one unless given)."""
        spec = f"{bind + ':' if bind else ''}{host_port or ''}:{int(guest_port)}"
        info = self.client.post(f"vms/{self.name}/ports", {"port": spec})
        return int(info["ports"][-1].split(":")[-2])

    def unpublish(self, host_port):
        return self.client.delete(f"vms/{self.name}/ports", port=str(host_port))

    # lifecycle ---------------------------------------------------------------------------
    def start(self):
        return self.client.post(f"vms/{self.name}/start")

    def stop(self):
        return self.client.post(f"vms/{self.name}/stop")

    def restart(self):
        return self.client.post(f"vms/{self.name}/restart")

    def remove(self):
        return self.client.delete(f"vms/{self.name}")

    def update(self, **fields):
        """restart, ttl, idle_stop (None clears), secrets ({NAME: value}), unset_secrets, forward."""
        return self.client.post(f"vms/{self.name}/update", fields)

    def snapshot(self, name):
        self.client.post(f"vms/{self.name}/snapshot", {"name": name})
        return Snapshot(self.client, name)

    def commit(self, image):
        self.client.post(f"vms/{self.name}/commit", {"image": image})
        return image

    def logs(self):
        return self.client.get(f"vms/{self.name}/logs")["log"]

    def egress(self):
        return self.client.get(f"vms/{self.name}/egress")["text"]

    def balloon(self, mib=None):
        if mib is None:
            return self.client.get(f"vms/{self.name}/balloon")
        return self.client.post(f"vms/{self.name}/balloon", {"mib": int(mib)})

    def processes(self):
        return self.client.get(f"vms/{self.name}/processes")

    def stats(self, range="10m"):
        return self.client.get(f"vms/{self.name}/stats", range=range)


class Job:
    """A background command (exec with background=True)."""

    def __init__(self, sandbox, job_id, info=None):
        self.sandbox, self.id, self._info = sandbox, job_id, info

    def __repr__(self):
        return f"Job({self.id!r} in {self.sandbox.name!r})"

    def status(self, tail=200):
        """{id, command, started, status: running|exited|lost, exit_code, stdout, stderr}."""
        self._info = self.sandbox.client.get(f"vms/{self.sandbox.name}/jobs/{self.id}", tail=tail)
        return self._info

    def kill(self):
        return self.sandbox.client.delete(f"vms/{self.sandbox.name}/jobs/{self.id}")

    def wait(self, timeout=None, poll=1.0, tail=200):
        """The job's final status (polling); TimeoutError after `timeout` seconds."""
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            s = self.status(tail)
            if s["status"] != "running":
                return s
            if deadline and time.monotonic() > deadline:
                raise TimeoutError(f"job {self.id} still running after {timeout} s")
            time.sleep(poll)


class ServerJob:
    """A server-side job (an image import or a build)."""

    def __init__(self, client, info):
        self.client, self.id, self._info = client, info["id"], info

    def status(self):
        self._info = self.client.get(f"jobs/{self.id}")
        return self._info

    def wait(self, timeout=None, poll=1.0):
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            s = self.status()
            if s["status"] != "running":
                if s["status"] == "failed":
                    raise FcvmError(0, f"job {s.get('title', self.id)} failed: " + "\n".join(s.get("lines", [])[-5:]))
                return s
            if deadline and time.monotonic() > deadline:
                raise TimeoutError(f"job {self.id} still running after {timeout} s")
            time.sleep(poll)


class Snapshot:
    def __init__(self, client, name, info=None):
        self.client, self.name, self._info = client, name, info

    def __repr__(self):
        return f"Snapshot({self.name!r})"

    def fork(self, name=None, count=1):
        """Running VMs from this snapshot; returns them (name, or name-1..name-N)."""
        before = {v["name"] for v in self.client.get("vms")}
        self.client.post(f"snapshots/{self.name}/fork", {"count": count, **({"name": name} if name else {})})
        return [Sandbox(self.client, v["name"], v) for v in self.client.get("vms") if v["name"] not in before]

    def remove(self):
        return self.client.delete(f"snapshots/{self.name}")
