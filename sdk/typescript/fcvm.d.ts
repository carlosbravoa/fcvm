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
  /** your tags on the VM; keys: letters, digits, . _ - /, not starting with fcvm. */
  labels?: Record<string, string>;
  /** the tenant label, set once (never changed by update); its quota applies when the VM starts */
  tenant?: string;
  /** until when the VM is wanted: a Unix time, an ISO 8601 time, or a duration such as "2h" */
  lease?: number | string;
  /** succeed if a VM of this name exists with the same spec (a safe retry) instead of failing */
  if_same_spec?: boolean;
}

export interface Tenant {
  name: string;
  quota: { vcpus: number | null; mem_mib: number | null; vms: number | null; disk_mib: number | null };
  /** vCPUs, memory and count of its running VMs; the disk of all its VMs, images, snapshots and volumes */
  allocated: { vcpus: number; mem_mib: number; vms: number; disk_mib: number };
  vms: string[];
  images: string[];
  snapshots: string[];
  volumes: string[];
}

export interface VmInfo {
  name: string;
  state: "running" | "stopped" | "exited";
  /** the state, why, and since when (Unix time) */
  status: { state: string; reason: string; since: number | null };
  labels: Record<string, string>;
  /** the create request as given (the fields of a template) */
  spec: Record<string, unknown> | null;
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
  /** every VM; `label: "KEY=VALUE"` or `tenant: "NAME"` keeps only those */
  vms(opts?: { label?: string; tenant?: string }): Promise<Sandbox[]>;
  tenants(): Promise<Tenant[]>;
  tenant(name: string): Promise<Tenant>;
  /** a tenant's quota: each a number, or null for no limit */
  setTenant(name: string, quota: Partial<Tenant["quota"]>): Promise<Tenant>;
  removeTenant(name: string): Promise<{ removed: string }>;
  sandbox(name: string): Promise<Sandbox>;
  /** images, snapshots and volumes carry a `tenant` (null when public); `tenant` keeps only that tenant's */
  images(opts?: { tenant?: string }): Promise<Record<string, unknown>[]>;
  templates(): Promise<Record<string, unknown>[]>;
  snapshots(opts?: { tenant?: string }): Promise<Snapshot[]>;
  volumes(opts?: { tenant?: string }): Promise<Record<string, unknown>[]>;
  pools(): Promise<Record<string, unknown>[]>;
  /** `fcvm import`; with a tenant, the image is that tenant's own */
  importImage(ref: string, name?: string, opts?: { tenant?: string }): Promise<ServerJob>;
  /** `fcvm volume create`; size such as "10G" (the server's default when omitted) */
  createVolume(name: string, opts?: { size?: string; tenant?: string }): Promise<Record<string, unknown>>;
  create(name: string, opts?: CreateOptions): Promise<Sandbox>;
  claim(pool: string, opts?: { ttl?: string; idle_stop?: string; labels?: Record<string, string>; lease?: number | string; tenant?: string }): Promise<Sandbox>;
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
  update(fields: Partial<Pick<CreateOptions, "restart" | "ttl" | "idle_stop" | "secrets" | "forward" | "labels">> & { unset_secrets?: string[]; unset_labels?: string[] }): Promise<VmInfo>;
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
  fork(opts?: { name?: string; count?: number; labels?: Record<string, string>; lease?: number | string; tenant?: string }): Promise<Sandbox[]>;
  remove(): Promise<unknown>;
}
