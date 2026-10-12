#!/usr/bin/env python3
"""The published packages carry fcvm's VERSION: the SDKs, the MCP launcher and
its MCP Registry entry. A release bumps them all together."""
import json
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[2]
want = (root / "VERSION").read_text().strip()
found = {}
for f in ["sdk/python/pyproject.toml", "sdk/mcp/pyproject.toml"]:
    found[f] = re.search(r'^version = "([^"]+)"', (root / f).read_text(), re.M).group(1)
for f in ["sdk/python/fcvm.py", "sdk/mcp/fcvm_mcp.py"]:
    found[f] = re.search(r'^__version__ = "([^"]+)"', (root / f).read_text(), re.M).group(1)
found["sdk/typescript/package.json"] = json.loads((root / "sdk/typescript/package.json").read_text())["version"]
server = json.loads((root / "sdk/mcp/server.json").read_text())
found["sdk/mcp/server.json"] = server["version"]
for p in server["packages"]:
    found[f"sdk/mcp/server.json ({p['identifier']})"] = p["version"]
bad = {f: v for f, v in found.items() if v != want}
for f, v in bad.items():
    print(f"version: {f} says {v}, VERSION is {want}")
sys.exit(1 if bad else 0)
