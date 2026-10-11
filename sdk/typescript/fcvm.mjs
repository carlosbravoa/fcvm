// fcvm: a client for the fcvm HTTP API (fcvm serve, or the fcvm service).
//
//   import { Client } from "./fcvm.mjs";
//   const c = new Client();                         // vms/.serve.json, or FCVM_URL + FCVM_TOKEN
//   const sb = await c.create("box", { template: "node-sandbox", ttl: "1h" });
//   const r = await sb.exec("node -v");             // { exit_code, stdout, stderr }
//   await sb.writeFile("/work/app.js", "console.log('hi')");
//   const job = await sb.execBackground("node /work/app.js");
//   const port = await sb.publish(8000);            // http://127.0.0.1:PORT on the host
//   await sb.remove();
//
// Node 18+ (fetch), no dependencies. Types in fcvm.d.ts.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export class FcvmError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "FcvmError";
    this.status = status;
  }
}

export function discover() {
  if (process.env.FCVM_URL && process.env.FCVM_TOKEN) {
    return { url: process.env.FCVM_URL.replace(/\/$/, ""), token: process.env.FCVM_TOKEN };
  }
  const homes = [process.env.FCVM_HOME, join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "fcvm")];
  try {   // an fcvm run from a checkout keeps its state in the checkout
    homes.push(execFileSync("fcvm", ["_home"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 10000 }).trim());
  } catch {}
  for (const home of homes.filter(Boolean)) {
    try {
      const s = JSON.parse(readFileSync(join(home, "vms", ".serve.json"), "utf8"));
      return { url: s.url.split("/?")[0], token: s.token };
    } catch {}
  }
  throw new FcvmError(0, "no fcvm server found: run `fcvm serve` (or `fcvm service install`), or set FCVM_URL and FCVM_TOKEN");
}

const quote = (a) => (/^[\w@%+=:,./-]+$/.test(a) ? a : `'${a.replace(/'/g, "'\\''")}'`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class Client {
  constructor({ url, token } = {}) {
    if (!(url && token)) ({ url, token } = discover());
    this.url = url.replace(/\/$/, "");
    this.token = token;
  }

  async request(method, path, { body, params, data, raw } = {}) {
    let url = `${this.url}/api/${path.replace(/^\//, "")}`;
    if (params) {
      const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null));
      if ([...q].length) url += "?" + q;
    }
    const headers = { Authorization: `Bearer ${this.token}` };
    let payload = data;
    if (body !== undefined) {
      payload = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
    }
    let r;
    try {
      r = await fetch(url, { method, headers, body: payload });
    } catch (e) {
      throw new FcvmError(0, `cannot reach ${this.url}: ${e.message}`);
    }
    if (!r.ok) {
      let message = `HTTP ${r.status}`;
      try { message = (await r.json()).error || message; } catch {}
      throw new FcvmError(r.status, message);
    }
    if (raw) return Buffer.from(await r.arrayBuffer());
    const text = await r.text();
    return text ? JSON.parse(text) : null;
  }

  get(path, params) { return this.request("GET", path, { params }); }
  post(path, body = {}) { return this.request("POST", path, { body }); }
  delete(path, params) { return this.request("DELETE", path, { params }); }

  host() { return this.get("host"); }
  async vms() { return (await this.get("vms")).map((v) => new Sandbox(this, v.name, v)); }
  async sandbox(name) { return new Sandbox(this, name, await this.get(`vms/${name}`)); }
  images() { return this.get("images"); }
  templates() { return this.get("templates"); }
  async snapshots() { return (await this.get("snapshots")).map((s) => new Snapshot(this, s.name, s)); }
  volumes() { return this.get("volumes"); }
  pools() { return this.get("pools"); }

  async importImage(ref, name) {
    return new ServerJob(this, await this.post("images", name ? { ref, name } : { ref }));
  }

  // opts: the fields of POST /vms (vcpus, mem_mib, network, allow, ports, volumes, idle,
  // command, jail, restart, ttl, idle_stop, secrets, forward, cache, ...), plus
  // image / template / start. A template's fields come first; opts override them.
  async create(name, { image, template, start = true, ...opts } = {}) {
    let body = {};
    if (template) {
      const t = (await this.templates()).find((x) => x.name === template);
      if (!t) throw new FcvmError(404, `no template '${template}'`);
      for (const [k, v] of Object.entries(t)) {
        if (!["name", "builtin", "description", "process", "command", "secrets"].includes(k)) body[k] = v;
      }
      if (t.process === "idle") body.idle = true;
      else if (t.process === "command") body.command = t.command.map(quote).join(" ");
      const secrets = Object.fromEntries((t.secrets || []).filter((n) => n in process.env).map((n) => [n, process.env[n]]));
      if (Object.keys(secrets).length) body.secrets = secrets;
    }
    if (image) body.image = image;
    if (!body.image) throw new FcvmError(400, "give an image or a template");
    if (Array.isArray(opts.command)) opts.command = opts.command.map(quote).join(" ");
    for (const [k, v] of Object.entries(opts)) if (v != null) body[k] = v;
    body = { ...body, name, start };
    return new Sandbox(this, name, await this.post("vms", body));
  }

  async claim(pool, { ttl, idle_stop } = {}) {
    const info = await this.post(`pools/${pool}/claim`, { ...(ttl && { ttl }), ...(idle_stop && { idle_stop }) });
    return new Sandbox(this, info.name, info);
  }
}

export class Sandbox {
  constructor(client, name, info = null) {
    this.client = client;
    this.name = name;
    this._info = info;
  }

  async info() { return (this._info = await this.client.get(`vms/${this.name}`)); }
  async state() { return (await this.info()).state; }

  // exec(command | { argv }, { workdir, env, user, stdin, timeout })
  async exec(command, { argv, workdir, env, user, stdin, timeout = 300, background = false } = {}) {
    const body = { command, argv, workdir, env, user, stdin, timeout, background };
    for (const k of Object.keys(body)) if (body[k] == null || body[k] === false) delete body[k];
    const r = await this.client.post(`vms/${this.name}/exec`, body);
    return background ? new Job(this, r.job) : r;
  }
  execBackground(command, opts = {}) { return this.exec(command, { ...opts, background: true }); }
  async run(command, opts) {
    const r = await this.exec(command, opts);
    if (r.exit_code !== 0) throw new FcvmError(0, `command exited ${r.exit_code}: ${(r.stderr || r.stdout).trim()}`);
    return r.stdout;
  }
  async jobs() { return (await this.client.get(`vms/${this.name}/jobs`)).map((j) => new Job(this, j.id, j)); }
  job(id) { return new Job(this, id); }

  readFile(path) { return this.client.request("GET", `vms/${this.name}/file`, { params: { path }, raw: true }); }
  async readText(path) { return (await this.readFile(path)).toString("utf8"); }
  writeFile(path, data, mode) {
    return this.client.request("PUT", `vms/${this.name}/file`, { params: { path, mode }, data: typeof data === "string" ? Buffer.from(data) : data });
  }
  async ls(path = "/") { return (await this.client.get(`vms/${this.name}/files`, { path })).entries; }
  mkdir(path) { return this.client.post(`vms/${this.name}/files`, { op: "mkdir", path }); }
  removePath(path) { return this.client.post(`vms/${this.name}/files`, { op: "remove", path }); }
  rename(path, to) { return this.client.post(`vms/${this.name}/files`, { op: "rename", path, to }); }
  mount(host, path = "", ro = false) { return this.client.post(`vms/${this.name}/mounts`, { host, path, ro }); }
  umount(path) { return this.client.delete(`vms/${this.name}/mounts`, { path }); }

  async publish(guestPort, { hostPort, bind } = {}) {
    const spec = `${bind ? bind + ":" : ""}${hostPort || ""}:${guestPort}`;
    const info = await this.client.post(`vms/${this.name}/ports`, { port: spec });
    const parts = info.ports[info.ports.length - 1].split(":");
    return +parts[parts.length - 2];
  }
  unpublish(hostPort) { return this.client.delete(`vms/${this.name}/ports`, { port: String(hostPort) }); }

  start() { return this.client.post(`vms/${this.name}/start`); }
  stop() { return this.client.post(`vms/${this.name}/stop`); }
  restart() { return this.client.post(`vms/${this.name}/restart`); }
  remove() { return this.client.delete(`vms/${this.name}`); }
  update(fields) { return this.client.post(`vms/${this.name}/update`, fields); }
  async snapshot(name) { await this.client.post(`vms/${this.name}/snapshot`, { name }); return new Snapshot(this.client, name); }
  async commit(image) { await this.client.post(`vms/${this.name}/commit`, { image }); return image; }
  async logs() { return (await this.client.get(`vms/${this.name}/logs`)).log; }
  async egress() { return (await this.client.get(`vms/${this.name}/egress`)).text; }
  balloon(mib) { return mib == null ? this.client.get(`vms/${this.name}/balloon`) : this.client.post(`vms/${this.name}/balloon`, { mib }); }
  processes() { return this.client.get(`vms/${this.name}/processes`); }
  stats(range = "10m") { return this.client.get(`vms/${this.name}/stats`, { range }); }
}

export class Job {
  constructor(sandbox, id, info = null) {
    this.sandbox = sandbox;
    this.id = id;
    this._info = info;
  }
  async status(tail = 200) { return (this._info = await this.sandbox.client.get(`vms/${this.sandbox.name}/jobs/${this.id}`, { tail })); }
  kill() { return this.sandbox.client.delete(`vms/${this.sandbox.name}/jobs/${this.id}`); }
  async wait({ timeout, poll = 1000, tail = 200 } = {}) {
    const deadline = timeout ? Date.now() + timeout : null;
    for (;;) {
      const s = await this.status(tail);
      if (s.status !== "running") return s;
      if (deadline && Date.now() > deadline) throw new Error(`job ${this.id} still running after ${timeout} ms`);
      await sleep(poll);
    }
  }
}

export class ServerJob {
  constructor(client, info) {
    this.client = client;
    this.id = info.id;
    this._info = info;
  }
  async status() { return (this._info = await this.client.get(`jobs/${this.id}`)); }
  async wait({ timeout, poll = 1000 } = {}) {
    const deadline = timeout ? Date.now() + timeout : null;
    for (;;) {
      const s = await this.status();
      if (s.status !== "running") {
        if (s.status === "failed") throw new FcvmError(0, `job ${s.title || this.id} failed: ${(s.lines || []).slice(-5).join("\n")}`);
        return s;
      }
      if (deadline && Date.now() > deadline) throw new Error(`job ${this.id} still running after ${timeout} ms`);
      await sleep(poll);
    }
  }
}

export class Snapshot {
  constructor(client, name, info = null) {
    this.client = client;
    this.name = name;
    this._info = info;
  }
  async fork({ name, count = 1 } = {}) {
    const before = new Set((await this.client.get("vms")).map((v) => v.name));
    await this.client.post(`snapshots/${this.name}/fork`, { count, ...(name && { name }) });
    return (await this.client.get("vms")).filter((v) => !before.has(v.name)).map((v) => new Sandbox(this.client, v.name, v));
  }
  remove() { return this.client.delete(`snapshots/${this.name}`); }
}
