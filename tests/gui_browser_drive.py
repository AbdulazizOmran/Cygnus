"""Drive the real application page offscreen with the backend faked: a web browser gets "Make default browser…", the dialog says what
will happen, the choice goes through and the page then offers to put it back; a browser that cannot be chosen explains why and the
button stays off; a program that is no browser shows nothing. Prints one JSON line (see test_gui_browser.py)."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402

from cygnus.core import defaultbrowser  # noqa: E402
from cygnus.gui import service  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

log: dict = {"calls": [], "snaps": {}}
INFO = {"browser": True, "desktop_id": "helium.desktop", "name": "Helium", "can": True, "blockers": [], "notes": ["It is stored on a drive that can be unplugged."],
        "repairable": False, "is_default": False, "by_accident": False, "remembered": False, "current_name": "Firefox"}
info = dict(INFO)


def fake_enable(desktop_id, run=None):
    log["calls"].append(["enable", desktop_id])
    info.update(is_default=True, remembered=True)
    return {"changed": True}


def fake_restore(run=None):
    log["calls"].append(["restore"])
    info.update(is_default=False, remembered=False)
    return {"left_alone": []}


defaultbrowser.enable = fake_enable
defaultbrowser.restore = fake_restore
service.default_browser_info = lambda installation_id: dict(info)
service.check_app = lambda query, registry_path=None: {"known": False, "name": query}

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


def child(name):
    return state["window"].findChild(QObject, name)


def snap(label):
    def go():
        action, dialog, button = child("browserAction"), child("browserDialog"), child("browserDialogAction")
        log["snaps"][label] = {
            "action": None if action is None else {"visible": action.property("visible"), "text": action.property("text"), "enabled": action.property("enabled")},
            "dialog": None if dialog is None else {"open": dialog.property("visible"), "title": dialog.property("title"), "subtitle": dialog.property("subtitle")},
            "button": None if button is None else {"text": button.property("text"), "enabled": button.property("enabled")},
            "message": None if child("pageMessage") is None else {"visible": child("pageMessage").property("visible"), "text": child("pageMessage").property("text")},
            "is_default": js("pageStack.currentItem.browserInfo ? pageStack.currentItem.browserInfo.is_default : null"),
            "status_line": js("pageStack.currentItem.browserInfo ? 'info' : 'none'"),
            "busy": js("pageStack.currentItem.busyText"),
        }
    return go


def press(name):
    def go():
        item = child(name)
        if item is not None:
            QMetaObject.invokeMethod(item, "trigger")
    return go


def reload(**changes):
    def go():
        info.clear()
        info.update(INFO)
        info.update(changes)
        js("pageStack.currentItem.loadBrowser()")
    return go


def close_dialog():
    dialog = child("browserDialog")
    if dialog is not None:
        QMetaObject.invokeMethod(dialog, "close")


STEPS = [
    (lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", "app"),
                                      Q_ARG("QVariant", {"appName": "Helium", "installationId": "inst-1", "installationFormat": "appimage"})), 2500),
    (snap("start"), 10),
    (press("browserAction"), 1000),
    (snap("asking"), 10),
    (press("browserDialogAction"), 2000),  # chosen
    (snap("chosen"), 10),
    (press("browserAction"), 1000),  # now it offers to put the old one back
    (snap("putting-back"), 10),
    (press("browserDialogAction"), 2000),
    (snap("put-back"), 10),
    (reload(can=False, blockers=["Its menu entry does not say it opens https links, so the desktop would ignore the choice."]), 800),
    (press("browserAction"), 1000),
    (snap("blocked"), 10),
    (close_dialog, 500),
    (reload(is_default=False, by_accident=True, remembered=False, notes=[]), 800),
    (snap("by-accident"), 10),
    (reload(browser=False), 800),
    (snap("not-a-browser"), 10),
]


def run(i=0):
    if i >= len(STEPS):
        print(json.dumps(log), flush=True)
        app.quit()
        return
    fn, delay = STEPS[i]
    fn()
    QTimer.singleShot(delay, lambda: run(i + 1))


def on_status(status):
    if status == QQmlComponent.Status.Ready:
        state["window"] = component.create()
        QTimer.singleShot(1500, run)
    elif status == QQmlComponent.Status.Error:
        print([e.toString() for e in component.errors()], flush=True)
        app.exit(1)


component.statusChanged.connect(on_status)
component.loadUrl(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")), QQmlComponent.CompilationMode.Asynchronous)
QTimer.singleShot(90000, lambda: app.exit(3))
sys.exit(app.exec())
