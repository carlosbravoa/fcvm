"""Shared bits for unit tests: import fcvm's Python modules from lib/, and run
bash helpers from lib/common.sh in isolation (a throwaway FCVM_HOME)."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(ROOT, "lib")
for p in (LIB, os.path.join(LIB, "web")):
    if p not in sys.path:
        sys.path.insert(0, p)


def bash(script, home=None, env=None):
    """Run SCRIPT after sourcing lib/common.sh, with state in a temp dir."""
    home = home or tempfile.mkdtemp(prefix="fcvm-unit-")
    e = {**os.environ, "FCVM_HOME": home, "FCVM_CONF": os.path.join(home, "none.conf"), **(env or {})}
    return subprocess.run(["bash", "-c", f'. "{LIB}/common.sh"; {script}'],
                          capture_output=True, text=True, env=e)
