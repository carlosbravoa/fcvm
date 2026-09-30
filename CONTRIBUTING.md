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
tests/run integration          # real VMs: about 2 minutes on a set-up host
tests/run lint unit integration -k Snapshots   # a subset, by name
tests/fresh-machine.sh         # everything on a fresh Ubuntu in Multipass, with a reboot (~15 min)
```

| level | what | needs |
|---|---|---|
| **lint** | the syntax of every shell and Python file, `shellcheck -S error` (when installed), and every link and anchor in the docs | nothing |
| **unit** (`tests/unit`) | pure logic: egress allowlist matching, the 9P server's path confinement, Dockerfile parsing, image references, layer flattening with whiteouts, USER resolution, port specs, the supervisor's decisions and pid identity, code/state locations, the installer and upgrades | nothing |
| **integration** (`tests/integration`) | the real CLI and real VMs: lifecycle and exec, cp, volumes and host directories, published ports, isolation (VMs vs host services, vs each other, anti-spoofing), egress allowlists, snapshots and forks (also jailed), the jailer, the API, restart policies, builds, commit, the MCP server, `status` | a set-up host (`fcvm setup`); internet access for the egress tests |
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
what `tests/fresh-machine.sh` covers. Run it before a release.

## Before a release

1. `tests/run lint unit integration` and `tests/fresh-machine.sh` pass, and
   CI is green.
2. Bump `VERSION`, and move the changelog's "Unreleased" notes under the new
   version.
3. Commit, tag `vX.Y.Z` (annotated), push the commit and the tag. The
   installer and `fcvm upgrade` pick up the highest `vX.Y.Z` tag.

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
