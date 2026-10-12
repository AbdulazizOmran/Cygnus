"""Drive the real Settings page offscreen with the tray backend faked (nothing is started or written): the switch shows what is set, switching on and off
calls the backend once each, and a failure puts the switch back to what is really so and says why. Prints one JSON line (see test_gui_tray.py)."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

import shiboken6  # noqa: E402
from PySide6.QtCore import Q_ARG, QMetaObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402

from cygnus.core import tray  # noqa: E402
from cygnus.core.errors import CygnusError  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

log: dict = {"calls": [], "snaps": {}}
world = {"on": False, "fail": None}


def status():
    return {"enabled": world["on"], "entry": world["on"], "why": ""}


def enable(**kw):
    log["calls"].append("enable")
    if world["fail"]:
        raise CygnusError(world["fail"])
    world["on"] = True
    return status()


def disable():
    log["calls"].append("disable")
    if world["fail"]:
        raise CygnusError(world["fail"])
    world["on"] = False
    return status()


tray.status, tray.enable, tray.disable = status, enable, disable

app = QGuiApplication(sys.argv[:1])
engine = QQmlApplicationEngine()
backend = Backend()
engine.rootContext().setContextProperty("backend", backend)
component = QQmlComponent(engine)
state: dict = {}


def js(expression):
    window = state["window"]
    value, undefined = QQmlExpression(QQmlEngine.contextForObject(window), window, expression).evaluate()
    return None if undefined else value


def _items(item):
    yield item
    for child in item.childItems():
        yield from _items(child)


def found(name):
    root = shiboken6.wrapInstance(shiboken6.getCppPointer(js("pageStack"))[0], QQuickItem)
    return [i for i in _items(root) if i.objectName() == name]


def snap(label):
    def go():
        switch, note = found("traySwitch"), found("trayNote")
        log["snaps"][label] = {"checked": switch[0].property("checked") if switch else None, "enabled": switch[0].property("enabled") if switch else None,
                               "note": note[0].property("description") if note else None, "world": world["on"]}
    return go


def toggle():
    switch = found("traySwitch")
    if switch:
        QMetaObject.invokeMethod(switch[0], "click")


def failing(message):
    def go():
        world["fail"] = message
    return go


STEPS = [
    (lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", "settings"), Q_ARG("QVariant", {})), 3000),
    (snap("start"), 10),
    (toggle, 1500),  # on
    (snap("on"), 10),
    (failing("The start-up entry could not be written (disk full)."), 10),
    (toggle, 2000),  # off, but it fails: the switch goes back to what is really so
    (snap("failed"), 10),
]


def run(i=0):
    if i >= len(STEPS):
        print(json.dumps(log), flush=True)
        app.quit()
        return
    fn, delay = STEPS[i]
    fn()
    QTimer.singleShot(delay, lambda: run(i + 1))


def on_status(status_):
    if status_ == QQmlComponent.Status.Ready:
        state["window"] = component.create()
        QTimer.singleShot(1500, run)
    elif status_ == QQmlComponent.Status.Error:
        print([e.toString() for e in component.errors()], flush=True)
        app.exit(1)


component.statusChanged.connect(on_status)
component.loadUrl(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")), QQmlComponent.CompilationMode.Asynchronous)
QTimer.singleShot(90000, lambda: app.exit(3))
sys.exit(app.exec())
