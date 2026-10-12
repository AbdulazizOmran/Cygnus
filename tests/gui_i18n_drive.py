"""Start the real window offscreen with a translation installed the way the program does it (cygnus/gui/i18n.py) and print what the menu says.
Prints one JSON line (see test_i18n.py). The language and the folder come from CYGNUS_LANGUAGE and CYGNUS_TRANSLATIONS."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402

from cygnus.gui import i18n  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

app = QGuiApplication(sys.argv[:1])
used = i18n.install(app)
engine = QQmlApplicationEngine()
backend = Backend()
engine.rootContext().setContextProperty("backend", backend)
component = QQmlComponent(engine)
state: dict = {}


def js(expression):
    window = state["window"]
    value, undefined = QQmlExpression(QQmlEngine.contextForObject(window), window, expression).evaluate()
    return None if undefined else value


def report():
    print(json.dumps({"used": None if used is None else used.name, "drawer": json.loads(js("JSON.stringify(globalDrawer.actions.map(a => a.text))")),
                      "page": js("pageStack.currentItem.title")}), flush=True)
    app.quit()


def on_status(status):
    if status == QQmlComponent.Status.Ready:
        state["window"] = component.create()
        QTimer.singleShot(2500, report)
    elif status == QQmlComponent.Status.Error:
        print([e.toString() for e in component.errors()], flush=True)
        app.exit(1)


component.statusChanged.connect(on_status)
component.loadUrl(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")), QQmlComponent.CompilationMode.Asynchronous)
QTimer.singleShot(60000, lambda: app.exit(3))
sys.exit(app.exec())
