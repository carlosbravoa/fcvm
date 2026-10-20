# fcvm: notes for Claude Code sessions

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: layout, tests, style, the
release checklist. This file is what a session tends to get wrong.

## Shape of the code

- `fcvm` dispatches to `lib/*.sh` (bash, `set -euo pipefail`) and stdlib-only
  Python in `lib/` (`web/server.py` API and console, `web/supervisor.py`,
  `mcp_server.py`, `templates.py`, `project.py`, `egress_proxy.py`, ...).
  `init/fc-init.c` is the static guest init and exec agent; the initramfs
  rebuilds itself when it changes.
- **A feature has five surfaces:** CLI (`lib/vm.sh`, usage text in `fcvm`,
  `docs/cli.md`), HTTP API (`docs/api.md`, and both SDKs in `sdk/`), MCP
  tools (`lib/mcp_server.py`, `docs/agents.md`), templates and `fcvm.toml`
  (`lib/templates.py`, `lib/project.py`), the web console
  (`lib/web/static/app.js`). Plus `docs/*.md`, `README.md`'s feature list,
  `CHANGELOG.md`, and tests. Budget for all of them: a "small" feature is
  300 to 800 lines with tests and docs. Say so before starting a batch.
- Host-side services for a VM talk vsock: `<vsock uds>_PORT` (9000 secrets,
  9001 ssh-agent, 9002 git credentials, 10000+ shares). Jailed VMs need
  the chroot path (`vsock_prefix`); test new ones in a jail too.
- `use_daemon`/`daemon_api`: the CLI delegates start/fork/publish to the
  running service. `FCVM_DAEMON=1` disables that (tests that need the
  caller's environment, like forwarding, use it).

## Testing

- `tests/run lint unit integration`: 101 unit tests in seconds, 80
  integration tests in about 5 minutes on real VMs. Run `-k Name` subsets
  while working, the whole thing before a commit that touches `lib/vm.sh`.
- Lint silently skips shellcheck when it isn't installed; CI runs it. If
  `shellcheck` is missing, fetch the static release binary into the
  scratchpad and put it on PATH for the run.
- Integration tests only touch `fcvmtest-*` names and use the running fcvm
  service. **The service runs the supervisor/server code it was started
  with.** After editing `lib/web/*.py`, either ask the user to run
  `! ./fcvm service install` (sudo) or test against a temporary serve, see
  "Changing the supervisor or the API" in CONTRIBUTING. `lib/jaild.py`
  changes need `! ./fcvm jail-setup`.
- Never `pkill -f "some pattern"` from a tool call: the pattern matches the
  shell running it. Kill by pid, or use a `[.]` in the pattern.
- `tests/fresh-ec2.sh` runs the fresh-machine test on a throwaway EC2
  bare-metal instance (the `aws` CLI is configured); it cleans up after
  itself, and `--keep` leaves the instance for inspection.
- `tests/fresh-machine.sh` needs Multipass with about 10 GB free on the
  root disk; CI's integration job covers the same minus the reboot.

## Conventions

- Features are minor bumps (`0.x.0`); fixes are patch bumps. Bump `VERSION`,
  move changelog notes under the version, annotated tag `vX.Y.Z`, push only
  when asked. `lib/mcp_server.py` reads `VERSION`.
- One commit per feature or fix, with its tests and docs. Messages say what
  was wrong and what changed, not what files were edited.
- Docs are prose, short, with runnable examples; `tests/run lint` checks
  every link. `fcvm` usage text and `docs/cli.md` must agree.
- When the user asks for a review, run `/code-review high <paths>`; verify
  each finding against the code before fixing. The two reviews so far
  found real bugs in both the old tree and the fresh features.
