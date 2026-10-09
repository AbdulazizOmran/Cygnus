"""Progress lines that can carry a real number.

A progress line is a plain string, so the command line just prints it. A window can also read `fraction` (0..1) when
there is a real count behind it (step 3 of 6, pacman's "(12/173)", a download's bytes): that is what a progress bar
shows. Nothing here ever guesses a fraction: no count, no number, and the bar shows that it is busy instead."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar


class Progress(str):
    """The text of a progress line, plus `fraction` (None when unknown) and `log` (False for a line that only moves the
    bar and is not worth adding to the log)."""

    fraction: float | None
    log: bool

    def __new__(cls, text: str, fraction: float | None = None, *, log: bool = True) -> Progress:
        self = super().__new__(cls, text)
        self.fraction = None if fraction is None else min(1.0, max(0.0, float(fraction)))
        self.log = log
        return self


# The step an operation is in, so a long step (a download) can report how far it is inside the whole operation.
_step: ContextVar[tuple[int, int, Callable[[str], None]] | None] = ContextVar("progress_step", default=None)


def steps(progress: Callable[[str], None], *, numbered: bool = False) -> Callable[[int, int, object], None]:
    """The executor's per-step callback: "step i of n" with the step's own description (`numbered` puts "[i/n]" in
    front of the text, as the log of some operations always did)."""
    def on_step(i: int, n: int, step) -> None:
        _step.set((i, n, progress))
        what = getattr(step, "description", "") or getattr(step, "kind", "")
        progress(Progress(f"[{i + 1}/{n}] {what}" if numbered else f"{what}…", i / n if n else None))
    return on_step


def within_step(fraction: float, text: str) -> None:
    """A long step says how far it is (0..1). Counted as part of the current operation; not added to the log."""
    current = _step.get()
    if current is not None:
        i, n, progress = current
        progress(Progress(text, (i + min(1.0, max(0.0, fraction))) / n if n else None, log=False))


def clear() -> None:
    _step.set(None)


# pacman prints "(12/173) upgrading glib2" for every package and every hook; each phase counts from 1 again.
_PACMAN_COUNT = re.compile(r"^\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)\s*(.*)$")


def pacman_line(line: str) -> str:
    """A line from pacman, with the fraction of its phase when it is a "(n/m) …" line."""
    m = _PACMAN_COUNT.match(line)
    if m and int(m.group(2)) > 0 and int(m.group(1)) <= int(m.group(2)):
        return Progress(line, int(m.group(1)) / int(m.group(2)))
    return line
