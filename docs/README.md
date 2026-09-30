# fcvm documentation

Read from the top down. Each level assumes the one before it, and you can
stop wherever you have what you need.

## 1. Start here

- **[Getting started](getting-started.md).** Requirements and installation
  step by step, with what each step does. Also: the optional jailer and boot
  service, where files live, updating and uninstalling.
- **[Use cases](use-cases.md).** Walkthroughs:
  - a sandbox for your coding agent;
  - running code you don't trust;
  - prepare once, fork many;
  - a development machine;
  - a small service that stays up;
  - building an image from a Dockerfile.

## 2. Using fcvm

- **[Images](images.md).** Where images come from, app vs system images,
  registries (private too) and local archives, the Ubuntu base, Dockerfile
  builds, commit, layers and squash.
- **[Running VMs](vms.md).** create, start and run; commands, `--idle` and
  exit codes; resources; exec and shell; copying files; consoles and logs;
  ports, volumes and host directories.
- **[Networking and egress](networking.md).** The three network modes,
  egress allowlists and presets, isolation defaults, addresses.
- **[Snapshots and fork](snapshots.md).** Capturing running VMs and forking
  them; when to snapshot and when to commit.
- **[The fcvm service](service.md).** Running at boot, restart policies,
  the supervisor, shutdown and recovery.
- **[Web console](web-console.md).** The browser UI: what's in it, and how
  it's secured.
- **[Agents (MCP)](agents.md).** The MCP server's tools, network pinning,
  jailing, patterns.

## 3. Reference

- **[Commands](cli.md).** Every command and option.
- **[Configuration](configuration.md).** Every setting, `fcvm.conf`,
  customizing the kernel, egress presets, the base image.
- **[HTTP API](api.md).** Endpoints, authentication, objects.

## 4. In depth

- **[How it works](internals.md).** Architecture, the design decisions
  behind it, and how each part is implemented: the kernel, boot, layers,
  the exec agent, host directories, networking, snapshots, builds, the
  supervisor.
- **[Security and isolation](security.md).** The threat model; rootless vs
  jailed VMMs and the jailer helper in detail; network isolation,
  anti-spoofing and egress. Also verification steps, troubleshooting and
  known limitations.
- **[Roadmap](roadmap.md).** What's planned, and what has been done from
  earlier plans.
