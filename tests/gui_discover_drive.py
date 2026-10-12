"""Drive the real Discover pages offscreen with the backend faked (no network, no libflatpak): the list fills, typing waits before searching,
the filters and "show more" ask for the right page, opening a program shows its ways to get it, and each way hands over to the page that
already does that job without installing anything. Prints one JSON line (see test_gui_discover.py)."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
import shiboken6  # noqa: E402

from cygnus.core.errors import CygnusError  # noqa: E402
from cygnus.gui import service  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

log: dict = {"searches": [], "fetches": [], "snaps": {}}


def flatpak(remote="flathub", app="org.mozilla.firefox", installed=False):
    return {"kind": "flatpak", "remote": remote, "id": app, "label": f"{remote} (Flatpak)", "installed": installed}


def entry(key, name, sources, groups=("internet",), **over):
    base = {"key": key, "name": name, "summary": f"{name} does things", "developer": "", "categories": [], "keywords": [], "url": "", "description": f"About {name}.",
            "icon_url": "", "icon_name": "package-x-generic", "groups": list(groups), "sources": sources,
            "installed": any(s["installed"] for s in sources)}
    return {**base, **over}


FIREFOX = entry("org.mozilla.firefox", "Firefox", [flatpak(), {"kind": "repo", "package": "firefox", "label": "Your repositories (pacman)", "installed": False}],
                ("browsers",), url="https://www.firefox.com/", developer="Mozilla")
CHROME = entry("com.google.Chrome", "Google Chrome", [flatpak(app="com.google.Chrome"), {"kind": "vendor", "package": "google-chrome-stable", "formats": ["deb", "rpm"],
                                                                                              "label": "The vendor's own package", "note": "", "installed": False}], ("browsers",))
EDGE = entry("vendor:microsoft-edge-stable", "Microsoft Edge", [{"kind": "vendor", "package": "microsoft-edge-stable", "formats": ["deb"],
                                                                  "label": "The vendor's own package", "note": "", "installed": True}], ("browsers",))
MANY = [entry(f"org.example.App{i:02}", f"App {i:02}", [flatpak(app=f"org.example.App{i:02}")]) for i in range(90)]
ALL = [FIREFOX, CHROME, EDGE, *MANY]
GROUPS = [{"id": "browsers", "name": "Web browsers", "count": 3}, {"id": "internet", "name": "Internet", "count": 90}]


def fake_search(query="", group="", kind="", offset=0):
    log["searches"].append([query, group, kind, offset])
    pool = [e for e in ALL if (not group or group in e["groups"]) and (not kind or any(s["kind"] == kind for s in e["sources"]))
            and (not query or query.lower() in e["name"].lower())]
    return {"total": len(pool), "offset": offset, "entries": pool[offset:offset + 40], "groups": GROUPS}


def fake_fetch(package, fmt, progress):
    log["fetches"].append([package, fmt])
    progress("Downloading…")
    return {"ok": True, "file_url": f"file:///nonexistent/{package}.{fmt}", "version": "9.9", "format": fmt, "checked": fmt == "deb", "package": package}


def refuse(*_a, **_k):
    raise CygnusError("the fake backend analyses nothing")


service.discover_overview = lambda: {"total": len(ALL), "sources": [{"kind": "flatpak", "label": "flathub", "updated": time.time() - 3 * 86400}],
                                     "problems": [], "repo_missing": True, "vendors": 2}
service.discover_search = fake_search
service.discover_app = lambda key: next(e for e in ALL if e["key"] == key)
service.fetch_vendor_package = fake_fetch
service.analyse_flatpak_ref = refuse  # the Install page is only opened, not driven
service.analyse_file = refuse

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


def children(name):
    """Every item with this object name, delegates of a Repeater or a ListView included (findChildren does not see those)."""
    stack = js("pageStack")  # the page row: everything on screen is below it
    root_item = shiboken6.wrapInstance(shiboken6.getCppPointer(stack)[0], QQuickItem)
    return [i for i in _items(root_item) if i.objectName() == name]


def click(name, index=0, only_visible=True):
    def go():
        items = [c for c in children(name) if not only_visible or c.property("visible")]
        if len(items) > index:
            QMetaObject.invokeMethod(items[index], "click")
    return go


def snap(label, *expressions):
    def go():
        log["snaps"][label] = {
            "title": js("pageStack.currentItem.title"),
            "values": [js(e) for e in expressions],
            "visible": {n: [bool(c.property("visible")) for c in children(n)] for n in ("repoMissing", "moreButton", "flatpakButton", "repoButton", "debButton", "rpmButton", "websiteButton")},
            "page": js("pageStack.currentItem.objectName || pageStack.currentItem.toString().split('_')[0]"),
            "count_text": [i.property("text") for i in children("resultCount")],
        }
    return go


def open_page(name, props):
    return lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", name), Q_ARG("QVariant", props))


STEPS = [
    (open_page("discover", {}), 2500),
    (snap("start", "pageStack.currentItem.results.length", "pageStack.currentItem.total", "pageStack.currentItem.groups.length",
          "pageStack.currentItem.overview.repo_missing"), 10),
    (lambda: js("pageStack.currentItem.query = 'f'"), 80),  # typed letter by letter: nothing is asked until the typing stops
    (lambda: js("pageStack.currentItem.query = 'fi'"), 80),
    (lambda: js("pageStack.currentItem.query = 'fir'"), 80),
    (lambda: js("pageStack.currentItem.query = 'fire'"), 1500),
    (snap("typed", "pageStack.currentItem.results.length"), 10),
    (lambda: js("pageStack.currentItem.query = ''"), 1500),
    (click("group-browsers"), 1000),
    (snap("group", "pageStack.currentItem.group", "pageStack.currentItem.results.length"), 10),
    (click("group-browsers"), 1000),  # the chosen group chosen again: cleared
    (snap("group-cleared", "pageStack.currentItem.group", "pageStack.currentItem.results.length"), 10),
    (lambda: (js("pageStack.currentItem.kind = 'vendor'"), js("pageStack.currentItem.search(false)")), 1000),
    (snap("vendor-only", "pageStack.currentItem.results.length"), 10),
    (lambda: (js("pageStack.currentItem.kind = ''"), js("pageStack.currentItem.search(false)")), 1000),
    (click("moreButton"), 1000),
    (snap("more", "pageStack.currentItem.results.length", "pageStack.currentItem.total"), 10),
    (lambda: js("pageStack.currentItem.query = 'zzz'"), 10),  # a new query is waiting its turn...
    (click("moreButton"), 1500),  # ...and "Show more" is pressed: it must go on with the list that is on screen, not with "zzz"
    (snap("mixed", "pageStack.currentItem.results.length"), 10),
    (lambda: js("pageStack.currentItem.query = ''"), 1500),
    (lambda: js("pageStack.currentItem.open(pageStack.currentItem.results[0])"), 2000),  # Firefox: Flatpak and repository
    (snap("firefox", "pageStack.currentItem.entry.name", "pageStack.currentItem.entry.sources.length"), 10),
    (click("flatpakButton"), 1500),
    (snap("flatpak-handover", "pageStack.currentItem.appSpec"), 10),
    (open_page("discoverApp", {"entryKey": "org.mozilla.firefox"}), 2000),
    (click("repoButton"), 1500),
    (snap("repo-handover", "pageStack.currentItem.packageName"), 10),
    (open_page("discoverApp", {"entryKey": "com.google.Chrome"}), 2000),
    (snap("chrome", "pageStack.currentItem.entry.name"), 10),
    (click("debButton"), 1500),
    (snap("deb-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
    (open_page("discoverApp", {"entryKey": "com.google.Chrome"}), 2000),
    (click("rpmButton"), 1500),
    (snap("rpm-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
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
QTimer.singleShot(120000, lambda: app.exit(3))
sys.exit(app.exec())
