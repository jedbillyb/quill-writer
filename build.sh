#!/usr/bin/env bash
# Package the extension into claude-writer.oxt (a zip), using Python's zipfile
# so no external `zip` binary is required.
#
# The sidecar Python venv (.venv) is NOT bundled — it is platform-specific and
# heavy. Create it once after install (see README) or point CLAUDE_WRITER_PYTHON
# at any Python 3.10+ that has claude-agent-sdk installed.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" - <<'PYCODE'
import zipfile, os, sys

OUT = "claude-writer.oxt"
FILES = [
    "description.xml",
    "description-en.txt",
    "META-INF/manifest.xml",
    "Factories.xcu",
    "Sidebar.xcu",
    "Addons.xcu",
    "python/claude_panel.py",
    "python/sidecar_client.py",
    "python/writer_ops.py",
    "sidecar/agent_main.py",
    "sidecar/writer_tools.py",
    "icons/claude.png",
]

missing = [f for f in FILES if not os.path.exists(f)]
if missing:
    sys.exit("Missing files: " + ", ".join(missing))

if os.path.exists(OUT):
    os.remove(OUT)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for f in FILES:
        z.write(f)

print("Built", OUT)
with zipfile.ZipFile(OUT) as z:
    for n in z.namelist():
        print("  ", n)
PYCODE
