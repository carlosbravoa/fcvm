# fcvm-mcp

<!-- mcp-name: io.github.carlosbravoa/fcvm -->

Starts the MCP server of [fcvm](https://github.com/carlosbravoa/fcvm), which
gives an AI agent disposable Firecracker microVMs as sandboxes. Each sandbox
boots its own kernel in about a second from any OCI image. The agent gets
tools to run commands, read and write files, build images, commit prepared
states, and snapshot a VM to fork running copies in about 150 ms. Network
access is off, full, or an allowlist of hosts enforced by a logging proxy.

This package is only a launcher: it finds the installed `fcvm` and runs
`fcvm mcp`. fcvm itself needs a Linux host with KVM:

```sh
curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh
fcvm setup
```

Then register the server with your MCP client. With Claude Code:

```sh
claude mcp add fcvm -- uvx fcvm-mcp
claude mcp add fcvm -e FCVM_MCP_NETWORK=@pypi,@github -- uvx fcvm-mcp   # agents can only reach PyPI and GitHub
```

In a client's JSON configuration:

```json
{"mcpServers": {"fcvm": {"command": "uvx", "args": ["fcvm-mcp"]}}}
```

The launcher looks for `fcvm` on `PATH`, then in `~/.local/bin` and
`/usr/local/bin`. Set `FCVM_BIN` to use another one. Running `fcvm mcp`
directly works the same way and needs no Python package.

Settings are environment variables: `FCVM_MCP_NETWORK` pins the network
policy, `FCVM_MCP_TEMPLATE` picks the default template, `FCVM_MCP_TTL` and
`FCVM_MCP_IDLE_STOP` stop forgotten sandboxes, `FCVM_MCP_SECRETS` and
`FCVM_MCP_FORWARD` hand credentials to sandboxes without the agent seeing
them. [The agents guide](https://github.com/carlosbravoa/fcvm/blob/main/docs/agents.md)
covers the tools and every setting.

Apache License 2.0.
