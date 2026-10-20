"""fcvm-mcp: start fcvm's MCP server from an MCP client (`uvx fcvm-mcp`).

The server itself is `fcvm mcp`, part of fcvm. This launcher only finds the
installed fcvm and replaces itself with `fcvm mcp`, so MCP registries and
clients can install it by package name. Settings are fcvm's FCVM_MCP_*
environment variables, which pass through unchanged.
"""
import os
import shutil
import sys

__version__ = "0.10.0"

# Where install.sh puts the command (per user, then --system). MCP clients
# often start servers with a minimal PATH, so these are tried after PATH.
FALLBACKS = ["~/.local/bin/fcvm", "/usr/local/bin/fcvm"]


def find_fcvm():
    explicit = os.environ.get("FCVM_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("fcvm")
    if found:
        return found
    for path in FALLBACKS:
        path = os.path.expanduser(path)
        if os.access(path, os.X_OK):
            return path
    return None


def main():
    if any(a in ("-V", "--version") for a in sys.argv[1:]):
        print(f"fcvm-mcp {__version__}")
        return 0
    fcvm = find_fcvm()
    if not fcvm:
        sys.stderr.write(
            "fcvm-mcp: fcvm isn't installed (not on PATH, ~/.local/bin or /usr/local/bin).\n"
            "fcvm needs Linux with KVM. Install it with:\n"
            "  curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh\n"
            "  fcvm setup\n"
            "or set FCVM_BIN to the fcvm command. See https://github.com/carlosbravoa/fcvm\n")
        return 1
    try:
        os.execv(fcvm, [fcvm, "mcp", *sys.argv[1:]])
    except OSError as e:
        sys.stderr.write(f"fcvm-mcp: can't run {fcvm}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
