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

log: dict = {"searches": [], "fetches": [], "aur": [], "appimages": [], "debians": [], "fedoras": [], "refreshes": [], "switches": [], "decisions": [], "snaps": {}}


def flatpak(remote="flathub", app="org.mozilla.firefox", installed=False):
    return {"kind": "flatpak", "remote": remote, "id": app, "label": f"{remote} (Flatpak)", "installed": installed}


def entry(key, name, sources, groups=("internet",), **over):
    base = {"key": key, "name": name, "summary": f"{name} does things", "developer": "", "categories": [], "keywords": [], "url": "", "description": f"About {name}.",
            "icon_url": "", "icon_name": "package-x-generic", "groups": list(groups), "sources": sources,
            "installed": any(s["installed"] for s in sources), "installed_hint": any(s["installed"] for s in sources) and all(s.get("hint") for s in sources if s["installed"])}
    return {**base, **over}


FIREFOX = entry("org.mozilla.firefox", "Firefox", [flatpak(), {"kind": "repo", "package": "firefox", "label": "Your repositories (pacman)", "installed": False}],
                ("browsers",), url="https://www.firefox.com/", developer="Mozilla")
CHROME = entry("com.google.Chrome", "Google Chrome", [flatpak(app="com.google.Chrome"), {"kind": "vendor", "package": "google-chrome-stable", "formats": ["deb", "rpm"],
                                                                                              "label": "The vendor's own package", "note": "", "installed": False}], ("browsers",))
EDGE = entry("vendor:microsoft-edge-stable", "Microsoft Edge", [{"kind": "vendor", "package": "microsoft-edge-stable", "formats": ["deb"],
                                                                  "label": "The vendor's own package", "note": "", "installed": True}], ("browsers",))
RELEASE = {"kind": "release", "key": "obsidian", "formats": ["appimage", "deb", "archive"], "page": "https://github.com/obsidianmd/obsidian-releases/releases",
           "label": "The maker's own download (GitHub)", "installed": False}
OBSIDIAN = entry("md.obsidian.Obsidian", "Obsidian", [flatpak(app="md.obsidian.Obsidian"), RELEASE], ("office",))
HELIUM = entry("release:helium", "Helium", [{**RELEASE, "key": "helium", "formats": ["appimage", "deb", "archive"], "installed": True,
                                              "page": "https://github.com/imputnet/helium-linux/releases"}], ("browsers",))  # Cygnus already manages one
APPIMAGE = entry("appimage:owner/Tool", "Tool", [{"kind": "appimage", "repo": "owner/Tool", "formats": ["appimage"], "page": "https://github.com/owner/Tool/releases",
                                                   "label": "A community listing (AppImageHub), not checked by Cygnus", "installed": False}], ("utility",))
APPIMAGE_SEEN = entry("appimage:owner/Seen", "Seen", [{"kind": "appimage", "repo": "owner/Seen", "formats": ["appimage"], "page": "",
                                                        "label": "A community listing (AppImageHub), not checked by Cygnus", "installed": True, "hint": True}], ("utility",))
def debian(package, needs):
    return {"kind": "debian", "package": package, "version": "1.0-1", "size": 1000, "filename": f"pool/main/{package[0]}/{package}/{package}_1.0-1_amd64.deb", "sha256": "ab" * 32,
            "formats": ["deb"], "needs": needs, "label": "Debian's own package, converted by Cygnus (it may not work)", "installed": False}


def fedora(package, needs):
    return {"kind": "fedora", "package": package, "version": "2.0-1.fc44", "size": 1000, "release": 44, "location": f"Packages/{package[0]}/{package}-2.0-1.fc44.x86_64.rpm",
            "sha256": "ab" * 32, "formats": ["rpm"], "needs": needs, "label": "Fedora's own package, converted by Cygnus (it may not work)", "installed": False}


FEDORA_NEEDY = entry("fedora:gnome-tweaks", "Tweaks", [fedora("gnome-tweaks", ["gnome-tweaks-data", "python3-gobject-base"])], ("system",))
DEBIAN_NEEDY = entry("debian:abiword", "AbiWord", [debian("abiword", ["abiword-common", "fonts-urw-base35"])], ("office",))
DEBIAN_PLAIN = entry("debian:aclock", "Aclock", [debian("aclock", [])], ("utility",))
MANY = [entry(f"org.example.App{i:02}", f"App {i:02}", [flatpak(app=f"org.example.App{i:02}")]) for i in range(90)]
ALL = [FIREFOX, CHROME, EDGE, OBSIDIAN, HELIUM, APPIMAGE, APPIMAGE_SEEN, DEBIAN_NEEDY, DEBIAN_PLAIN, FEDORA_NEEDY, *MANY]
GROUPS = [{"id": "browsers", "name": "Web browsers", "count": 4}, {"id": "internet", "name": "Internet", "count": 90}, {"id": "office", "name": "Office", "count": 1}]


def fake_search(query="", group="", kind="", offset=0):
    log["searches"].append([query, group, kind, offset])
    pool = [e for e in ALL if (not group or group in e["groups"]) and (not kind or any(s["kind"] == kind for s in e["sources"]))
            and (not query or query.lower() in e["name"].lower())]
    return {"total": len(pool), "offset": offset, "entries": pool[offset:offset + 40], "groups": GROUPS}


def fake_fetch(package, fmt, progress):
    log["fetches"].append([package, fmt])
    progress("Downloading…")
    return {"ok": True, "file_url": f"file:///nonexistent/{package}.{fmt}", "version": "9.9", "format": fmt, "checked": fmt == "deb", "package": package}


def fake_release_fetch(key, fmt, progress):
    log["fetches"].append(["release", key, fmt])
    progress("Downloading…")
    return {"ok": True, "file_url": f"file:///nonexistent/{key}.{fmt}", "version": "v9.9", "format": fmt, "format_label": {"appimage": "AppImage"}.get(fmt, "." + fmt),
            "checked": fmt != "appimage", "package": key}


AUR_ANSWER = {"total": 31, "entries": [
    {"name": "vlc-git", "version": "4.0-1", "summary": "A player", "votes": 12, "popularity": 0.5, "maintainer": "somebody", "out_of_date": False, "url": "https://example.org/vlc",
     "installed": False},
    {"name": "old-vlc", "version": "1-1", "summary": "", "votes": 0, "popularity": 0.0, "maintainer": "", "out_of_date": True, "url": "", "installed": True}]}


def fake_aur(query):
    log["aur"].append(query)
    if query == "boom":
        raise CygnusError("The AUR could not be asked: no network")
    return {**AUR_ANSWER, "query": query}


def fake_appimage_fetch(repo, progress):
    log["appimages"].append(repo)
    progress("Downloading…")
    return {"ok": True, "file_url": "file:///nonexistent/tool.AppImage", "version": "v1.2", "format": "appimage", "format_label": "AppImage", "checked": False, "package": repo}


def fake_debian_fetch(package, progress):
    log["debians"].append(package)
    progress("Downloading…")
    return {"ok": True, "file_url": f"file:///nonexistent/{package}.deb", "version": "1.0-1", "format": "deb", "format_label": ".deb", "checked": True, "package": package, "needs": []}


def fake_fedora_fetch(package, progress):
    log["fedoras"].append(package)
    progress("Downloading…")
    return {"ok": True, "file_url": f"file:///nonexistent/{package}.rpm", "version": "2.0-1.fc44", "format": "rpm", "format_label": ".rpm", "checked": True, "package": package, "needs": []}


CATALOGUE = {"on": True, "stale": True, "count": 0, "decided": False}  # what the fake backend says about AppImageHub: not downloaded yet, until the page asks for it


def catalogue_rows():
    return [{"id": "appimagehub", "label": "AppImageHub", "what": "Community AppImages.", "size": "about 2 MB", "on": CATALOGUE["on"], "count": CATALOGUE["count"],
             "fetched": 0 if not CATALOGUE["count"] else time.time(), "stale": CATALOGUE["stale"]}]


def fake_refresh(force, progress, wait=None):
    log["refreshes"].append(force)
    if not force and not CATALOGUE["stale"]:  # like the real one: what is on and recent is not downloaded again unless the button was pressed
        return {"ok": True, "refreshed": [], "errors": {}, "busy": False, "status": catalogue_rows()}
    progress("Reading AppImageHub's list…")
    time.sleep(0.4)
    CATALOGUE.update(stale=False, count=2796)
    return {"ok": True, "refreshed": ["appimagehub"], "errors": {}, "busy": False, "status": catalogue_rows()}


def fake_decide(accept):
    log["decisions"].append(accept)
    CATALOGUE.update(decided=True, on=CATALOGUE["on"] and accept)  # a yes leaves a switch as it was; a no switches off
    return {"status": catalogue_rows(), "decided": True}


def fake_set_on(cat_id, on):
    log["switches"].append([cat_id, on])
    if CATALOGUE.get("fail_set"):
        raise CygnusError("The setting could not be saved.")
    CATALOGUE["on"] = on
    return {"status": catalogue_rows()}


def refuse(*_a, **_k):
    raise CygnusError("the fake backend analyses nothing")


service.discover_overview = lambda: {"total": len(ALL), "sources": [{"kind": "flatpak", "label": "flathub", "updated": time.time() - 3 * 86400}],
                                     "problems": [], "repo_missing": True, "vendors": 2, "releases": 2, "appimages": 2, "debian": 2, "fedora": 1, "catalogues": catalogue_rows(), "catalogues_decided": CATALOGUE["decided"]}
service.discover_search = fake_search
service.discover_app = lambda key: next(e for e in ALL if e["key"] == key)
service.fetch_vendor_package = fake_fetch
service.fetch_release_download = fake_release_fetch
service.discover_aur = fake_aur
service.fetch_appimage_download = fake_appimage_fetch
service.fetch_debian_download = fake_debian_fetch
service.fetch_fedora_download = fake_fedora_fetch
service.catalogue_refresh = fake_refresh
service.catalogue_set_on = fake_set_on
service.catalogue_decide = fake_decide
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


RELEASE_BUTTONS = ("releaseButton_appimage", "releaseButton_deb", "releaseButton_rpm", "releaseButton_archive", "releaseButton_pkg", "releasePageButton", "releaseInstalledNote")
AUR_ITEMS = ("aurSearchButton", "aurHeading", "aurWarning", "aurError", "aurInstallButton", "aurWebsiteButton",
             "appimageButton", "debianButton", "fedoraButton", "foreignWarning", "refreshingMessage", "refreshError", "catalogueSwitch", "catalogueUpdateButton", "catalogueState", "cataloguesQuestion", "listBadge", "listInstalled")  # the AUR's and the online catalogues'


def snap(label, *expressions):
    def go():
        log["snaps"][label] = {
            "title": js("pageStack.currentItem.title"),
            "values": [js(e) for e in expressions],
            "visible": {n: [bool(c.property("visible")) for c in children(n)] for n in ("repoMissing", "moreButton", "flatpakButton", "repoButton", "debButton", "rpmButton", "websiteButton",
                                                                                          *RELEASE_BUTTONS, *AUR_ITEMS)},
            "texts": {n: [c.property("text") for c in children(n)] for n in (*RELEASE_BUTTONS, *AUR_ITEMS)},
            "checked": [bool(c.property("checked")) for c in children("catalogueSwitch")],
            "refreshes": len(log["refreshes"]),  # how many times the page had asked for the catalogues by now
            "page": js("pageStack.currentItem.objectName || pageStack.currentItem.toString().split('_')[0]"),
            "count_text": [i.property("text") for i in children("resultCount")],
        }
    return go


def open_page(name, props):
    return lambda: QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", name), Q_ARG("QVariant", props))


STEPS = [
    (open_page("discover", {}), 2500),
    (snap("question", "pageStack.currentItem.overview.catalogues_decided", "pageStack.currentItem.refreshing"), 10),  # nothing is downloaded before the answer
    (lambda: js("pageStack.currentItem.decideCatalogues(true)"), 2500),  # yes (the message's own button calls this)
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
    (open_page("discover", {}), 2500),
    (lambda: (js("pageStack.currentItem.kind = 'release'"), js("pageStack.currentItem.search(false)")), 1000),  # the filter for the makers' own downloads
    (snap("release-only", "pageStack.currentItem.results.length"), 10),
    (lambda: (js("pageStack.currentItem.kind = ''"), js("pageStack.currentItem.search(false)")), 1000),
    (open_page("discoverApp", {"entryKey": "md.obsidian.Obsidian"}), 2000),  # the Flatpak, and the maker's own download beside it
    (snap("release-card", "pageStack.currentItem.entry.name", "pageStack.currentItem.entry.sources.length"), 10),
    (click("releaseButton_deb"), 1500),
    (snap("release-deb-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
    (open_page("discoverApp", {"entryKey": "md.obsidian.Obsidian"}), 2000),
    (click("releaseButton_appimage"), 1500),
    (snap("release-appimage-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
    (open_page("discoverApp", {"entryKey": "release:helium"}), 2000),  # a program that is not on Flathub: the download alone
    (snap("release-alone", "pageStack.currentItem.entry.name", "pageStack.currentItem.entry.sources.length"), 10),
    (open_page("discoverApp", {"entryKey": "appimage:owner/Tool"}), 2000),  # a program from AppImageHub's community list
    (snap("appimage-card", "pageStack.currentItem.entry.name", "pageStack.currentItem.entry.sources.length"), 10),
    (click("appimageButton"), 1500),
    (snap("appimage-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
    (open_page("discoverApp", {"entryKey": "debian:abiword"}), 2000),  # a program from Debian's repository, with other packages it says it needs
    (snap("debian-needy", "pageStack.currentItem.entry.name", "pageStack.currentItem.entry.sources.length"), 10),
    (click("debianButton"), 1500),
    (snap("debian-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
    (open_page("discoverApp", {"entryKey": "debian:aclock"}), 2000),  # one that needs nothing but libraries
    (snap("debian-plain", "pageStack.currentItem.entry.name"), 10),
    (open_page("discoverApp", {"entryKey": "fedora:gnome-tweaks"}), 2000),  # a program from Fedora's repository
    (snap("fedora-card", "pageStack.currentItem.entry.name", "pageStack.currentItem.entry.sources.length"), 10),
    (click("fedoraButton"), 1500),
    (snap("fedora-handover", "pageStack.currentItem.fileUrl", "pageStack.currentItem.launchNotice"), 10),
    (open_page("discoverApp", {"entryKey": "appimage:owner/Seen"}), 2000),  # one whose name Cygnus already manages
    (snap("appimage-seen", "pageStack.currentItem.entry.name"), 10),
    (open_page("discover", {}), 2500),
    (lambda: (js("pageStack.currentItem.kind = 'appimage'"), js("pageStack.currentItem.search(false)")), 1000),  # the filter for the community AppImages
    (snap("appimage-only", "pageStack.currentItem.results.length"), 10),
    (lambda: (js("pageStack.currentItem.kind = 'debian'"), js("pageStack.currentItem.search(false)")), 1000),  # the filter for Debian's packages
    (snap("debian-only", "pageStack.currentItem.results.length"), 10),
    (lambda: (js("pageStack.currentItem.kind = 'fedora'"), js("pageStack.currentItem.search(false)")), 1000),  # the filter for Fedora's packages
    (snap("fedora-only", "pageStack.currentItem.results.length"), 10),
    (lambda: (js("pageStack.currentItem.kind = ''"), js("pageStack.currentItem.search(false)")), 1000),
    (snap("catalogues-now", "pageStack.currentItem.overview.catalogues[0].count", "pageStack.currentItem.refreshing"), 10),
    (click("catalogueUpdateButton"), 1500),  # pressed: downloaded again even though it is recent
    (snap("catalogues-forced", "pageStack.currentItem.overview.catalogues[0].count"), 10),
    (click("catalogueSwitch"), 1500),  # switched off
    (snap("catalogues-off", "pageStack.currentItem.overview.catalogues[0].on"), 10),
    (click("catalogueSwitch"), 1500),  # and on again: it is recent, so nothing is downloaded
    (snap("catalogues-on", "pageStack.currentItem.overview.catalogues[0].on"), 10),
    (lambda: CATALOGUE.update(decided=False, on=True), 10),  # a person who has not been asked yet, and answers "not now"
    (open_page("discover", {}), 2500),
    (snap("question-again", "pageStack.currentItem.overview.catalogues_decided"), 10),
    (lambda: js("pageStack.currentItem.decideCatalogues(false)"), 2000),  # the message's own button calls this
    (snap("question-no", "pageStack.currentItem.overview.catalogues_decided", "pageStack.currentItem.overview.catalogues[0].on"), 10),
    (lambda: CATALOGUE.update(fail_set=True), 10),  # a switch that could not be saved shows what IS saved, and says why
    (click("catalogueSwitch"), 2000),
    (snap("switch-failed", "pageStack.currentItem.refreshError", "pageStack.currentItem.overview.catalogues[0].on"), 10),
    (lambda: CATALOGUE.update(fail_set=False), 10),
    (open_page("discover", {}), 2500),  # the AUR: asked only when the button is pressed, for the words in the box
    (snap("aur-start", "pageStack.currentItem.query"), 10),
    (lambda: js("pageStack.currentItem.query = 'v'"), 1500),  # one letter is too few
    (snap("aur-one-letter", "pageStack.currentItem.query"), 10),
    (lambda: js("pageStack.currentItem.query = 'vlc'"), 1500),
    (snap("aur-button", "pageStack.currentItem.query"), 10),
    (click("aurSearchButton"), 1500),
    (snap("aur-results", "pageStack.currentItem.aurResult.total", "pageStack.currentItem.aurResult.entries.length"), 10),
    (click("aurInstallButton"), 1500),
    (snap("aur-handover", "pageStack.currentItem.appSpec"), 10),
    (open_page("discover", {}), 2500),
    (lambda: js("pageStack.currentItem.query = 'vlc'"), 1500),
    (click("aurSearchButton"), 1500),
    (lambda: js("pageStack.currentItem.query = 'vlcx'"), 1500),  # other words: the answer for the old ones is gone
    (snap("aur-cleared", "pageStack.currentItem.aurResult === null"), 10),
    (lambda: js("pageStack.currentItem.query = 'boom'"), 1500),
    (click("aurSearchButton"), 1500),
    (snap("aur-error", "pageStack.currentItem.aurError", "pageStack.currentItem.aurResult === null"), 10),
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
