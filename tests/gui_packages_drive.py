"""Drive the real Packages screens in an offscreen window with the backend's answers faked (no pacman, no helper): search while typing,
a late answer to an older search, and Install / Remove from a package's page through the confirmation window. Prints one JSON line.
Run by test_gui_packages.py in a subprocess, so a QML crash or hang cannot take the test session down."""

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

from cygnus.gui import fixes, service  # noqa: E402
from cygnus.gui.app import QML_DIR  # noqa: E402
from cygnus.gui.bridge import Backend  # noqa: E402

log: dict = {"searches": [], "plans": [], "commits": [], "enabled": []}


def hit(name):
    return {"name": name, "version": "1-1", "repo": "extra", "desc": f"The {name} tool", "installed_version": None, "reason": None, "update": False}


def fake_search(text):
    log["searches"].append(text)
    if text == "slow":
        time.sleep(1.5)  # answered after the newer search below has been
    return {"terms": [text], "results": [hit(f"{text}-result")], "total": 1, "truncated": False, "lists": {"refreshed": None}}


def fake_details(name, repo=None):
    installed = name == "cowsay"
    return {"found": True, "name": name, "version": "1-1", "arch": "x86_64", "desc": "x", "url": "", "licenses": [], "groups": [], "packager": "",
            "build_date": 0, "repo": "extra", "package_size": 0, "installed_size": 0, "depends": [], "depends_more": 0, "optdepends": [],
            "optdepends_more": 0, "installed": installed, "installed_version": "1-1" if installed else None, "provides": [], "conflicts": [],
            "replaces": [], "depends_missing": 0, "protected": False, "blocked": {"install": "It is already installed." if installed else None,
                                                                                    "remove": None if installed else "It is not installed."},
            "required_by": [], "required_by_more": 0, "optional_for": [], "optional_for_more": 0, "files": [], "files_total": 0,
            "has_install_script": False, "reason": "explicit", "install_date": 0, "managed_as": None, "lists": {}}


def plan(verb, names):
    return {"kind": "helper", "token": f"tok-{verb}", "title": f"{verb} {names[0]}", "message": f"{verb} {names[0]}.", "security_note": "",
            "dialog_title": f"{verb} packages", "action": verb, "upgrades": [], "installs": [], "removals": [], "download_bytes": 0}


def fake_install(names):
    log["plans"].append(["install", list(names)])
    return plan("Install", names)


def fake_remove(names, confirm=False):
    log["plans"].append(["remove", list(names), confirm])
    if not confirm:
        return {"refused": "not_installed_by_cygnus", "text": "Cygnus did not install this package.", "names": list(names)}
    return {**plan("Remove", names), **({"token": "tok-Remove-orphans"} if "orph1" in names else {})}


def fake_older(name):
    log.setdefault("older_asked", []).append(name)
    return {"installed_version": "1-1", "versions": [{"version": "0.9-1", "arch": "x86_64", "size": 1024, "modified": 1700000000},
                                                     {"version": "0.8-1", "arch": "x86_64", "size": 1024, "modified": 1690000000}]
            if name == "cowsay" else []}


def fake_downgrade(name, version):
    log["plans"].append(["downgrade", name, version])
    return {**plan("Downgrade", [name]), "token": f"tok-Downgrade-{version}", "action": "Install the older version",
            "dialog_title": "Install an older version", "downgrade": {"name": name, "from": "1-1", "to": version}}


service.packages_search, service.package_details, service.package_older_versions = fake_search, fake_details, fake_older
fixes.plan_downgrade = fake_downgrade
fixes.plan_install_packages, fixes.plan_remove_by_name = fake_install, fake_remove
def fake_commit(token, progress):
    if token == "tok-Remove-orphans":
        progress("removing packages…")
        progress("")  # pacman prints blank lines: that must not end "busy"
        progress("   ")
        time.sleep(1.5)
    if token.endswith("-slow"):
        time.sleep(1.5)  # a commit that is still running when the person leaves the page
    log["commits"].append(token)
    return {"ok": True}


def fake_installed():
    gone = "tok-Downgrade-0.1-slow" in log["commits"]  # what the slow commit "removes"
    names = ["other"] if gone else ["cowsay", "other"]
    rows = [{"name": n, "version": "1-1", "desc": "", "reason": "explicit", "size": 1, "installed": 1, "repo": "extra", "update": False} for n in names]
    if log.get("orphans") and "tok-Remove-orphans" not in log["commits"]:
        rows += [{"name": n, "version": "1-1", "desc": "", "reason": "dependency", "size": 1, "installed": 1, "repo": "extra", "update": False,
                  "unneeded": unneeded, "protected": protected, "managed": managed}
                 for n, unneeded, protected, managed in (("orph1", True, False, False), ("orph2", True, False, False), ("needed", False, False, False),
                                                         ("firmware-dep", True, True, False), ("app-dep", True, False, True))]
    return {"packages": rows, "updates": 0, "lists": {"refreshed": None}}


def fake_cache_preview(keep, uninstalled):
    log.setdefault("previews", []).append([keep, uninstalled])
    files = {0: 10, 1: 7, 2: 4}.get(keep, 1)
    return {"files": files, "bytes": files * 2**20, "packages": files // 2 + 1, "looked_at": 100, "examples": ["foo 1.0-1"], "folders": ["/var/cache/pacman/pkg"],
            "problems": [], "keep": keep, "uninstalled": uninstalled}


def fake_cache_plan(keep, uninstalled):
    log["plans"].append(["cache", keep, uninstalled])
    return {**plan("Clean", ["x"]), "token": "tok-Cache", "action": "Delete the files", "dialog_title": "Clean the package cache",
            "cache": {"files": 4, "bytes": 4 * 2**20, "packages": 3, "keep": keep, "uninstalled": uninstalled, "folders": ["/var/cache/pacman/pkg"],
                      "examples": ["foo 1.0-1"], "problems": []}}


fixes.commit = fake_commit
service.packages_installed = fake_installed
service.cache_preview = fake_cache_preview
fixes.plan_cache_clean = fake_cache_plan

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


def page(name, props=None):
    return lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", name), Q_ARG("QVariant", props or {}))


def trigger(name):
    def go():
        action = state["window"].findChild(QObject, name)
        log["enabled"].append([name, None if action is None else action.property("enabled")])
        if action is not None:
            QMetaObject.invokeMethod(action, "trigger")
    return go


def record(key):
    return lambda: log.__setitem__(key, js("JSON.stringify(pageStack.currentItem.results.map(r => r.name))"))


STEPS = [
    (page("packages"), 2000),
    (lambda: js("pageStack.currentItem.query = 'fi'"), 100),
    (lambda: js("pageStack.currentItem.query = 'fir'"), 100),
    (lambda: js("pageStack.currentItem.query = 'fire'"), 1500),  # typed quickly: only the last text is searched
    (record("after_typing"), 10),
    (lambda: js("pageStack.currentItem.query = 'slow'"), 700),  # asked, and answered only after the next one
    (lambda: js("pageStack.currentItem.query = 'fast'"), 3500),  # the slow answer arrives meanwhile and must be dropped
    (record("after_late_answer"), 10),
    (page("package", {"packageName": "sl"}), 2000),
    (lambda: js("pageStack.currentItem.planInstall()"), 1500),
    (trigger("confirmAction"), 1500),
    (page("package", {"packageName": "cowsay"}), 2000),
    (lambda: js("pageStack.currentItem.planRemove(false)"), 1500),
    (trigger("warnAction"), 800),  # the box is not ticked: nothing happens
    (lambda: js("pageStack.currentItem.warnTicked = true"), 500),
    (trigger("warnAction"), 1500),  # ticked: planned again with the confirmation
    (trigger("confirmAction"), 1500),
    (lambda: log.__setitem__("older_shown", js("JSON.stringify(pageStack.currentItem.older.map(v => v.version))")), 10),
    (lambda: js("pageStack.currentItem.planDowngrade('0.9-1')"), 1500),
    (trigger("confirmAction"), 1500),
    (page("package", {"packageName": "sl"}), 2000),  # not installed: no older versions are looked for
    (lambda: log.__setitem__("older_shown_sl", js("JSON.stringify(pageStack.currentItem.older)")), 10),
    # leaving a package's page while its change is still being made: the list below it must still show the result
    (page("packages"), 2000),
    (lambda: log.__setitem__("list_before", js("JSON.stringify(pageStack.currentItem.installedAll.map(p => p.name))")), 10),
    (lambda: js("pageStack.push(packagePage, {packageName: 'cowsay'})"), 2000),
    (lambda: js("pageStack.currentItem.planDowngrade('0.1-slow')"), 1500),
    (trigger("confirmAction"), 300),
    (lambda: js("pageStack.pop()"), 4000),  # the page is gone before the commit ends; the commit ends meanwhile
    (lambda: log.__setitem__("list_after", js("JSON.stringify(pageStack.currentItem.installedAll.map(p => p.name))")), 10),
    (lambda: log.__setitem__("title_after", js("pageStack.currentItem.title")), 10),
    # everything no longer needed: ticked, asked about on purpose, removed in one plan
    (lambda: log.__setitem__("orphans", True), 10),
    (page("packages"), 2000),
    (lambda: js("pageStack.currentItem.mode = 'installed'; pageStack.currentItem.filterIndex = 3"), 800),
    (lambda: log.__setitem__("orphans_shown", js("JSON.stringify(pageStack.currentItem.shown.map(p => p.name))")), 10),
    (lambda: log.__setitem__("picked_before", js("JSON.stringify(pageStack.currentItem.pickedNames())")), 10),
    (lambda: js("pageStack.currentItem.pickAll()"), 300),
    (lambda: log.__setitem__("picked_all", js("JSON.stringify(pageStack.currentItem.pickedNames())")), 10),
    (lambda: js("pageStack.currentItem.planRemoveSelected(false)"), 1500),
    (trigger("listWarnAction"), 800),
    (lambda: js("pageStack.currentItem.warnTicked = true"), 500),
    (trigger("listWarnAction"), 1500),
    (trigger("listConfirmAction"), 800),
    (lambda: log.__setitem__("busy_mid", js("pageStack.currentItem.busyText")), 10),  # the commit is still running, and blank lines came in
    (lambda: None, 3000),
    (lambda: log.__setitem__("picked_after", js("JSON.stringify(pageStack.currentItem.pickedNames())")), 10),
    (lambda: log.__setitem__("orphans_after", js("JSON.stringify(pageStack.currentItem.installedAll.map(p => p.name))")), 10),
    # cleaning the package cache
    (page("packages"), 2000),
    (lambda: js("pageStack.currentItem.openCache()"), 2000),
    (lambda: log.__setitem__("cache_first", js("pageStack.currentItem.cacheShown && pageStack.currentItem.cacheShown.files")), 10),
    (lambda: [QMetaObject.invokeMethod(state["window"].findChild(QObject, "cacheKeep"), "decrease") for _ in range(2)], 2000),
    (lambda: log.__setitem__("cache_then", js("pageStack.currentItem.cacheShown && pageStack.currentItem.cacheShown.files")), 10),
    (trigger("cacheAction"), 1500),
    (trigger("listConfirmAction"), 2000),
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
