"""The real QML UI loads offscreen with no QML warnings, and the install flow analyses a file."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import builders

pytest.importorskip("PySide6")
SMOKE = Path(__file__).with_name("gui_smoke.py")
# Kirigami's PageRow creates every pushed page with a QtObject parent (PageRow.qml, initPage), which
# makes Qt print this for any Kirigami app; it is not about Cygnus's QML.
UPSTREAM = "Created graphical object was not placed in the graphics scene."


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_every_page_loads_without_warnings(tmp_path):
    app = builders.build_appimage(tmp_path)
    proc = subprocess.run([sys.executable, str(SMOKE), str(app)], capture_output=True, text=True, timeout=120)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    assert proc.returncode == 0 and lines, proc.stderr[-3000:]
    report = json.loads(lines[-1])
    assert report["window"] and not report["errors"]
    assert report["pages"] == {"apps": "Applications", "app": "Hello World", "updates": "Updates",
                               "storage": "Storage", "settings": "Settings", "install": "Install"}
    assert [w for w in report["warnings"] if not w.endswith(UPSTREAM)] == []
    plan = report["install_plan"]
    assert plan and plan["format"] == "appimage" and plan["name"] == "Hello World"
    # the smoke run starts from an empty registry, so the file is analysed but cannot be installed yet
    assert not plan["installable"] and any(i["code"] == "STORAGE_NOT_SET_UP" for i in plan["issues"])
