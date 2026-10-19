"""A stand-in for `docker run`, not a test module: a launcher that logs and execs.

    fake_launcher.py LOG [WORDS...] -- COMMAND...

Writes ``{"argv": <its own arguments>, "env": <sorted environment names>}`` to
LOG, then runs COMMAND with stdin, stdout and stderr inherited and exits with
its status.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

log_path, *rest = sys.argv[1:]
command = rest[rest.index("--") + 1 :]
with open(log_path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}))
raise SystemExit(subprocess.call(command))
