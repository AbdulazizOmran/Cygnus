"""cygnus-helper end to end on a private D-Bus with a fake polkit (no root, commands faked)."""

import hashlib
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

import pytest

import builders
from cygnus.core.privilege import HelperClient, HelperError, HelperTimeout

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.needs_tool("dbus-daemon")


class Bus:
    def __init__(self, tmp: Path, allow: bool = True):
        self.tmp = tmp
        self.polkit_log = tmp / "polkit.jsonl"
        self.state = tmp / "state"
        self.staging = tmp / "staging"
        self.daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                                       stdout=subprocess.PIPE, text=True)
        self.address = self.daemon.stdout.readline().strip()
        env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": self.address, "PYTHONPATH": f"{REPO}:{REPO / 'tests'}",
               "FAKE_POLKIT_LOG": str(self.polkit_log), "FAKE_POLKIT_ALLOW": "1" if allow else "0",
               "CYGNUS_HELPER_BUS": "session", "CYGNUS_HELPER_FAKE_EXEC": "1",
               "CYGNUS_HELPER_STATE_DIR": str(self.state), "CYGNUS_HELPER_STAGING_DIR": str(self.staging)}
        self.polkit = subprocess.Popen([sys.executable, str(REPO / "tests/fake_polkit.py")], env=env,
                                       stdout=subprocess.PIPE, text=True)
        assert self.polkit.stdout.readline().strip() == "ready"
        self.helper = subprocess.Popen([sys.executable, "-m", "cygnus.helper.service"], env=env, cwd=REPO)
        self.client = HelperClient(self.address)
        for _ in range(100):
            try:
                self.client.ledger()
                break
            except HelperError:
                time.sleep(0.05)
        else:
            raise RuntimeError("helper did not start")

    def polkit_calls(self):
        if not self.polkit_log.exists():
            return []
        return [json.loads(line) for line in self.polkit_log.read_text().splitlines()]

    def close(self):
        for p in (self.helper, self.polkit, self.daemon):
            p.terminate()
            p.wait(timeout=10)


@pytest.fixture
def bus(tmp_path):
    b = Bus(tmp_path)
    yield b
    b.close()


ME = pwd.getpwuid(os.getuid()).pw_name


def test_group_add_flow(bus):
    plan = bus.client.plan_group("input", "add")
    assert "keystroke" in plan.message and plan.summary == {"user": ME, "group": "input", "op": "add"}
    lines = []
    ok, detail = bus.client.commit(plan, on_progress=lines.append)
    assert ok, detail
    assert any(f"gpasswd -a {ME} input" in line for line in lines)
    [call] = bus.polkit_calls()
    assert call["action"] == "io.github.omranabdulaziz.Cygnus.permissions.manage"
    assert call["message"] == plan.message and call["subject"] == "system-bus-name" and call["interactive"]
    [op] = bus.client.ledger()
    assert op["state"] == "succeeded" and op["kind"] == "group"


def test_a_commit_that_times_out_names_its_operation_and_the_ledger_says_how_it_ended(bus):
    plan = bus.client.plan_group("input", "add")
    with pytest.raises(HelperTimeout) as caught:
        bus.client.commit(plan, timeout_s=0)  # the helper was asked, but nobody waited for its answer
    op_id = caught.value.op_id
    for _ in range(100):  # the helper goes on without the client
        if bus.client.operation_state(op_id) == "succeeded":
            break
        time.sleep(0.1)
    assert bus.client.operation_state(op_id) == "succeeded"
    assert bus.client.operation_state("no-such-operation") is None


def test_group_outside_allowlist_refused(bus):
    with pytest.raises(HelperError, match="does not manage"):
        bus.client.plan_group("wheel", "add")


def test_group_remove_requires_ledger(bus):
    with pytest.raises(HelperError, match="did not add"):
        bus.client.plan_group("input", "remove")


def test_local_package_is_staged_by_fd_and_described(bus, tmp_path):
    pkg = builders.build_pkg(tmp_path, name="cygnus-test-pkg", install_script="post_install() { :; }\n")
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    plan = bus.client.plan_packages(local_files=[(str(pkg), sha)])
    assert "UNSIGNED" in plan.message and sha[:8] in plan.message and "install script as root" in plan.message
    [entry] = plan.summary["local"]
    assert entry["read_by_uid"] == os.getuid()  # the file was read by the package reader, not by the helper itself
    staged = list(bus.staging.iterdir())
    assert len(staged) == 1 and oct(staged[0].stat().st_mode & 0o777) == "0o644"
    lines = []
    ok, _ = bus.client.commit(plan, on_progress=lines.append)
    assert ok
    assert any(line.startswith("[fake] pacman -U --noconfirm -- " + str(bus.staging)) for line in lines)
    assert not list(bus.staging.iterdir())  # staged copy removed afterwards
    assert not any("--needed" in line for line in lines if "pacman -U" in line)  # a chosen file is never skipped as "up to date"
    assert bus.polkit_calls()[0]["action"].endswith("packages.install-local")


def test_wrong_checksum_refused(bus, tmp_path):
    pkg = builders.build_pkg(tmp_path)
    with pytest.raises(HelperError, match="checksum"):
        bus.client.plan_packages(local_files=[(str(pkg), "0" * 64)])
    assert not list(bus.staging.iterdir()) if bus.staging.exists() else True


def test_non_package_file_refused(bus, tmp_path):
    deb = builders.build_deb(tmp_path)
    sha = hashlib.sha256(deb.read_bytes()).hexdigest()
    with pytest.raises(HelperError, match="Arch packages"):
        bus.client.plan_packages(local_files=[(str(deb), sha)])


def test_plan_is_single_use_and_bound_to_caller(bus):
    plan = bus.client.plan_group("input", "add")
    other = HelperClient(bus.address)
    with pytest.raises(HelperError, match="another caller"):
        other.commit(plan)
    ok, _ = bus.client.commit(plan)
    assert ok
    with pytest.raises(HelperError, match="unknown or expired"):
        bus.client.commit(plan)


def test_protected_and_unowned_removals_refused(bus):
    with pytest.raises(HelperError, match="protected"):
        bus.client.plan_packages(remove=["glibc"])
    with pytest.raises(HelperError, match="not installed by Cygnus"):
        bus.client.plan_packages(remove=["ncdu"])
    with pytest.raises(HelperError, match="invalid package name"):
        bus.client.plan_packages(install_repo=["--overwrite=*"])


def test_denied_authorization_runs_nothing(tmp_path):
    b = Bus(tmp_path, allow=False)
    try:
        plan = b.client.plan_group("input", "add")
        ok, detail = b.client.commit(plan)
        assert not ok and "denied" in detail
        assert b.client.ledger() == []
    finally:
        b.close()


@pytest.mark.host
def test_repo_install_plan_uses_real_package_analysis(bus):
    plan = bus.client.plan_packages(install_repo=["ncdu"])
    names = [p["name"] for p in plan.summary["install"]]
    assert "ncdu" in names and plan.message.startswith("Install ncdu")
    lines = []
    ok, _ = bus.client.commit(plan, on_progress=lines.append)
    assert ok and any("[fake] pacman -S --noconfirm --needed -- ncdu" in line for line in lines)
    assert bus.polkit_calls()[0]["action"].endswith("packages.install-repo")


def test_refused_requests_never_leak_descriptors_or_staged_files(bus, tmp_path):
    """Any user can call PlanPackages; refused requests must leave nothing open or staged in root's helper."""
    pkg = builders.build_pkg(tmp_path)
    V, Gio = bus.client.GLib.Variant, bus.client.Gio

    def open_fds():
        return len(os.listdir(f"/proc/{bus.helper.pid}/fd"))

    def mismatched():
        fd_list = Gio.UnixFDList.new()
        fd = os.open(pkg, os.O_RDONLY)
        try:
            handles = [fd_list.append(fd), fd_list.append(fd)]  # two files, one checksum
            bus.client._call("PlanPackages", V("(a{sv}ahas)", ({}, handles, ["0" * 64])), "(sss)", fd_list=fd_list)
        finally:
            os.close(fd)

    for _ in range(3):  # warm up
        with pytest.raises(HelperError):
            mismatched()
    before = open_fds()
    for _ in range(20):
        with pytest.raises(HelperError, match="exactly one checksum"):
            mismatched()
        with pytest.raises(HelperError, match="does not match"):
            bus.client.plan_packages(local_files=[(str(pkg), "0" * 64)])
    assert open_fds() <= before
    assert not bus.staging.exists() or list(bus.staging.iterdir()) == []


def test_stale_lock_method_refuses_when_nothing_is_stale(bus):
    # The test helper looks at the real /var/lib/pacman: normally unlocked (or in use by a running pacman).
    with pytest.raises(HelperError, match="not locked|is running"):
        bus.client.plan_clear_stale_lock()


def _raw_pkg(dest, name, pkginfo: bytes, late_install=False):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        def add(n, data):
            info = tarfile.TarInfo(n)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        add(".PKGINFO", pkginfo)
        add("usr/share/cygnus-test/x", b"x")
        if late_install:
            add(".INSTALL", b"post_install() { :; }\n")
    out = dest / f"{name}.pkg.tar.zst"
    out.write_bytes(subprocess.run(["zstd", "-q", "-c"], input=buf.getvalue(), capture_output=True, check=True).stdout)
    return out


@pytest.mark.parametrize("pkginfo", [
    b"pkgname = glibc\npkgver = 9.99-1\npkgdesc = nice\x1cpkgname = harmless\narch = any\n",
    b"pkgname = glibc\n pkgname = harmless\npkgver = 9.99-1\narch = any\n",
])
def test_the_helper_sees_what_pacman_will_install(bus, tmp_path, pkginfo):
    """A .PKGINFO that reads as "harmless" to a naive parser but is glibc to libalpm."""
    pkg = _raw_pkg(tmp_path, "disguised", pkginfo)
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    with pytest.raises(HelperError, match="glibc is a protected system package"):
        bus.client.plan_packages(local_files=[(str(pkg), sha)])


def test_a_late_install_script_is_announced(bus, tmp_path):
    pkg = _raw_pkg(tmp_path, "late", b"pkgname = cygnus-late-test\npkgver = 1-1\narch = any\n", late_install=True)
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    plan = bus.client.plan_packages(local_files=[(str(pkg), sha)])
    assert "runs an install script as root" in plan.message


def test_pkginfo_is_read_like_libalpm():
    from cygnus.core.detect.pkg import parse_pkginfo

    assert parse_pkginfo("pkgname = glibc\npkgdesc = a\x1cpkgname = x\n")["pkgname"] == "glibc"
    assert parse_pkginfo("pkgname = glibc\n pkgname = x\n")["pkgname"] == "glibc"


def test_the_dialog_says_when_a_file_is_the_same_version_that_is_already_installed():
    from cygnus.helper import actions

    base = {"install": [], "upgrade": [], "remove": [], "local": [
        {"name": "chrome", "version": "1-1", "sha256": "ab" * 32, "signed": False, "has_install_script": False,
         "replaces_installed": "1-1"}], "notes": []}
    assert "REINSTALLING the same version (1-1)" in actions._package_message(base, False)
    base["local"][0]["replaces_installed"] = "1-0"
    assert "REPLACING the installed chrome 1-0" in actions._package_message(base, False)
    base["local"][0].pop("replaces_installed")
    assert "REPLAC" not in actions._package_message(base, False) and "REINSTALL" not in actions._package_message(base, False)


def test_a_second_helper_that_is_told_the_name_is_taken_leaves_the_running_helpers_state_alone(bus):
    import sqlite3

    # a "running" operation and a staged file belong to the live helper...
    plan = bus.client.plan_group("input", "add")
    bus.client.commit(plan)
    con = sqlite3.connect(bus.state / "ledger.db", timeout=30)
    con.execute("UPDATE operation SET state='running', finished=NULL")
    con.commit()
    con.close()
    staged = bus.staging / "staged-for-the-live-helper"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"x")
    # ...and a second helper process starts on the same bus (the name is taken, so it quits)
    env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": bus.address, "PYTHONPATH": f"{REPO}:{REPO / 'tests'}",
           "CYGNUS_HELPER_BUS": "session", "CYGNUS_HELPER_FAKE_EXEC": "1", "CYGNUS_HELPER_STATE_DIR": str(bus.state),
           "CYGNUS_HELPER_STAGING_DIR": str(bus.staging)}
    second = subprocess.run([sys.executable, "-m", "cygnus.helper.service"], env=env, cwd=REPO, capture_output=True, text=True,
                            timeout=60)
    assert "could not own" in second.stderr
    assert staged.exists()  # not swept away
    con = sqlite3.connect(bus.state / "ledger.db", timeout=30)
    states = [r[0] for r in con.execute("SELECT state FROM operation")]
    con.close()
    assert states == ["running"]  # not declared interrupted: the live helper is still working on it


def test_an_old_operation_is_found_by_its_id_however_many_came_after_it(bus):
    import sqlite3

    plan = bus.client.plan_group("input", "add")
    ok, _ = bus.client.commit(plan)
    assert ok
    [first] = bus.client.ledger()
    con = sqlite3.connect(bus.state / "ledger.db", timeout=30)
    for i in range(150):  # 150 newer operations of the same user: the list (last 100) no longer holds the first
        con.execute("INSERT INTO operation(id, kind, caller_uid, summary, argv, state, started) VALUES (?,?,?,?,?,?,?)",
                    (f"later-{i}", "group", os.getuid(), "{}", "[]", "succeeded", "2099-01-01T00:00:00+00:00"))
    con.execute("INSERT INTO operation(id, kind, caller_uid, summary, argv, state, started) VALUES "
                "('someone-elses', 'group', 1, '{}', '[]', 'succeeded', '2099-01-01T00:00:00+00:00')")
    con.commit()
    con.close()
    assert first["id"] not in [r["id"] for r in bus.client.ledger()]
    assert bus.client.operation_state(first["id"]) == "succeeded"  # found anyway
    assert bus.client.operation_state("someone-elses") is None  # only your own
    assert bus.client.operation_state("x" * 100) is None


def test_a_helper_from_before_the_exact_lookup_existed_is_still_understood(bus, monkeypatch):
    plan = bus.client.plan_group("input", "add")
    bus.client.commit(plan)
    [op] = bus.client.ledger()
    real = bus.client._call

    def old_helper(method, *args, **kw):
        if method == "GetOperation":
            raise HelperError("GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod: No such method “GetOperation”")
        return real(method, *args, **kw)

    monkeypatch.setattr(bus.client, "_call", old_helper)
    assert bus.client.operation_state(op["id"]) == "succeeded"  # from the list of recent operations instead
