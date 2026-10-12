"""The update icon in the system tray (`cygnus-tray`; switched on in Settings, see cygnus/core/tray.py).

It looks at the stored results of the last update checks once a minute (files only: no network, nothing installed, no root helper) and shows
an icon while something waits. Clicking it opens Cygnus; the menu can also look for updates now. When the choice is switched off in Settings it
notices and quits. One copy runs at a time."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from cygnus.core import paths
from cygnus.core import tray as core

POLL_MS = 60_000
WAIT_FOR_TRAY_SECONDS = 60  # the panel may still be starting when the session begins


def gui_command() -> list[str]:
    found = shutil.which("cygnus-gui")
    return [found] if found else [sys.executable, "-m", "cygnus.gui.app"]


def check_command() -> list[str]:
    found = shutil.which("cygnus")
    return [found, "watch"] if found else [sys.executable, "-m", "cygnus", "watch"]


class Controller:
    """What the icon shows and when it goes. `icon` is a QSystemTrayIcon (or anything with setToolTip and setVisible), `quit` ends the program;
    both are handed in so this can be tried without a window."""

    def __init__(self, icon: Any, quit: Callable[[], None], *, state: Callable[[], dict] = core.pending, wanted: Callable[[], bool] = core.wanted) -> None:
        self.icon, self.quit, self.state, self.wanted = icon, quit, state, wanted
        self.shown: dict[str, Any] | None = None

    def refresh(self) -> None:
        if not self.wanted():  # switched off in Settings: go
            self.quit()
            return
        try:
            shown = core.describe(self.state())
        except Exception:  # noqa: BLE001 - a file that cannot be read this minute is no reason to leave a broken icon
            return
        if shown == self.shown:
            return
        self.shown = shown
        self.icon.setToolTip(shown["tooltip"])
        self.icon.setVisible(shown["visible"])


def main(argv: list[str] | None = None) -> int:
    if not core.wanted():
        print("The update icon is switched off (Settings → Update icon).")
        return 0
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")
    from PySide6.QtCore import QLockFile, QProcess, QTimer
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    from cygnus import APP_ID, APP_NAME

    app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName(f"{APP_NAME} updates")
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    folder = Path(runtime) if runtime and os.path.isdir(runtime) else paths.state_dir()  # both are the person's own: never a shared folder like /tmp
    folder.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(folder / "cygnus-tray.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if lock.error() == QLockFile.LockError.LockFailedError:
            return 0  # another copy is already running
        print(f"The update icon could not take its lock file ({lock.error().name}).", file=sys.stderr)
        return 1
    icon = QSystemTrayIcon(QIcon.fromTheme(core.ICON, QIcon.fromTheme(APP_ID)))
    controller = Controller(icon, app.quit)

    def start(argv: list[str]) -> None:
        QProcess.startDetached(argv[0], argv[1:])

    menu = QMenu()
    open_action = QAction("Open Cygnus", menu)
    open_action.triggered.connect(lambda: start(gui_command()))
    check_action = QAction("Look for updates now", menu)
    check_action.triggered.connect(lambda: start(check_command()))
    quit_action = QAction("Hide until next login", menu)
    quit_action.triggered.connect(app.quit)
    for action in (open_action, check_action):
        menu.addAction(action)
    menu.addSeparator()
    menu.addAction(quit_action)
    icon.setContextMenu(menu)
    icon.activated.connect(lambda reason: start(gui_command()) if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    waited = {"seconds": 0}

    def begin() -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            controller.refresh()
            timer.start(POLL_MS)
        elif waited["seconds"] >= WAIT_FOR_TRAY_SECONDS:
            print("There is no system tray to show the icon in.", file=sys.stderr)
            app.quit()
        else:
            waited["seconds"] += 5
            QTimer.singleShot(5000, begin)

    timer = QTimer()
    timer.timeout.connect(controller.refresh)
    QTimer.singleShot(0, begin)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
