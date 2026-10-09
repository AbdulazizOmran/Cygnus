"""Load the real QML UI offscreen, open every page (and the install flow for a file), and print a
JSON report on the last line. Run by test_gui_smoke.py in a subprocess so a QML crash or hang
cannot take the test session down; never shows a window."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

from PySide6.QtCore import Q_ARG, QMetaObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402

from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

FILE = QUrl.fromLocalFile(sys.argv[1]).toString() if len(sys.argv) > 1 else ""
PAGES = [("apps", {}), ("app", {"appName": "Hello World"}), ("updates", {}), ("storage", {}), ("settings", {}),
         ("install", {"fileUrl": FILE} if FILE else {})]

app = QGuiApplication(sys.argv[:1])
engine = QQmlApplicationEngine()
warnings: list[str] = []
engine.warnings.connect(lambda ws: warnings.extend(w.toString() for w in ws))
backend = Backend()
engine.rootContext().setContextProperty("backend", backend)
component = QQmlComponent(engine)
report: dict = {"window": False, "pages": {}, "errors": []}
state: dict = {}


def js(expression: str):
    """Evaluate in the window's QML context (Kirigami's QML types cannot cross into Python)."""
    window = state["window"]
    value, undefined = QQmlExpression(QQmlEngine.contextForObject(window), window, expression).evaluate()
    return None if undefined else value


def current_title() -> str:
    return js("pageStack.currentItem ? pageStack.currentItem.title : ''")


def step(i: int = 0) -> None:
    if i < len(PAGES):
        name, props = PAGES[i]
        QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", name), Q_ARG("QVariant", props))
        QTimer.singleShot(1500, lambda: (report["pages"].__setitem__(name, current_title()), step(i + 1)))
        return
    report["install_plan"] = json.loads(js("JSON.stringify(pageStack.currentItem.plan)") or "null")
    report["warnings"] = list(warnings)  # before teardown: only what a user could have seen
    print(json.dumps(report, default=str), flush=True)
    app.quit()


def on_status(status) -> None:
    if status == QQmlComponent.Status.Ready:
        state["window"] = component.create()
        report["window"] = state["window"] is not None
        if not report["window"]:
            report["errors"] = [e.toString() for e in component.errors()]
            print(json.dumps(report), flush=True)
            app.exit(1)
            return
        QTimer.singleShot(1000, step)
    elif status == QQmlComponent.Status.Error:
        report["errors"] = [e.toString() for e in component.errors()]
        print(json.dumps(report), flush=True)
        app.exit(1)


component.statusChanged.connect(on_status)
component.loadUrl(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")), QQmlComponent.CompilationMode.Asynchronous)
QTimer.singleShot(60000, lambda: app.exit(3))
sys.exit(app.exec())
