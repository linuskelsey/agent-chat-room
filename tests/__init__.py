import os
import sys

# The tests act as a person at a terminal. The daemon's human-only guard reads the variables that a Claude
# Code session puts in a process's environment, and /proc keeps showing the original environment even if
# os.environ is edited. So when the suite is started from inside a session, restart it once without them.
MARKERS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID")
if any(m in os.environ for m in MARKERS):
    os.execvpe(sys.executable, sys.orig_argv, {k: v for k, v in os.environ.items() if k not in MARKERS})

# The CLI writes an example config on first use. Point every test at a throwaway location, never the real one.
import tempfile

os.environ.setdefault("ACM_CONFIG", os.path.join(tempfile.mkdtemp(prefix="acm-test-config-"), "config.toml"))

# Likewise never read the real Omarchy usage cache: tests that want that source point at a file of their own.
os.environ.setdefault("ACM_OMARCHY_USAGE_CACHE", "")

