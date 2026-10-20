# Contributing

fcvm is bash and standard-library Python on the host, one C file in the
guest (`init/fc-init.c`), and a kernel config fragment. There's nothing to
install to work on it beyond what `fcvm setup` installs. [How it
works](docs/internals.md) explains the architecture, and why it is the way
it is.

- [Running fcvm from a checkout](#running-fcvm-from-a-checkout)
- [Tests](#tests)
- [Continuous integration](#continuous-integration)
- [Before a release](#before-a-release)
- [Style](#style)

## Running fcvm from a checkout

```sh
git clone https://github.com/carlosbravoa/fcvm ~/src/fcvm && cd ~/src/fcvm
./fcvm setup
```

A fresh clone keeps its state in `~/.local/share/fcvm`, like an installed
copy. `fcvm version` shows the code and state locations. Link the checkout
onto your PATH if you like: `ln -s "$PWD/fcvm" ~/.local/bin/fcvm`. Don't
also install a release: the network, jailer helper and service serve one
state directory per user.

## Tests

```sh
tests/run                      # lint + unit tests: seconds, no KVM, no root
tests/run integration          # real VMs: about 5 minutes on a set-up host
tests/run lint unit integration -k Snapshots   # a subset, by name
tests/fresh-machine.sh         # everything on a fresh Ubuntu in Multipass, with a reboot (~15 min)
tests/fresh-ec2.sh             # the same on a throwaway EC2 bare-metal instance (~45 min, about $4)
```

| level | what | needs |
|---|---|---|
| **lint** | the syntax of every shell and Python file, `shellcheck -S error` (when installed), and every link and anchor in the docs | nothing |
| **unit** (`tests/unit`) | pure logic: egress allowlist matching and the mirror, the 9P server's path confinement, Dockerfile parsing, image references, layer flattening with whiteouts, USER resolution, port specs, the supervisor's decisions (restart policies, lifetimes, pools) and pid identity, templates and project files, the secrets and forwarding servers, OCI export and push, the Python client, code/state locations, the installer and upgrades | nothing |
| **integration** (`tests/integration`) | the real CLI and real VMs: lifecycle and exec, background jobs, cp, volumes and host directories, published and live ports, isolation (VMs vs host services, vs each other, anti-spoofing), egress allowlists and the mirror, secrets and credential forwarding, the balloon, snapshots and forks (also jailed) and their export/import, image export, the jailer (with secrets, ports, forwarding, the mirror and pools inside it), the API and both client libraries, lifetimes, warm pools, restart policies, builds, commit, templates and project files, `ssh-config`, the MCP server, `status` | a set-up host (`fcvm setup`); internet access for the egress tests; `ubuntu-26.04` and `python-3.13-slim` images for a few (skipped otherwise) |
| **fresh machine** (`tests/fresh-machine.sh`) | install from this tree, `setup` twice (across the kvm-group re-login), all of the above from the installed copy, then a real reboot | Multipass, ~4 CPUs, 8 GB, 25 GB disk |

**The integration tests are safe on your own machine.**
- **Names.** They only create, and clean up, VMs, images, volumes and
  snapshots named `fcvmtest-*`. They sweep leftovers from an interrupted run
  first. Your own VMs are never touched.
- **State.** They run against your normal state directory, because the
  network, the jailer helper and the service serve one.
- **Server.** When the fcvm service is running, they use it. Otherwise they
  start `fcvm serve` on a free port for the API and supervisor tests.
- **Skips.** Tests that need something missing (the jailer helper for this
  state directory, `NET_ISOLATE=1`, ...) are skipped, not failed. A host
  that isn't set up at all skips the integration tests with a message saying
  what's missing.

**Changing the supervisor or the API while the service runs.** The tests use
the running fcvm service, which executes the supervisor and server code it
was started with, not your edited `lib/web/*.py` (the CLI it runs *is* the
checkout's). Either `fcvm service install` (sudo; restarts it on the new
code, VMs keep running), or run a second server from the tree and let the
tests find it:

```sh
cp vms/.serve.json /tmp/serve.bak              # the service's state file: a test serve overwrites it
./fcvm serve --port 9797 & pid=$!              # the tests then use this one
tests/run integration -k Service
kill $pid; cp /tmp/serve.bak vms/.serve.json   # back to the service
```

**Writing tests.**
- **Unit tests** import modules from `lib/` directly (see
  `tests/unit/helpers.py`), or source `lib/common.sh` with a throwaway
  `FCVM_HOME`.
- **Integration tests** derive from `VMTestCase` in
  `tests/integration/fcvmtest.py`. Its `vm()`, `snapshot()`, `volume()` and
  `image_name()` register cleanups, and `sh(vm, script)` runs a command
  inside a VM.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:
- **lint and unit tests**, with shellcheck;
- **integration tests** on a fresh GitHub runner. The job opens `/dev/kvm`
  (the runners support KVM), runs `fcvm setup -y`, then
  `tests/run integration`. The guest kernel is cached, keyed on the newest
  stable version and the config fragment. On failure, it prints
  `fcvm status`, the services' journals and the VMs' consoles.

CI can't reboot or start from a machine without KVM access set up, which is
what `tests/fresh-machine.sh` covers. Run it before a release. Without
Multipass (or without the disk for it), `tests/fresh-ec2.sh` does the same
on a throwaway `c5.metal` instance with the configured `aws` CLI: its own
key pair and security group (SSH from your address only), terminated
afterwards, and self-terminating after two hours whatever happens. It
reboots from inside the guest: `aws ec2 reboot-instances` hard-resets a
bare-metal instance that takes long to shut down, which would show as
VMs not stopped cleanly.

## Before a release

1. `tests/run lint unit integration` and `tests/fresh-machine.sh` (or
   `tests/fresh-ec2.sh`) pass, and
   CI is green.
2. Bump `VERSION`, and the same version in the packages under `sdk/`
   (`tests/run lint` lists any it missed). Move the changelog's
   "Unreleased" notes under the new version. If a file, field, flag or
   code listed in [docs/node-contract.md](docs/node-contract.md) was
   renamed or removed, bump the contract version there, in `CONTRACT`
   (`lib/common.sh`) and in `host_info` (`lib/web/server.py`), and say so
   in the changelog.
3. Commit, tag `vX.Y.Z` (annotated), push the commit and the tag. The
   installer and `fcvm upgrade` pick up the highest `vX.Y.Z` tag.
4. Publish the packages, below.

### Publishing the packages

Three packages carry fcvm's version: the Python client `fcvm` and the MCP
launcher `fcvm-mcp` on PyPI, and the Node client `fcvm` on npm. The launcher
is listed in the [MCP Registry](https://registry.modelcontextprotocol.io/)
as `io.github.carlosbravoa/fcvm`, from `sdk/mcp/server.json`.

```sh
python3 -m venv /tmp/pub && /tmp/pub/bin/pip install build twine
for d in sdk/python sdk/mcp; do /tmp/pub/bin/python -m build -o /tmp/pub/dist "$d"; done
/tmp/pub/bin/twine upload /tmp/pub/dist/*          # a PyPI API token
(cd sdk/typescript && npm publish)                  # after npm login
(cd sdk/mcp && mcp-publisher login github && mcp-publisher publish)
```

The registry checks that the PyPI package is ours through the `mcp-name`
comment in `sdk/mcp/README.md`, so publish to PyPI first.
`mcp-publisher` comes from the
[registry's releases](https://github.com/modelcontextprotocol/registry/releases).

## Style

- **Match the surrounding code.** Comments explain why, not what. Messages
  are short and actionable: an error says what to run to fix it.
- **Bash** runs with `set -euo pipefail`. Watch for pipelines that may
  legitimately match nothing (add `|| true`), and for `local x=$(...)`
  hiding a failure.
- **Python** is standard library only. `lib/` scripts run on the Python of
  a stock Ubuntu LTS.
- **Docs.** A user-visible change updates the docs in the same commit. The
  lint step checks the links.
