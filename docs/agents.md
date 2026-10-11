# Agents (MCP)

`fcvm mcp` is an MCP server on stdio. It gives an AI agent tools to create
and use sandboxes, each one an fcvm VM. It wraps the CLI, so everything
behaves exactly as on the command line.

- [Setup](#setup)
- [Tools](#tools)
- [Network policy](#network-policy)
- [Jailing](#jailing)
- [Secrets](#secrets)
- [Lifetimes](#lifetimes)
- [Behaviour and limits](#behaviour-and-limits)
- [Patterns](#patterns)

## Setup

With Claude Code:

```sh
claude mcp add fcvm -- "$(command -v fcvm)" mcp
claude mcp add fcvm -e FCVM_MCP_NETWORK=@pypi,@github -- "$(command -v fcvm)" mcp   # with a pinned network policy
```

Any MCP client that launches stdio servers works the same way: the command
is the full path of `fcvm` (`command -v fcvm`) with the argument `mcp`, and
the settings below are environment variables. For an installed fcvm, that
path goes through the `current` link, so upgrades carry the registration
along.

The agent needs the images it will use. It can import them itself
(`pull_image`), or you can import them beforehand.

## Tools

| tool | does |
|---|---|
| `images`, `pull_image` | list images; import from Docker Hub or any registry, or a local archive |
| `create_sandbox` | create and boot a VM from an `image` or a `template`. App images stay idle for `exec` unless given a command (or the template says otherwise). `ttl` and `idle_stop` (`"2h"`, `"30m"`) make it stop on its own |
| `templates` | list [templates](templates.md): saved recipes with an image, resources, network and process |
| `exec` | run a shell command (`command`) or exact `argv`, with `workdir`, `env`, `user`, `stdin` and `timeout` (default 300 s). Returns `exit_code`, `stdout`, `stderr`. With `background`, starts it as a job and returns its id at once |
| `jobs`, `job`, `kill_job` | background commands: list them, get one's status and output tail, end one. The way to run a dev server and then `publish_port` it |
| `write_file`, `read_file` | text files in the VM |
| `copy_to_vm`, `copy_from_vm` | host files and directories in or out (`fcvm cp`) |
| `commit_vm` | save a VM's state as an image; new sandboxes start from it |
| `build_image` | build an image from a Dockerfile in a host directory (cached per step) |
| `snapshot_vm`, `fork`, `snapshots`, `remove_snapshot` | snapshot a prepared sandbox and fork running copies in ~150 ms, for parallel attempts or rollback |
| `egress_log` | a sandbox's network policy and its allowed/denied requests |
| `publish_port`, `unpublish_port` | publish a TCP port of a running sandbox on the host (a free host port unless given): returns `host_port` and a `url`, so the agent can reach a server it started |
| `logs`, `list_vms`, `start_vm`, `stop_vm`, `remove_vm`, `volumes` | lifecycle and state |

## Network policy

`create_sandbox` and `build_image` take `network`: `"full"`, `"none"`, or an
allowlist such as `["@pypi", "github.com"]` (see
[egress allowlists](networking.md#restricted-network-egress-allowlists)).

**Pinning.** To decide the policy yourself, set `FCVM_MCP_NETWORK` when
registering the server, for example `@pypi,@github`. Every sandbox then
gets that allowlist. The agent may only narrow it to `"none"`, and no tool
widens it.

## Templates

`create_sandbox` takes `template` (see the `templates` tool), and the
agent's other arguments override the template's. Set `FCVM_MCP_TEMPLATE`
to the template sandboxes get when the agent names neither an image nor a
template, for example:

```sh
claude mcp add fcvm -e FCVM_MCP_TEMPLATE=python-sandbox -- "$(command -v fcvm)" mcp
```

A pinned network (`FCVM_MCP_NETWORK`) still wins over a template's network,
except that a template with no network stays without one. A template that
asks for the jailer gets it.

## Jailing

Once `fcvm-jaild` is installed (`fcvm jail-setup`), every sandbox the
server creates runs under the Firecracker jailer. Set `FCVM_MCP_JAIL=0` to
turn that off.

## Secrets

An agent often needs a token (GitHub, a package registry, an API) inside
its sandbox, and it shouldn't be the one holding it. Name the secrets when
registering the server, with their values in its environment:

```sh
GITHUB_TOKEN=ghp_... claude mcp add fcvm -e FCVM_MCP_SECRETS=GITHUB_TOKEN -- "$(command -v fcvm)" mcp
```

Every sandbox then gets them as files in `/run/secrets` and as environment
variables of `exec`, as described in [Secrets](vms.md#secrets): never on
the sandbox's disk, so a `commit_vm` can't leak them. There is no tool to
read or set secrets; the agent only learns their names from the server's
instructions.

## Lifetimes

Agents forget sandboxes. `create_sandbox` takes `ttl` (stop that long after
boot) and `idle_stop` (stop after that long without `exec` or file
operations), and you can set defaults for sandboxes the agent gives none:

```sh
claude mcp add fcvm -e FCVM_MCP_TTL=4h -e FCVM_MCP_IDLE_STOP=30m -- "$(command -v fcvm)" mcp
```

A template's `ttl` and `idle_stop` apply too. The stops are made by the
supervisor in `fcvm serve` (the fcvm service), see
[lifetimes](vms.md#lifetimes---ttl-and---idle-stop).

## Behaviour and limits

- **Output cap.** Output is capped at 20,000 characters per stream, keeping
  the head and the tail, so a noisy command can't flood the agent's
  context.
- **Errors.** Errors from fcvm itself (no such VM, VM not running) come
  back as tool errors, not as a command's exit code.
- **Timing.** A typical loop takes about 1.5 s to create a sandbox, and
  under 0.5 s per `exec`.
- **Names.** Sandboxes are ordinary VMs, named after the image
  (`python-a1b2c3`) unless the agent names them. `fcvm ls` and the web
  console show them, and you can step in with `fcvm shell`.

## Patterns

- **Prepare, then commit.** Install dependencies once, `commit_vm`, and
  create later sandboxes from that image, so they start ready.
- **Parallel attempts.** `snapshot_vm` a prepared sandbox, then `fork` it
  once per attempt.
- **Rollback.** Remove a fork that went wrong and fork again from the
  snapshot.
- **Reviewing what happened.** `egress_log` shows every host a sandbox
  tried to reach, and whether it was allowed. `fcvm egress VM` shows the
  same on the command line.
