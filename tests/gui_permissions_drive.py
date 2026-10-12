"""Drive the real application page offscreen with the backend faked: a Flatpak's page has "Permissions…", the dialog lists them by group with the
worst marked, says what you changed, and a program that is not a Flatpak has no such button. Prints one JSON line (see test_gui_permissions.py)."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

import shiboken6  # noqa: E402
from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402

from cygnus.core import flatpak_permissions as fp  # noqa: E402
from cygnus.gui import service  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

META = """[Context]
shared=network;
sockets=wayland;x11;
filesystems=host;xdg-download:ro;
[Session Bus Policy]
org.freedesktop.Flatpak=talk
"""
OVERRIDE = "[Context]\nsockets=!x11\nfilesystems=xdg-pictures\n"
merged = fp.merge([("app", fp.parse_keyfile(META)), ("you", fp.parse_keyfile(OVERRIDE))])
items = fp.describe(merged)
RESULT = {"app": "com.example.App", "name": "Example", "origin": "flathub", "items": items, "summary": fp.summary(items), "removed": fp.removed_text(merged),
          "changed_by_you": True, "notes": [], "groups": [{"id": g, "title": t} for g, t in fp.GROUPS if any(i["group"] == g for i in items)]}
log: dict = {"asked": [], "snaps": {}}


def fake(installation_id):
    log["asked"].append(installation_id)
    return RESULT


service.flatpak_permissions = fake
service.check_app = lambda query, registry_path=None: {"known": False, "name": query}
service.default_browser_info = lambda installation_id: {"browser": False}

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


def _as_item(obj):
    return shiboken6.wrapInstance(shiboken6.getCppPointer(obj)[0], QQuickItem)


def found(name):
    """Items with this object name on the page and in the open dialog (a dialog's content lives in an overlay, outside the page row)."""
    roots = [_as_item(js("pageStack"))]
    dialog = state["window"].findChild(QObject, "permissionsDialog")
    if dialog is not None and dialog.property("contentItem") is not None:
        roots.append(_as_item(dialog.property("contentItem")))
    return [i for root in roots for i in _items(root) if i.objectName() == name]


def snap(label):
    def go():
        action = state["window"].findChild(QObject, "permissionsAction")
        dialog = state["window"].findChild(QObject, "permissionsDialog")
        summary = [i.property("text") for i in found("permissionsSummary")]
        log["snaps"][label] = {
            "action": None if action is None else {"visible": bool(action.property("visible")), "enabled": bool(action.property("enabled"))},
            "dialog_open": None if dialog is None else bool(dialog.property("visible")),
            "summary": summary[0] if summary else None,
            "headings": [i.property("text") for i in found("permissionHeading")],
            "rows": [i.property("text") for i in found("permissionItem")],
            "busy": js("pageStack.currentItem.busyText"),
        }
    return go


def open_app(fmt, iid):
    return lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", "app"),
                                            Q_ARG("QVariant", {"appName": "Example", "installationId": iid, "installationFormat": fmt}))


def press():
    action = state["window"].findChild(QObject, "permissionsAction")
    if action is not None:
        QMetaObject.invokeMethod(action, "trigger")


STEPS = [
    (open_app("flatpak", "inst-flatpak"), 2500),
    (snap("before"), 10),
    (press, 2000),
    (snap("open"), 10),
    (lambda: QMetaObject.invokeMethod(state["window"].findChild(QObject, "permissionsDialog"), "close"), 500),
    (open_app("appimage", "inst-appimage"), 2500),
    (snap("appimage"), 10),
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
