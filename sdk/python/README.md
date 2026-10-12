# fcvm for Python

A client for the HTTP API of [fcvm](https://github.com/carlosbravoa/fcvm),
which runs disposable, isolated Firecracker microVMs with container
ergonomics. Use it to give an agent framework, a test harness or a build
pipeline real VM sandboxes: create one from any OCI image, run commands,
move files, snapshot it and fork running copies in about 150 ms.

The client has no dependencies and needs Python 3.9 or newer. The VMs run
on a Linux host with KVM where fcvm is installed; the client itself runs
anywhere that can reach that host's API.

```sh
pip install fcvm
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
```

`Client()` finds the local fcvm service by itself. For another host, set
`FCVM_URL` and `FCVM_TOKEN`. The token gives full control of fcvm, so treat
it like an SSH key.

- [All methods](https://github.com/carlosbravoa/fcvm/blob/main/sdk/README.md)
- [The HTTP API](https://github.com/carlosbravoa/fcvm/blob/main/docs/api.md)
- [Installing fcvm](https://github.com/carlosbravoa/fcvm/blob/main/docs/getting-started.md)

Apache License 2.0.
