"""A second launch (Flathub's Install button, a file opened from the file manager) hands its arguments to the window that is
already open instead of opening another one. Run on a private session bus, with real processes."""

import json
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.needs_tool("dbus-daemon")

LISTENER = r'''
import sys
from PySide6.QtCore import QCoreApplication, QTimer
from cygnus.gui import instance
app = QCoreApplication([])
receiver = instance.Receiver()
print("LISTEN", instance.listen(receiver), flush=True)
receiver.opened.connect(lambda payload: (print("GOT", payload, flush=True), app.quit()))
QTimer.singleShot(30000, app.quit)
app.exec()
'''
SECOND = r'''
import sys
from PySide6.QtCore import QCoreApplication
from cygnus.gui import instance
app = QCoreApplication([])
receiver = instance.Receiver()
listened = instance.listen(receiver)
# as the application does: only a launch that is NOT the first one hands its arguments over
print("LISTENED", listened, "FORWARDED", None if listened else instance.forward(sys.argv[1:], "/work"), flush=True)
'''
FORWARD_ONLY = r'''
import sys
from PySide6.QtCore import QCoreApplication
from cygnus.gui import instance
app = QCoreApplication([])
print("FORWARDED", instance.forward(sys.argv[1:], "/work"), flush=True)
'''


@pytest.fixture
def bus():
    daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"], stdout=subprocess.PIPE, text=True)
    address = daemon.stdout.readline().strip()
    yield {**os.environ, "DBUS_SESSION_BUS_ADDRESS": address, "QT_QPA_PLATFORM": "offscreen"}
    daemon.terminate()
    daemon.wait(timeout=10)


def _python(script, env, *args, **kw):
    return subprocess.Popen([sys.executable, "-c", script, *args], env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kw)


def test_a_second_launch_gives_its_arguments_to_the_first_and_does_not_become_a_window(bus):
    first = _python(LISTENER, bus)
    assert first.stdout.readline().strip() == "LISTEN True"
    second = subprocess.run([sys.executable, "-c", SECOND, "appstream:org.mozilla.firefox", "/tmp/x.flatpakref"], env=bus,
                            capture_output=True, text=True, timeout=60)
    assert second.stdout.strip() == "LISTENED False FORWARDED True", second.stderr[-1500:]
    line = first.stdout.readline()
    first.wait(timeout=30)
    assert line.startswith("GOT ")
    assert json.loads(line[4:]) == {"args": ["appstream:org.mozilla.firefox", "/tmp/x.flatpakref"], "cwd": "/work"}


def test_with_no_other_window_the_first_launch_simply_is_the_instance(bus):
    out = subprocess.run([sys.executable, "-c", SECOND, "x"], env=bus, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "LISTENED True FORWARDED None"  # it is the first: nothing to hand over
    alone = subprocess.run([sys.executable, "-c", FORWARD_ONLY, "x"], env=bus, capture_output=True, text=True, timeout=60)
    assert alone.stdout.strip() == "FORWARDED False"  # and if nobody were there, handing over reports failure


def test_without_a_session_bus_nothing_is_coordinated_and_nothing_fails(bus):
    env = {**bus, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus"}
    out = subprocess.run([sys.executable, "-c", SECOND, "x"], env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "LISTENED True FORWARDED None"


def test_what_a_launch_asks_for_becomes_the_install_pages_properties(tmp_path):
    from cygnus.gui import app

    assert app.page_request([], "/") is None and app.page_request(["--some-option"], "/") is None
    assert app.page_request(["appstream:org.mozilla.firefox"], "/") == {"appSpec": "flathub:org.mozilla.firefox", "launchNotice": ""}
    deb = tmp_path / "x.deb"
    deb.write_bytes(b"x")
    assert app.page_request(["--ignored", str(deb)], "/") == {"fileUrl": deb.as_uri(), "launchNotice": ""}
    refused = app.page_request(["https://evil.example/x.deb"], "/")
    assert "fileUrl" not in refused and refused["launchNotice"]


def test_the_real_application_hands_a_second_launch_to_the_open_window(bus, tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**bus, "PYTHONPATH": repo, "QT_QUICK_CONTROLS_STYLE": "org.kde.desktop", "XDG_RUNTIME_DIR": str(tmp_path)}
    first = subprocess.Popen([sys.executable, "-m", "cygnus.gui.app"], env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=repo)
    try:
        for _ in range(100):  # the first window is the one that owns the application's name on the bus
            probe = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
                                    "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
                                    "string:io.github.omranabdulaziz.Cygnus"], env=env, capture_output=True, text=True)
            if "boolean true" in probe.stdout:
                break
            time.sleep(0.2)
        else:
            pytest.fail("the first window never appeared on the bus")
        time.sleep(4)  # let its QML finish loading
        started = time.monotonic()
        second = subprocess.run([sys.executable, "-m", "cygnus.gui.app", "appstream:org.mozilla.firefox"], env=env,
                                capture_output=True, text=True, timeout=60, cwd=repo)
        assert second.returncode == 0 and time.monotonic() - started < 30, second.stderr[-1500:]
        time.sleep(3)
        assert first.poll() is None  # the first window is still the only one, and still running
    finally:
        first.terminate()
        try:
            _, err = first.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            first.kill()
            _, err = first.communicate()
    assert "Traceback" not in err, err[-2000:]
