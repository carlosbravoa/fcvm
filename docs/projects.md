# Project files

A project file, `fcvm.toml`, describes the VMs a directory needs, so a
repository can carry its own environment and anyone brings it up with one
command. It's checked in, next to the code; templates, by contrast, are
yours and live in your state directory.

```sh
fcvm up            # create what's missing, start what's stopped (fcvm.toml here or above)
fcvm down          # stop and remove the project's VMs; volumes are kept
fcvm up box        # one of them
fcvm project show  # what the file means, as JSON, with the create command for each VM
```

## The file

```toml
name = "shop"                    # optional; default: the directory's name. VMs are named shop-web, shop-box

[vms.web]
image = "nginx-latest"           # an fcvm image...
ref = "nginx:latest"             # ...imported from here if it's missing
ports = ["8080:80"]
volumes = ["site:/usr/share/nginx/html", "./site:/srv:ro"]   # host paths are relative to the file
restart = "unless-stopped"

[vms.box]
template = "python-sandbox"      # start from a template; everything below overrides or adds
volumes = ["./:/work"]
command = ["python", "/work/app.py"]
ttl = "4h"
secrets = ["API_KEY"]            # from the environment of whoever runs fcvm up
forward = ["git"]
```

Each `[vms.NAME]` table takes the fields of a [template](templates.md#managing-templates):
`image`, `ref`, `vcpus`, `mem_mib`, `disk`, `copy`, `network`, `allow`,
`ports`, `volumes`, `process`, `command`, `entrypoint`, `jail`, `restart`,
`ttl`, `idle_stop`, `secrets`, `forward`, `labels`. Plus:

- **`template`**: a saved template to start from. Lists (`ports`,
  `volumes`, `allow`, `secrets`, `forward`) add to the template's; other
  fields replace them.
- **`idle = true`**: shorthand for `process = "idle"`. A `command` sets
  `process = "command"`.
- **Relative paths** in `volumes` (`./site`, `../data`) are resolved
  against the file's directory, so the project works from any checkout
  location.

## What up and down do

- **`up`** imports a missing image when the file gives its `ref`, creates
  the VMs that don't exist, and starts the ones that aren't running. A VM
  that already exists keeps its settings: after changing the file, `fcvm
  down` and `fcvm up` apply the change (its writable layer is replaced;
  named volumes survive).
- **`down`** stops and removes the project's VMs. Named volumes, images and
  snapshots stay.
- Both take VM names (`fcvm up web`, or the full `shop-web`) to act on a
  subset.

The VMs are ordinary VMs: `fcvm ls` shows them, `fcvm shell shop-box` gets
in, the console and the API see them. Several VMs in one project are
isolated from each other like any other VMs (see
[Networking](networking.md#isolation-defaults)); they share data through
named volumes and host directories, and reach each other's services
through published ports on the host.
