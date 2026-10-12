# fcvm for Node and TypeScript

A client for the HTTP API of [fcvm](https://github.com/carlosbravoa/fcvm),
which runs disposable, isolated Firecracker microVMs with container
ergonomics. Use it to give an agent framework, a test harness or a build
pipeline real VM sandboxes: create one from any OCI image, run commands,
move files, snapshot it and fork running copies in about 150 ms.

The client has no dependencies, needs Node 18 or newer, and ships its own
types. The VMs run on a Linux host with KVM where fcvm is installed; the
client itself runs anywhere that can reach that host's API.

```sh
npm install fcvm
```

```js
import { Client } from "fcvm";

const c = new Client();
const sb = await c.create("box", { template: "node-sandbox", ttl: "1h" });
const r = await sb.exec("node -v");                       // { exit_code, stdout, stderr }
await sb.writeFile("/work/app.js", "console.log('hi')");
const job = await sb.execBackground("node /work/app.js");
const port = await sb.publish(8000);                      // http://127.0.0.1:PORT on the host
await job.wait({ timeout: 60000 });
await sb.remove();
```

`new Client()` finds the local fcvm service by itself. For another host, set
`FCVM_URL` and `FCVM_TOKEN`. The token gives full control of fcvm, so treat
it like an SSH key.

- [All methods](https://github.com/carlosbravoa/fcvm/blob/main/sdk/README.md)
- [The HTTP API](https://github.com/carlosbravoa/fcvm/blob/main/docs/api.md)
- [Installing fcvm](https://github.com/carlosbravoa/fcvm/blob/main/docs/getting-started.md)

Apache License 2.0.
