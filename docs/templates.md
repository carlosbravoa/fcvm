# Templates

A template is a saved launch recipe: an image, with the resources, network,
ports, volumes, process and restart policy to run it with. It gives you "a
Python sandbox that reaches only PyPI" by name, everywhere:

```sh
fcvm create box --template python-sandbox          # the CLI
fcvm run --template offline-shell -- ./untrusted   # a throwaway VM
```

The web console's launch dialog and the MCP server's `create_sandbox` take
the same templates.

## Built-in templates

| template | image | resources | network | process |
|---|---|---|---|---|
| `python-sandbox` | `python:3.13-slim` | 2 vCPU, 2 GiB | `@pypi`, `@github` only | idle, for `exec` |
| `node-sandbox` | `node:22-slim` | 2 vCPU, 2 GiB | `@npm`, `@github` only | idle, for `exec` |
| `offline-shell` | `alpine:latest` | 512 MiB | none | idle, for `exec` |

A template names the registry reference its image came from, so the image
is imported on first use if it isn't here yet. A template you save under a
built-in name replaces the built-in one, and deleting yours brings it back.

## Using a template

`--template NAME` goes right after the VM name in `create`, or first in
`run`. Your own options come after it and win over the template's:

```sh
fcvm create big --template python-sandbox --mem 8192 -v ~/src
fcvm create job --template python-sandbox -- python /work/job.py
```

- **Options** such as `--mem`, `--vcpus` and `--restart` replace the
  template's. Ports, volumes and allowlist hosts add to its own.
- **The process** is the template's, unless you give one: `-- CMD`,
  `--idle` or `--entrypoint`.

## Saving templates

From an image and `create` options: the options are checked exactly as
`create` would check them, but no VM or volume is created.

```sh
fcvm template save pydev python-3.13-slim --mem 4096 --allow @pypi,@github \
    -v pip-cache:/root/.cache/pip --idle -d "Python with a pip cache"
```

From an existing VM, to repeat it: its image, resources, network, ports,
volumes, host directories, jail, restart policy and command.

```sh
fcvm template save web --from web-1 -d "the web app, as web-1 runs"
```

In the web console, fill in the launch dialog and choose **Save as
template**. The **Templates** page lists them all, and launches or deletes
them.

## Managing templates

```sh
fcvm template ls                # * marks built-in templates
fcvm template show NAME         # as JSON
fcvm template rm NAME
```

User templates are JSON files in `~/.local/share/fcvm/templates/` (or
`templates/` in a checkout). You can write them by hand:

```json
{
  "description": "Python sandbox, @pypi only, 2 GB",
  "image": "python-3.13-slim", "ref": "python:3.13-slim",
  "vcpus": 2, "mem_mib": 2048, "disk": null, "copy": false,
  "network": "restricted", "allow": ["@pypi"],
  "ports": [], "volumes": ["pip-cache:/root/.cache/pip"],
  "process": "idle", "command": [], "entrypoint": null,
  "jail": null, "restart": "no"
}
```

| field | |
|---|---|
| `image`, `ref` | the fcvm image name, and optionally where to import it from when it's missing |
| `vcpus`, `mem_mib`, `disk`, `copy` | as `create`'s options. `null`: fcvm's defaults |
| `network`, `allow` | `full`, `none`, or `restricted` with an allowlist of hosts and `@presets` |
| `ports`, `volumes` | as `-p` and `-v` |
| `process`, `command`, `entrypoint` | `image` (the image's command), `idle`, or `command` (the `command` list). `entrypoint`, when set, replaces the image's |
| `jail` | `true`, `false`, or `null` for the default (`JAIL` for the CLI; the MCP server jails when it can) |
| `restart` | the restart policy (`create` only) |

## With agents

The MCP server has a `templates` tool, and `create_sandbox` takes
`template`. Set `FCVM_MCP_TEMPLATE` to the template sandboxes use when the
agent names no image. A pinned network policy (`FCVM_MCP_NETWORK`) still
applies over a template's network. See [Agents](agents.md).
