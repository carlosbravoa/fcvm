# Error codes

Every fcvm failure prints one `error:` line and exits non-zero. For programs
(the web console, the MCP server, the client libraries, your scripts), the
CLI also names the failure's **code**, so they can tell "no such VM" from
"already running" without parsing prose:

- **On the command line**, when stderr isn't a terminal, a second line
  `fcvm-error: CODE` follows the `error:` line:

  ```sh
  $ fcvm start box 2>&1 | cat
  error: VM 'box' is already running (pid 41023)
  fcvm-error: vm_running
  ```

- **In the [HTTP API](api.md)**, the reply is `{"error": "message",
  "code": "CODE"}` with the status below.
- **In the client libraries**, `FcvmError` has a `code` attribute next to
  `status`.

## The codes

| code | status | meaning |
|---|---|---|
| `vm_not_found` | 404 | no VM of that name |
| `image_not_found` | 404 | no image of that name (`fcvm images`) |
| `snapshot_not_found` | 404 | no snapshot of that name |
| `volume_not_found` | 404 | no volume of that name |
| `pool_not_found` | 404 | no warm pool of that name |
| `not_found` | 404 | something else named in the request doesn't exist (a job, a mount) |
| `vm_exists` | 409 | a VM of that name already exists |
| `image_exists`, `snapshot_exists`, `volume_exists` | 409 | the name is taken |
| `vm_running` | 409 | the VM is running and the command needs it stopped (`rm`, `commit`), or it's already running (`start`) |
| `vm_not_running` | 409 | the command needs a running VM (`exec`, `cp`, `snapshot`, `publish`...) |
| `vm_busy` | 409 | another fcvm command is changing the VM; retry |
| `no_agent` | 409 | the VM was created with `--no-agent` and the command needs the exec agent |
| `no_network` | 409 | the VM has `--net none` and the command needs a network (`publish`, `ssh`) |
| `in_use` | 409 | an image or volume can't be removed or attached because VMs use it |
| `pool_empty` | 409 | the pool has no ready VM right now |
| `no_network_slot` | 503 | no free tap on the bridge; the host is at its VM limit for that network |
| `jail_unavailable` | 503 | the VM is jailed but `fcvm-jaild` isn't running |
| `timeout` | 504 | the API's call to the CLI took too long (the operation may still complete) |
| `invalid` | 400 | the request is malformed: an unknown option, a bad name or spec, a missing argument |
| `error` | 400 | any other failure; the message says what went wrong |

Codes are stable: a release may add codes, and the notes say so, but
doesn't rename or remove them. A failure that has no code yet is `error`.
