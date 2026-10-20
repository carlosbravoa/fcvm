// Types for fcvm.mjs, the fcvm HTTP API client.

export class FcvmError extends Error {
  status: number;
  /** fcvm's error code (`vm_not_found`, `vm_running`, ...; docs/errors.md), "error" when it has none */
  code: string;
  constructor(status: number, message: string, code?: string);
}

export function discover(): { url: string; token: string };

export interface ExecResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  timed_out?: boolean;
}

export interface ExecOptions {
  argv?: string[];
  workdir?: string;
  env?: Record<string, string>;
  user?: string;
  stdin?: string;
  timeout?: number;
  background?: boolean;
}

export interface CreateOptions {
  image?: string;
  template?: string;
  start?: boolean;
  vcpus?: number;
  mem_mib?: number;
  disk?: string;
  copy?: boolean;
  network?: "full" | "none" | "restricted";
  allow?: string[];
  cache?: boolean;
  ports?: string[];
  volumes?: string[];
  idle?: boolean;
  command?: string | string[];
  entrypoint?: string | null;
  jail?: boolean;
  restart?: "no" | "on-failure" | "unless-stopped" | "always";
  ttl?: string;
  idle_stop?: string;
  secrets?: Record<string, string>;
  forward?: ("ssh-agent" | "git")[];
  ref?: string;
}

export interface VmInfo {
  name: string;
  state: "running" | "stopped" | "exited";
  ip: string | null;
  image: string;
  type: "app" | "system";
  ports: string[];
  [key: string]: unknown;
}

export interface JobStatus {
  id: string;
  command: string[];
  started: number;
  status: "running" | "exited" | "lost";
  exit_code: number | null;
  ended: number | null;
  stdout?: string;
  stderr?: string;
}

export class Client {
  url: string;
  token: string;
  constructor(opts?: { url?: string; token?: string });
  request(method: string, path: string, opts?: { body?: unknown; params?: Record<string, unknown>; data?: Buffer; raw?: boolean }): Promise<any>;
  get(path: string, params?: Record<string, unknown>): Promise<any>;
  post(path: string, body?: unknown): Promise<any>;
  delete(path: string, params?: Record<string, unknown>): Promise<any>;
  host(): Promise<Record<string, unknown>>;
  vms(): Promise<Sandbox[]>;
  sandbox(name: string): Promise<Sandbox>;
  images(): Promise<Record<string, unknown>[]>;
  templates(): Promise<Record<string, unknown>[]>;
  snapshots(): Promise<Snapshot[]>;
  volumes(): Promise<Record<string, unknown>[]>;
  pools(): Promise<Record<string, unknown>[]>;
  importImage(ref: string, name?: string): Promise<ServerJob>;
  create(name: string, opts?: CreateOptions): Promise<Sandbox>;
  claim(pool: string, opts?: { ttl?: string; idle_stop?: string }): Promise<Sandbox>;
}

export class Sandbox {
  client: Client;
  name: string;
  constructor(client: Client, name: string, info?: VmInfo | null);
  info(): Promise<VmInfo>;
  state(): Promise<string>;
  exec(command: string | undefined, opts?: ExecOptions & { background?: false }): Promise<ExecResult>;
  exec(command: string | undefined, opts: ExecOptions & { background: true }): Promise<Job>;
  execBackground(command: string, opts?: ExecOptions): Promise<Job>;
  run(command: string, opts?: ExecOptions): Promise<string>;
  jobs(): Promise<Job[]>;
  job(id: string): Job;
  readFile(path: string): Promise<Buffer>;
  readText(path: string): Promise<string>;
  writeFile(path: string, data: string | Buffer, mode?: string): Promise<{ written: string; bytes: number }>;
  ls(path?: string): Promise<Record<string, unknown>[]>;
  mkdir(path: string): Promise<unknown>;
  removePath(path: string): Promise<unknown>;
  rename(path: string, to: string): Promise<unknown>;
  mount(host: string, path?: string, ro?: boolean): Promise<VmInfo>;
  umount(path: string): Promise<VmInfo>;
  publish(guestPort: number, opts?: { hostPort?: number; bind?: string }): Promise<number>;
  unpublish(hostPort: number): Promise<VmInfo>;
  start(): Promise<VmInfo>;
  stop(): Promise<VmInfo>;
  restart(): Promise<VmInfo>;
  remove(): Promise<{ removed: string }>;
  update(fields: Partial<Pick<CreateOptions, "restart" | "ttl" | "idle_stop" | "secrets" | "forward">> & { unset_secrets?: string[] }): Promise<VmInfo>;
  snapshot(name: string): Promise<Snapshot>;
  commit(image: string): Promise<string>;
  logs(): Promise<string>;
  egress(): Promise<string>;
  balloon(mib?: number): Promise<Record<string, unknown>>;
  processes(): Promise<Record<string, unknown>>;
  stats(range?: "10m" | "1h" | "6h" | "24h"): Promise<Record<string, unknown>>;
}

export class Job {
  sandbox: Sandbox;
  id: string;
  status(tail?: number): Promise<JobStatus>;
  kill(): Promise<unknown>;
  wait(opts?: { timeout?: number; poll?: number; tail?: number }): Promise<JobStatus>;
}

export class ServerJob {
  client: Client;
  id: number;
  status(): Promise<Record<string, unknown>>;
  wait(opts?: { timeout?: number; poll?: number }): Promise<Record<string, unknown>>;
}

export class Snapshot {
  client: Client;
  name: string;
  fork(opts?: { name?: string; count?: number }): Promise<Sandbox[]>;
  remove(): Promise<unknown>;
}
