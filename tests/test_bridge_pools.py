"""A job that holds its thread for minutes (a commit, a build, a download) must not freeze the quick requests."""

import subprocess
import sys

import pytest

pytest.importorskip("PySide6")

SCRIPT = r'''
import json, os, sys, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from cygnus.gui.bridge import Backend

app = QCoreApplication([])
backend = Backend()
release = threading.Event()
started = []
done = {}
backend.finished.connect(lambda rid, payload: done.__setitem__(rid, json.loads(payload)))

def blocking(label):
    def run(*_):
        started.append(label)
        release.wait(30)
        return label
    return run

# three kinds of long job: one that reports progress, one marked long, and a second of each
long_ids = [backend._submit(blocking("progress-1"), wants_progress=True), backend._submit(blocking("long-1"), long=True),
            backend._submit(blocking("progress-2"), wants_progress=True)]
quick_id = backend._submit(lambda: "quick answer")
loop = QEventLoop()
timeout = QTimer(); timeout.setSingleShot(True); timeout.timeout.connect(loop.quit); timeout.start(5000)
backend.finished.connect(lambda rid, payload: loop.quit() if rid == quick_id else None)
loop.exec()
quick_done_while_long_jobs_blocked = quick_id in done and not any(r in done for r in long_ids)
release.set()
loop2 = QEventLoop()
poll = QTimer(); poll.timeout.connect(lambda: loop2.quit() if all(r in done for r in long_ids) else None); poll.start(50)
QTimer.singleShot(10000, loop2.quit)
loop2.exec()
print(json.dumps({"quick_first": quick_done_while_long_jobs_blocked, "quick": done.get(quick_id),
                  "long_all_finished": all(r in done for r in long_ids), "started": sorted(started)}))
'''


def test_quick_requests_are_answered_while_every_long_job_is_still_running():
    out = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120)
    import json

    report = json.loads([l for l in out.stdout.splitlines() if l.startswith("{")][-1])
    assert report["quick_first"] is True, out.stderr[-2000:]
    assert report["quick"] == {"ok": True, "result": "quick answer"}
    assert report["long_all_finished"] and report["started"] == ["long-1", "progress-1", "progress-2"]


def test_the_slow_planning_and_network_jobs_are_not_on_the_quick_pool():
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "cygnus" / "gui" / "bridge.py").read_text()
    for name in ("service.analyse_flatpak_ref(", "service.aur_review(", "fixes.plan_repo_dependencies(",
                 "fixes.plan_local_package(", "fixes.plan_system_upgrade", "fixes.plan_built_packages(",
                 "fixes.plan_remove_packages(", "fixes.plan(app, component)"):  # the last may download a package
        call = re.search(r"self\._submit\((?:(?!\n    @Slot|\n    def ).)*?" + re.escape(name) + r".*?\)\n", source, re.S)
        assert call and "long=True" in call.group(0), name
    # analysing a file reports progress (it may have to fetch the package file lists), which also puts it on the long pool
    assert re.search(r"def analyseFile.*?wants_progress=True", source, re.S)
