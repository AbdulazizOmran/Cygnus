"""Cygnus GUI entry point.

QML is loaded *asynchronously*: a synchronous load deadlocks under PySide6 when Kirigami's plugin
is instantiated on Qt's QML loader thread (it needs the GIL the main thread is holding).

Cygnus can be the default program for Flathub's Install button and software files, so a launch with arguments (a
.flatpakref, an appstream: link, a .deb…) that finds a window already open hands them to it and exits (see instance.py).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cygnus import APP_ID, APP_NAME, __version__

QML_DIR = Path(__file__).with_name("qml")


# Qt's own options that take a value ("-platform offscreen", also written "--platform"): the value is not a file.
QT_VALUE_OPTIONS = frozenset({"platform", "platformtheme", "style", "stylesheet", "qmljsdebugger", "geometry", "display",
                              "qwindowtitle", "qwindowicon", "plugin", "session"})


def page_request(args: list[str], cwd: str) -> dict | None:
    """The Install page's properties for the first argument that is not an option, or None when there is none."""
    from cygnus.gui import launch, service

    files, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-") and a.lstrip("-") in QT_VALUE_OPTIONS:
            skip = True
        elif not a.startswith("-"):
            files.append(a)
    if not files:
        return None
    result = launch.resolve(files[0], cwd, service.configured_flatpak_remotes)
    return {**result.properties, "launchNotice": result.notice}


BUSY_MESSAGE = "Cygnus is busy with something else. Open the link or file again when it has finished."


def deliver(args: list[str], cwd: str, *, busy, navigate, notify, show) -> None:
    """Act on what a launch (a first one, or one handed over by a later one) asked for. A page that is in the middle of
    something is never replaced: the person is told instead, and the window is brought forward either way."""
    properties = page_request(args, cwd)
    if properties is not None:
        if busy():
            notify(BUSY_MESSAGE)
        else:
            navigate(properties)
    show()


def parse_handover(payload: str) -> tuple[list[str], str] | None:
    """What a later launch handed over: its arguments and folder, or None when it is not what a launch sends."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    args = data.get("args", []) if isinstance(data, dict) else None
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or len(args) > 64:
        return None
    return args, cwd if isinstance(cwd, str) and cwd else os.getcwd()


class Backlog:
    """Launches that arrive before the window has finished loading (a second click on a Flathub button a moment after the
    first) are held and acted on, in order, as soon as it has, instead of being lost."""

    def __init__(self, deliver) -> None:
        self._deliver, self._ready, self._held = deliver, False, []

    def add(self, args: list[str], cwd: str) -> None:
        if self._ready:
            self._deliver(args, cwd)
        else:
            self._held.append((args, cwd))

    def release(self) -> None:
        self._ready = True
        held, self._held = self._held, []
        for args, cwd in held:
            self._deliver(args, cwd)


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")
    from PySide6.QtCore import Q_ARG, QMetaObject, QSize, QUrl
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression

    from cygnus.gui import instance
    from cygnus.gui.bridge import Backend

    argv = argv if argv is not None else sys.argv
    app = QGuiApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationDomain("omranabdulaziz.github.io")
    app.setDesktopFileName(APP_ID)

    receiver = instance.Receiver()
    if not instance.listen(receiver):  # another window is open: give it the arguments and leave
        if instance.forward([a for a in argv[1:]]):
            return 0

    icons = Path(__file__).with_name("icons")
    fallback = QIcon(str(icons / f"{APP_ID}.svg"))
    fallback.addFile(str(icons / f"{APP_ID}-small.svg"), QSize(16, 16))
    fallback.addFile(str(icons / f"{APP_ID}-small.svg"), QSize(32, 32))
    app.setWindowIcon(QIcon.fromTheme(APP_ID, fallback))

    engine = QQmlApplicationEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)
    component = QQmlComponent(engine)
    holder: dict[str, object] = {}

    def show(window) -> None:
        for method in ("show", "raise", "requestActivate"):
            QMetaObject.invokeMethod(window, method)

    def busy(window) -> bool:
        value, undefined = QQmlExpression(QQmlEngine.contextForObject(window), window, "pageBusy()").evaluate()
        return bool(value) and not undefined

    def open_request(args: list[str], cwd: str) -> None:
        window = holder.get("window")
        if window is None:
            return
        deliver(args, cwd, busy=lambda: busy(window), show=lambda: show(window),
                navigate=lambda p: QMetaObject.invokeMethod(window, "openPage", Q_ARG("QVariant", "install"),
                                                            Q_ARG("QVariant", p)),
                notify=lambda text: QMetaObject.invokeMethod(window, "notify", Q_ARG("QVariant", text)))

    backlog = Backlog(open_request)

    def on_forwarded(payload: str) -> None:  # a later launch handed its arguments over
        handed = parse_handover(payload)
        if handed is not None:
            backlog.add(*handed)

    receiver.opened.connect(on_forwarded)

    def on_status(status) -> None:
        if status == QQmlComponent.Status.Ready:
            holder["window"] = component.create()
            open_request(argv[1:], os.getcwd())  # "Open with Cygnus", a Flathub link, or `cygnus-gui FILE`
            backlog.release()  # ...then whatever other launches handed over while the window was loading
        elif status == QQmlComponent.Status.Error:
            for err in component.errors():
                print(err.toString(), file=sys.stderr)
            app.exit(1)

    component.statusChanged.connect(on_status)
    component.loadUrl(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")), QQmlComponent.CompilationMode.Asynchronous)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
