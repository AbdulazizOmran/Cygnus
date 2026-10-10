"""Drive the real Updates dialog offscreen with the backend faked: when the Arch news says a manual step is needed, Update stays
disabled until the box is ticked, and the box starts clear for the next plan. Prints one JSON line (see test_gui_updates.py)."""

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

from cygnus.gui import fixes, service  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

log: dict = {"commits": [], "states": [], "shown": []}
NEWS = {"manual": {"ok": True, "total": 1, "since": 0, "needs_ack": True, "checked_at": 1, "items": [
            {"title": "foo requires manual intervention", "link": "https://archlinux.org/news/foo/", "published": 1, "summary": "Do this first.", "manual": True}]},
        "plain": {"ok": True, "total": 1, "since": 0, "needs_ack": False, "checked_at": 1, "items": [
            {"title": "A new ISO", "link": "", "published": 1, "summary": "Nothing to do.", "manual": False}]}}
which = {"news": "manual"}


def fake_plan():
    return {"kind": "helper", "token": f"tok-{which['news']}", "title": "Update the system", "message": "Upgrade.", "security_note": "",
            "upgrades": [{"name": "foo", "from": "1", "to": "2"}], "installs": [], "removals": [], "download_bytes": 0, "news": NEWS[which["news"]]}


fixes.plan_system_upgrade = fake_plan
fixes.commit = lambda token, progress: (log["commits"].append(token), {"ok": True})[1]
service.check_updates = lambda progress: []

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


def trigger():
    action = state["window"].findChild(QObject, "updateAction")
    log["states"].append(None if action is None else action.property("enabled"))
    if action is not None:
        QMetaObject.invokeMethod(action, "trigger")


def plan(kind):
    def go():
        which["news"] = kind
        js("pageStack.currentItem.planUpgrade()")
    return go


def shown():
    log["shown"].append(js("pageStack.currentItem.newsRead"))


STEPS = [
    (lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", "updates"), Q_ARG("QVariant", {})), 2000),
    (plan("manual"), 2000),
    (trigger, 800),  # the box is not ticked: disabled, nothing is committed
    (lambda: js("pageStack.currentItem.runUpgrade()"), 800),  # the other way in is closed too
    (lambda: js("pageStack.currentItem.newsRead = true"), 400),
    (trigger, 1500),  # ticked: it goes
    (plan("plain"), 2000),
    (shown, 10),  # a new plan starts with the box clear
    (trigger, 1500),  # news that needs nothing: no box, it goes at once
    (plan("manual"), 2000),
    (shown, 10),  # clear again after a plan whose box had been ticked
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
