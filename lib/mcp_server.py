#!/usr/bin/env python3
"""fcvm MCP server: Firecracker sandboxes as tools for agents.

  fcvm mcp            (stdio; register with e.g. `claude mcp add fcvm -- /path/to/fcvm mcp`)

Speaks MCP (JSON-RPC 2.0, newline-delimited over stdio) with the standard
library only. Every tool wraps the fcvm CLI, so behaviour matches the command
line exactly. Command output returned to the agent is capped (head and tail
kept) so one noisy command can't flood its context.
"""
import json
import os
import re
import subprocess
import sys

FCVM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fcvm")
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
OUTPUT_CAP = 20000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Operator-pinned network policy for sandboxes created through MCP, e.g.
#   claude mcp add fcvm -e FCVM_MCP_NETWORK=@pypi,@github -- fcvm mcp
# "none", or a comma-separated allowlist. Agents can then only narrow it to
# "none"; there is deliberately no tool to widen an allowlist.
PINNED_NETWORK = os.environ.get("FCVM_MCP_NETWORK", "").strip()

INSTRUCTIONS = """fcvm runs Firecracker microVMs: real kernel isolation, ~1 s to boot a container
image, ~2 s for Ubuntu. Typical loop: images (or pull_image) -> create_sandbox ->
exec / write_file / read_file / copy_* -> commit_vm to save a prepared state ->
remove_vm. build_image turns a Dockerfile into a reusable image. App sandboxes run as the image's USER; pass user="root" to exec
for installs. Network: "full" (NAT), "none", or an allowlist of hosts/@presets
(@pypi, @npm, @github, ...) enforced by a host-side HTTP(S) proxy; the sandbox's
http(s)_proxy variables are preset, and egress_log shows what was allowed or
denied. Files in a sandbox are lost on remove_vm unless committed or stored on
a volume."""


class ToolError(Exception):
    pass


def fcvm(*args, input=None, timeout=None, check=True):
    try:
        p = subprocess.run([FCVM, *map(str, args)], input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"fcvm {args[0]} timed out after {timeout}s")
    out, err = p.stdout.decode(errors="replace"), ANSI.sub("", p.stderr.decode(errors="replace"))
    if check and p.returncode != 0:
        raise ToolError(err.strip() or f"fcvm {args[0]} failed with status {p.returncode}")
    return p.returncode, out, err


def cap(text):
    if len(text) <= OUTPUT_CAP:
        return text, False
    half = OUTPUT_CAP // 2
    return text[:half] + f"\n[... {len(text) - OUTPUT_CAP} characters omitted ...]\n" + text[-half:], True


def inspect(vm):
    return json.loads(fcvm("inspect", vm)[1])


def running(vm):
    """Fail as a tool error (not a command exit code) if the VM can't take commands."""
    state = inspect(vm)["state"]
    if state != "running":
        raise ToolError(f"VM '{vm}' is {state}; start_vm it first")


# --- tools ------------------------------------------------------------------------

def t_images():
    return json.loads(fcvm("images", "--json")[1])


def t_pull_image(ref, name=None):
    fcvm("import", ref, *([name] if name else []), timeout=1800)
    name = name or re.sub(r"[^A-Za-z0-9_.-]", "_", re.sub(r"[@:]", "-", ref.rsplit("/", 1)[-1]))
    return next((i for i in t_images() if i["name"] == name), {"name": name})


def t_build_image(context, tag, file=None, build_args=None, network=None, no_cache=False):
    args = ["build", "-t", tag]
    if file:
        args += ["-f", file]
    for k, v in (build_args or {}).items():
        args += ["--build-arg", f"{k}={v}"]
    if no_cache:
        args.append("--no-cache")
    net = network_args(network)
    if net[:1] == ["--net"] or net[:1] == ["--allow"]:
        args += net
    code, out, err = fcvm(*args, context, timeout=3600, check=False)
    log, _ = cap(ANSI.sub("", err + out))
    if code != 0:
        raise ToolError(f"build failed (exit {code}):\n{log}")
    return {"image": tag, "log": log}


def t_list_vms():
    return json.loads(fcvm("ls", "--json")[1])


def network_args(network):
    if PINNED_NETWORK:
        if network not in (None, "none"):
            raise ToolError(f"the network policy is pinned by the server's configuration ({PINNED_NETWORK}); "
                            "you may only request network='none'")
        network = "none" if network == "none" else PINNED_NETWORK
    if network in (None, "full"):
        return []
    if network == "none":
        return ["--net", "none"]
    allow = network if isinstance(network, list) else network.split(",")
    return ["--allow", ",".join(a.strip() for a in allow if a.strip())]


def t_create_sandbox(image, name=None, command=None, vcpus=None, mem_mib=None, ports=None, volumes=None,
                     network=None):
    img = next((i for i in t_images() if i["name"] == image), None)
    if not img:
        raise ToolError(f"no image '{image}'; see the images tool or pull_image")
    if not name:
        name = f"{image.split('-')[0]}-{os.urandom(3).hex()}"
    args = ["create", name, image]
    if vcpus:
        args += ["--vcpus", vcpus]
    if mem_mib:
        args += ["--mem", mem_mib]
    for p in ports or []:
        args += ["-p", p]
    for v in volumes or []:
        args += ["-v", v]
    args += network_args(network)
    if img["type"] == "app":
        args += ["--", *command] if command else ["--idle"]
    fcvm(*args)
    fcvm("start", name)
    return inspect(name)


def t_exec(vm, command=None, argv=None, workdir=None, env=None, user=None, timeout=300, stdin=None):
    if bool(command) == bool(argv):
        raise ToolError("pass exactly one of command (a shell string) or argv (a list)")
    running(vm)
    args = ["exec", "--timeout", timeout]
    if stdin is not None:
        args.append("-i")
    if workdir:
        args += ["-w", workdir]
    if user:
        args += ["-u", user]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args += [vm, *(["sh", "-c", command] if command else argv)]
    code, out, err = fcvm(*args, input=(stdin or "").encode() if stdin is not None else None,
                          timeout=timeout + 30, check=False)
    out, t1 = cap(out)
    err, t2 = cap(err)
    result = {"exit_code": code, "stdout": out, "stderr": err}
    if code == 124:
        result["timed_out"] = True
    if t1 or t2:
        result["truncated"] = True
    return result


def t_write_file(vm, path, content, user=None):
    running(vm)
    args = ["exec", "-i", *(["-u", user] if user else []), vm, "sh", "-c",
            'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path]
    fcvm(*args, input=content.encode())
    return {"written": path, "bytes": len(content.encode())}


def t_read_file(vm, path, max_bytes=100000):
    running(vm)
    code, out, err = fcvm("exec", vm, "sh", "-c", 'head -c "$2" "$1"', "sh", path, max_bytes, check=False)
    if code != 0:
        raise ToolError(err.strip() or f"cannot read {path}")
    return {"path": path, "content": out, "truncated": len(out.encode()) >= max_bytes}


def t_copy_to_vm(vm, host_path, vm_path):
    fcvm("cp", host_path, f"{vm}:{vm_path}", timeout=1800)
    return {"copied": host_path, "to": f"{vm}:{vm_path}"}


def t_copy_from_vm(vm, vm_path, host_path):
    fcvm("cp", f"{vm}:{vm_path}", host_path, timeout=1800)
    return {"copied": f"{vm}:{vm_path}", "to": host_path}


def t_logs(vm, tail_lines=100):
    out = fcvm("logs", vm)[1].replace("\r", "")
    return {"logs": "\n".join(out.splitlines()[-tail_lines:])}


def t_stop_vm(vm):
    fcvm("stop", vm, timeout=60)
    return {"stopped": vm}


def t_start_vm(vm):
    fcvm("start", vm)
    return inspect(vm)


def t_remove_vm(vm):
    fcvm("stop", vm, timeout=60)
    if any(v["name"] == vm for v in t_list_vms()):   # throwaway VMs are gone already
        fcvm("rm", vm)
    return {"removed": vm}


def t_commit_vm(vm, image):
    was_running = inspect(vm)["state"] == "running"
    if was_running:
        fcvm("stop", vm, timeout=60)
    fcvm("commit", vm, image)
    if was_running:
        fcvm("start", vm)
    return {"image": image, "from": vm, "restarted": was_running}


def t_egress_log(vm, lines=50):
    return {"log": ANSI.sub("", fcvm("egress", vm, "-n", lines)[1])}


def t_volumes():
    return json.loads(fcvm("volume", "ls", "--json")[1])


S = {"type": "string"}
I = {"type": "integer"}
TOOLS = {
    "images": (t_images, "List VM images. type 'app': runs the image's command as the VM's single process (imported container images); type 'system': boots systemd like a full machine (the Ubuntu base).", {}, []),
    "pull_image": (t_pull_image, "Import an image from Docker Hub or any OCI registry (e.g. python:3.13, ghcr.io/org/app:tag), or a local docker save / OCI archive path.",
                   {"ref": S, "name": {**S, "description": "image name (default: derived from ref)"}}, ["ref"]),
    "build_image": (t_build_image,
        "Build an image from a Dockerfile subset (FROM, RUN, COPY, ADD, ENV, ARG, WORKDIR, USER, CMD, ENTRYPOINT, EXPOSE) in a host directory. Steps are cached; the result is its FROM image plus one layer, usable with create_sandbox.",
        {"context": {**S, "description": "host directory with the Dockerfile (or Fcvmfile) and the files it COPYs"},
         "tag": {**S, "description": "name of the new image"}, "file": S,
         "build_args": {"type": "object", "additionalProperties": S},
         "network": {"description": 'network for RUN steps: full (default), "none", or an allowlist', "anyOf": [S, {"type": "array", "items": S}]},
         "no_cache": {"type": "boolean"}}, ["context", "tag"]),
    "list_vms": (t_list_vms, "List VMs with state, IP, image, ports and volumes.", {}, []),
    "create_sandbox": (t_create_sandbox,
        "Create and boot a VM from an image. Container images stay up idle for exec unless a command is given. Returns the VM's details (name, ip).",
        {"image": S, "name": S,
         "command": {"type": "array", "items": S, "description": "run this instead of staying idle (app images)"},
         "vcpus": I, "mem_mib": I,
         "ports": {"type": "array", "items": S, "description": "publish TCP ports, [BIND:]HOST:GUEST"},
         "volumes": {"type": "array", "items": S, "description": "named volumes, NAME:/PATH[:ro] (created on first use)"},
         "network": {"description": 'full (default), "none", or an allowlist: ["@pypi", "github.com", "*.example.com"]'
                     + (f". Pinned by the server to: {PINNED_NETWORK}" if PINNED_NETWORK else ""),
                     "anyOf": [S, {"type": "array", "items": S}]}},
        ["image"]),
    "exec": (t_exec, "Run a command in a running VM. Returns exit_code, stdout and stderr (capped).",
             {"vm": S, "command": {**S, "description": "shell command, run with sh -c"},
              "argv": {"type": "array", "items": S, "description": "exact argv, for images without a shell"},
              "workdir": S, "env": {"type": "object", "additionalProperties": S},
              "user": {**S, "description": "name|uid[:group|gid]; default: the image's USER"},
              "timeout": {**I, "description": "seconds, default 300; exit_code 124 on timeout"},
              "stdin": {**S, "description": "text fed to the command's stdin"}}, ["vm"]),
    "write_file": (t_write_file, "Write a text file in a running VM (parent directories are created).",
                   {"vm": S, "path": S, "content": S, "user": S}, ["vm", "path", "content"]),
    "read_file": (t_read_file, "Read a text file from a running VM.",
                  {"vm": S, "path": S, "max_bytes": I}, ["vm", "path"]),
    "copy_to_vm": (t_copy_to_vm, "Copy a file or directory from the host into a running VM (like docker cp).",
                   {"vm": S, "host_path": S, "vm_path": S}, ["vm", "host_path", "vm_path"]),
    "copy_from_vm": (t_copy_from_vm, "Copy a file or directory from a running VM to the host.",
                     {"vm": S, "vm_path": S, "host_path": S}, ["vm", "vm_path", "host_path"]),
    "logs": (t_logs, "Console output of a VM (its main process, or boot messages).",
             {"vm": S, "tail_lines": I}, ["vm"]),
    "stop_vm": (t_stop_vm, "Shut a VM down gracefully. Its disk is kept.", {"vm": S}, ["vm"]),
    "start_vm": (t_start_vm, "Boot a stopped VM.", {"vm": S}, ["vm"]),
    "remove_vm": (t_remove_vm, "Stop and delete a VM and its writable layer. Volumes are kept.", {"vm": S}, ["vm"]),
    "commit_vm": (t_commit_vm,
        "Save a VM's changes as a new image (a layer on its image), e.g. after installing dependencies, so new sandboxes start from that state. A running VM is stopped and restarted.",
        {"vm": S, "image": S}, ["vm", "image"]),
    "volumes": (t_volumes, "List named volumes.", {}, []),
    "egress_log": (t_egress_log, "A sandbox's network policy and its recent allowed/denied requests.",
                   {"vm": S, "lines": I}, ["vm"]),
}


def tool_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}}
            for n, (_, d, p, r) in TOOLS.items()]


def call_tool(name, arguments):
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name}")
    fn = TOOLS[name][0]
    try:
        result = fn(**(arguments or {}))
    except TypeError as e:
        raise ToolError(f"bad arguments: {e}")
    return result


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fcvm", "version": "0.4"},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        try:
            result = call_tool(params.get("name"), params.get("arguments"))
            return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}], "isError": False}
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    raise LookupError(method)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            print(json.dumps(reply), flush=True)
            continue
        if "id" not in msg:        # notification (e.g. notifications/initialized)
            continue
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"method not found: {msg.get('method')}"}}
        except Exception as e:     # never let one bad call kill the server
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
