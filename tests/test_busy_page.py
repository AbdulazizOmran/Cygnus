"""A link handed to Cygnus (Flathub's Install button) must never replace a page that is in the middle of something."""

import json
import subprocess
import sys

import pytest

from cygnus.gui import app

pytest.importorskip("PySide6")


def _deliver(args, busy):
    seen = {"navigated": [], "told": [], "shown": 0}
    app.deliver(args, "/", busy=lambda: busy, navigate=seen["navigated"].append, notify=seen["told"].append,
                show=lambda: seen.__setitem__("shown", seen["shown"] + 1))
    return seen


def test_when_nothing_is_going_on_the_link_opens_its_page_and_the_window_comes_forward():
    seen = _deliver(["appstream:org.mozilla.firefox"], busy=False)
    assert seen["navigated"] == [{"appSpec": "flathub:org.mozilla.firefox", "launchNotice": ""}]
    assert seen["told"] == [] and seen["shown"] == 1


def test_when_the_page_is_busy_it_is_left_alone_and_the_person_is_told():
    seen = _deliver(["appstream:org.mozilla.firefox"], busy=True)
    assert seen["navigated"] == [] and seen["told"] == [app.BUSY_MESSAGE] and seen["shown"] == 1


def test_a_launch_without_arguments_only_brings_the_window_forward_even_when_busy():
    for busy in (False, True):
        seen = _deliver([], busy=busy)
        assert seen["navigated"] == [] and seen["told"] == [] and seen["shown"] == 1


SCRIPT = r'''
import json, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")
from PySide6.QtCore import Q_ARG, QMetaObject, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression
from cygnus.gui.app import QML_DIR
from cygnus.gui.bridge import Backend

app = QGuiApplication(sys.argv[:1])
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty("backend", Backend())
component = QQmlComponent(engine)
state, report = {}, {}

def js(expr):
    w = state["window"]
    value, undefined = QQmlExpression(QQmlEngine.contextForObject(w), w, expr).evaluate()
    return None if undefined else value

def open_page(name):
    QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", name), Q_ARG("QVariant", {}))

def probe():
    open_page("install")
    QTimer.singleShot(1500, lambda: check())

def check():
    page = "pageStack.currentItem"
    report["idle"] = js("pageBusy()")
    js(page + ".installing = true"); report["installing"] = js("pageBusy()"); js(page + ".installing = false")
    js(page + ".analysing = true"); report["analysing"] = js("pageBusy()"); js(page + ".analysing = false")
    js(page + ".confirmPlan = ({title: 'x'})"); report["question_open"] = js("pageBusy()"); js(page + ".confirmPlan = null")
    report["idle_again"] = js("pageBusy()")
    # an install that has just succeeded: the question that was answered must not keep the page busy
    js(page + ".confirmPlan = ({title: 'x'})"); js(page + ".installing = true")
    js(page + ".finish({ok: true, result: {ok: true}})"); report["after_install"] = js("pageBusy()")
    js(page + ".confirmPlan = ({title: 'x'})"); js(page + ".finish({ok: false, error: 'no'})"); report["after_failure"] = js("pageBusy()")
    report["why"] = [js("why({ok: false, error: ''})"), js("why({ok: true, result: {ok: false, error: '', detail: 'd'}})"),
                     js("why({ok: true, result: {ok: false}})"), js("why({ok: false, error: 'e'})")]
    QMetaObject.invokeMethod(state["window"], "openPage", Q_ARG("QVariant", "app"), Q_ARG("QVariant", {"appName": "x", "installationId": "1", "installationFormat": "appimage", "installationLocation": "HDD"}))
    QTimer.singleShot(1500, lambda: check_move_targets())

def check_move_targets():
    js("pageStack.currentItem.storage = ({locations: [{label: 'SSD', online: true, appimages: true, flatpak: true}, {label: 'HDD', online: true, appimages: true, flatpak: false}, {label: 'USB', online: false, appimages: true, flatpak: true}], candidates: []})")
    report["move_targets_appimage"] = js("pageStack.currentItem.moveTargets").toVariant()
    js("pageStack.currentItem.installationLocation = 'ssd'")  # compared without regard to case
    report["move_targets_from_ssd"] = js("pageStack.currentItem.moveTargets").toVariant()
    js("pageStack.currentItem.installationFormat = 'flatpak'")
    report["move_targets_flatpak_from_ssd"] = js("pageStack.currentItem.moveTargets").toVariant()
    open_page("updates")
    QTimer.singleShot(1500, lambda: check_updates())

def check_updates():
    js("pageStack.currentItem.checking = true"); report["updates_checking"] = js("pageBusy()")
    report["vendor_feed_text"] = js("pageStack.currentItem.describe('vendor-feed')")
    report["has_fetch_update"] = js("typeof pageStack.currentItem.fetchUpdate")
    open_page("settings")
    QTimer.singleShot(1500, lambda: (report.__setitem__("settings_idle", js("pageBusy()")), print(json.dumps(report), flush=True), app.quit()))

def on_status(status):
    if status == QQmlComponent.Status.Ready:
        state["window"] = component.create()
        QTimer.singleShot(1000, probe)
    elif status == QQmlComponent.Status.Error:
        print([e.toString() for e in component.errors()]); app.exit(1)

component.statusChanged.connect(on_status)
component.loadUrl(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")), QQmlComponent.CompilationMode.Asynchronous)
QTimer.singleShot(60000, lambda: app.exit(3))
sys.exit(app.exec())
'''


def test_the_real_window_knows_when_the_page_in_front_is_busy():
    out = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=120)
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    assert lines, out.stderr[-2000:]
    assert json.loads(lines[-1]) == {"idle": False, "installing": True, "analysing": True, "question_open": True,
                                     "idle_again": False, "after_install": False, "after_failure": False,
                                     "why": ["it did not finish", "d", "it did not finish", "e"],
                                     "updates_checking": True, "settings_idle": False,
                                     "move_targets_appimage": ["SSD"], "move_targets_from_ssd": ["HDD"],
                                     "move_targets_flatpak_from_ssd": [],
                                     "vendor_feed_text": "Source: the vendor's package list", "has_fetch_update": "function"}


def test_launches_that_arrive_before_the_window_exists_are_kept_and_acted_on_in_order():
    got = []
    backlog = app.Backlog(lambda args, cwd: got.append((args, cwd)))
    backlog.add(["a.flatpakref"], "/x")
    backlog.add(["appstream:org.x.App"], "/y")  # the window is still loading: held, not lost
    assert got == []
    backlog.release()
    assert got == [(["a.flatpakref"], "/x"), (["appstream:org.x.App"], "/y")]
    backlog.add(["b.deb"], "/z")  # afterwards: straight away
    assert got[-1] == (["b.deb"], "/z") and len(got) == 3
    backlog.release()
    assert len(got) == 3  # nothing is delivered twice


@pytest.mark.parametrize("payload", ["not json", "[]", "null", '{"args": "abc"}', '{"args": [1, 2]}', '{"args": {"a": 1}}',
                                     json.dumps({"args": ["x"] * 65})])
def test_a_handover_that_is_not_what_a_launch_sends_is_ignored(payload):
    assert app.parse_handover(payload) is None


def test_a_good_handover_is_read_and_a_missing_folder_means_here():
    import os

    assert app.parse_handover(json.dumps({"args": ["x.deb"], "cwd": "/tmp"})) == (["x.deb"], "/tmp")
    assert app.parse_handover(json.dumps({"args": []})) == ([], os.getcwd())
    assert app.parse_handover(json.dumps({"args": ["x"], "cwd": 5}))[1] == os.getcwd()


def test_the_value_of_a_qt_option_is_not_taken_for_a_file():
    assert app.page_request(["-platform", "offscreen"], "/") is None
    assert app.page_request(["-style", "Breeze", "appstream:org.x.App"], "/")["appSpec"] == "flathub:org.x.App"
    assert app.page_request(["--help"], "/") is None
    assert app.page_request(["--platform", "xcb", "appstream:org.x.App"], "/")["appSpec"] == "flathub:org.x.App"
