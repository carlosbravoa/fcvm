# fcvm client libraries

Thin clients for the [fcvm HTTP API](../docs/api.md), so a program (an
agent framework, a test harness, a build pipeline) uses sandboxes the way
the CLI does. Both find the local server by themselves (`vms/.serve.json`,
written by `fcvm serve` or the fcvm service), or take `FCVM_URL` and
`FCVM_TOKEN`. No dependencies.

## Python

```sh
pip install fcvm
# or from the repository: pip install "git+https://github.com/carlosbravoa/fcvm#subdirectory=sdk/python"
# or just copy sdk/python/fcvm.py next to your code: it's one file, standard library only
```

```python
from fcvm import Client

c = Client()
with c.create("box", template="python-sandbox", ttl="1h") as sb:      # removed when the block ends
    out = sb.run("pip install -q requests && python -c 'import requests; print(requests.__version__)'")
    sb.write_file("/work/app.py", "print('hi')\n")
    print(sb.exec("python /work/app.py").stdout)                      # ExecResult: exit_code, stdout, stderr
    job = sb.exec("python -m http.server 8000", background=True)      # a Job
    port = sb.publish(8000)                                           # http://127.0.0.1:{port} on the host
    job.kill()

snap = c.sandbox("prepared").snapshot("ready")
forks = snap.fork("try", count=3)                                     # three running Sandbox objects
quick = c.claim("py")                                                 # from a warm pool
```

`Client`: `vms(label=, tenant=)`, `sandbox(name)`, `images(tenant=)`,
`templates()`, `snapshots(tenant=)`, `volumes(tenant=)`, `pools()`,
`import_image(ref, name=, tenant=)` (a job to `wait()` on),
`create_volume(name, size=, tenant=)`, `create(name, image=...,
template=..., **fields)` with the fields of `POST /vms` (`vcpus`,
`mem_mib`, `network`, `allow`, `cache`, `ports`, `volumes`, `idle`,
`command`, `jail`, `restart`, `ttl`, `idle_stop`, `secrets`, `forward`,
`labels`, `tenant`, `lease`), `claim(pool)`, `tenants()`, `tenant(name)`,
`set_tenant(name, vcpus=, mem_mib=, vms=, disk_mib=)`, `remove_tenant(name)`.

`Sandbox`: `exec(command | argv=[...], workdir, env, user, stdin, timeout,
background)`, `run(command)` (raises on failure, returns stdout),
`jobs()`, `read_file`/`read_text`/`write_file`/`ls`/`mkdir`/`remove_path`/`rename`,
`mount`/`umount`, `publish(guest_port)`/`unpublish`, `start`/`stop`/`restart`/`remove`,
`update(ttl=..., ...)`, `snapshot(name)`, `commit(image)`, `logs()`,
`egress()`, `balloon(mib)`, `processes()`, `stats()`.

Errors are `FcvmError` with `.status` (the HTTP status), `.code` (fcvm's
[error code](../docs/errors.md), e.g. `vm_not_found`) and the server's
message.

## Node / TypeScript

```sh
npm install fcvm   # or copy fcvm.mjs (+ fcvm.d.ts)
```

```js
import { Client } from "fcvm";

const c = new Client();
const sb = await c.create("box", { template: "node-sandbox", ttl: "1h" });
const r = await sb.exec("node -v");                       // { exit_code, stdout, stderr }
await sb.writeFile("/work/app.js", "console.log('hi')");
const job = await sb.execBackground("node /work/app.js");
const port = await sb.publish(8000);
await job.wait({ timeout: 60000 });
await sb.remove();
```

The same methods, camel-cased (`readFile`, `writeFile`, `execBackground`,
`removePath`). Node 18 or newer (global `fetch`); the types are in
`fcvm.d.ts`.

## Notes

- The token gives full control of fcvm (see [the API](../docs/api.md#connecting)):
  treat it like an SSH key.
- `exec` is synchronous up to `timeout` seconds (default 300). For servers
  and long builds use `background` and poll the `Job`.
- `publish` without a host port picks a free one and returns it.
- Everything the clients do, `curl` can do too; the clients just save the
  plumbing.
