"""Progress bars show real counts only: steps, pacman's "(n/m)", download bytes, libflatpak's percentages."""

import json
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from cygnus.core import progress as pr
from cygnus.core.progress import Progress


def test_a_progress_line_is_a_plain_string_that_may_carry_a_number(capsys):
    line = Progress("Installing…", 0.25)
    print(line)
    assert capsys.readouterr().out == "Installing…\n"  # the command line never sees the number
    assert isinstance(line, str) and line == "Installing…" and line.fraction == 0.25 and line.log is True
    assert Progress("x", 7).fraction == 1.0 and Progress("x", -2).fraction == 0.0 and Progress("x").fraction is None
    assert Progress("x", 0.5, log=False).log is False


def test_the_executor_callback_reports_which_step_of_how_many():
    seen = []
    on_step = pr.steps(seen.append)
    on_step(0, 4, NS(description="Copy the file", kind="appimage.copy"))
    on_step(3, 4, NS(description="", kind="registry.record"))
    assert [(str(s), s.fraction) for s in seen] == [("Copy the file…", 0.0), ("registry.record…", 0.75)]
    numbered = []
    pr.steps(numbered.append, numbered=True)(1, 3, NS(description="Move it", kind="x"))
    assert str(numbered[0]) == "[2/3] Move it" and numbered[0].fraction == pytest.approx(1 / 3)


def test_a_long_step_reports_how_far_it_is_inside_the_whole_operation():
    seen = []
    pr.steps(seen.append)(1, 4, NS(description="Download", kind="x"))
    seen.clear()
    pr.within_step(0.5, "Downloading… 50%")
    [line] = seen
    assert line.fraction == pytest.approx(1.5 / 4) and line.log is False and str(line) == "Downloading… 50%"
    pr.clear()
    pr.within_step(0.9, "ignored")  # nothing is running: no number is made up
    assert len(seen) == 1


@pytest.mark.parametrize("text, fraction", [
    ("(12/173) upgrading glib2", 12 / 173), ("( 3/22) Updating the desktop file MIME type cache...", 3 / 22),
    ("(173/173) checking keys in keyring", 1.0), ("  (1/1) installing cygnus", 1.0)])
def test_pacmans_counters_become_fractions_of_their_phase(text, fraction):
    line = pr.pacman_line(text)
    assert str(line) == text and line.fraction == pytest.approx(fraction)


@pytest.mark.parametrize("text", ["downloading qt6-base...", "(5/3) odd", "(0/0) nothing", "warning: (3/4)", "", ":: Running pre-transaction hooks..."])
def test_a_line_without_a_real_count_gets_no_number(text):
    assert getattr(pr.pacman_line(text), "fraction", None) is None


BRIDGE = r'''
import json, os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from cygnus.core.progress import Progress
from cygnus.gui.bridge import Backend

app = QCoreApplication([])
backend = Backend()
lines, values, done = [], [], []
backend.progress.connect(lambda rid, text: lines.append(text))
backend.progressValue.connect(lambda rid, v, text: values.append((round(v, 3), text)))
loop = QEventLoop()
backend.finished.connect(lambda rid, payload: (done.append(payload), loop.quit()))

def job(progress):
    progress(Progress("[1/2] first", 0.0))
    progress("plain line")
    progress(Progress("Downloading… 60%", 0.6, log=False))
    progress(Progress("[2/2] second", 0.5))
    return "ok"

backend._submit(job, wants_progress=True)
QTimer.singleShot(10000, loop.quit)
loop.exec()
print(json.dumps({"lines": lines, "values": values, "done": len(done)}))
'''


def test_the_window_gets_text_lines_and_numbers_separately_and_a_quiet_line_only_moves_the_bar():
    out = subprocess.run([sys.executable, "-c", BRIDGE], capture_output=True, text=True, timeout=120)
    report = json.loads([l for l in out.stdout.splitlines() if l.startswith("{")][-1])
    assert report["done"] == 1, out.stderr[-1500:]
    assert report["lines"] == ["[1/2] first", "plain line", "[2/2] second"]  # the quiet line is not in the log
    assert report["values"] == [[0.0, "[1/2] first"], [0.6, "Downloading… 60%"], [0.5, "[2/2] second"]]
