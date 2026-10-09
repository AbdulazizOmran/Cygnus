"""The unprivileged package reader: untrusted package files are never opened by the root helper."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import builders
from cygnus.helper import actions, inspect_worker, seccomp

pytestmark = pytest.mark.needs_tool("bsdtar", "zstd")


def _run_worker(path):
    out = subprocess.run([sys.executable, "-I", str(actions.INSPECT_WORKER), str(path)], capture_output=True, text=True,
                         timeout=120)
    return out.returncode, json.loads(out.stdout)


# -- the reader as a program ---------------------------------------------------------------------------------
def test_the_reader_reports_what_libalpm_reads_from_a_package(tmp_path):
    pkg = builders.build_pkg(tmp_path, name="cygnus-test-pkg", install_script="post_install() { :; }\n")
    rc, data = _run_worker(pkg)
    assert rc == 0 and data["ok"] is True
    assert data["facts"]["name"] == "cygnus-test-pkg" and data["facts"]["has_scriptlet"] is True
    assert data["identity"] == {"uid": os.getuid(), "sandboxed": False}  # not root here: nothing to give up


@pytest.mark.parametrize("make", [
    lambda d: builders.build_deb(d, depends=""), lambda d: builders.build_appimage(d),
    lambda d: _write(d / "garbage.pkg.tar.zst", b"this is not an archive"), lambda d: _write(d / "empty.pkg.tar", b"")])
def test_anything_that_is_not_a_readable_arch_package_is_a_refusal_not_a_crash(tmp_path, make):
    if make.__name__ == "<lambda>" and "appimage" in make.__code__.co_names:
        pytest.importorskip("PySide6")
    rc, data = _run_worker(make(tmp_path))
    assert rc == 1 and data["ok"] is False and (data.get("refused") or data.get("error"))


def _write(path, data):
    path.write_bytes(data)
    return path


def test_a_missing_file_or_wrong_usage_is_reported(tmp_path):
    rc, data = _run_worker(tmp_path / "nope.pkg.tar.zst")
    assert rc == 1 and data["ok"] is False
    out = subprocess.run([sys.executable, "-I", str(actions.INSPECT_WORKER)], capture_output=True, text=True)
    assert out.returncode == 1 and json.loads(out.stdout)["ok"] is False


# -- the lockdown sequence (simulated: the tests do not run as root) --------------------------------------------
class FakeSystem:
    """Records the order of the calls lock_down makes, and behaves like a process that really became the reader account."""

    def __init__(self, monkeypatch, *, can_regain=False, capabilities=(0, 0), leaves_groups=False, fails_at=None):
        self.calls, self.uid, self.gid, self.groups = [], 0, 0, [0]
        self.can_regain, self.capabilities, self.leaves_groups, self.fails_at = can_regain, capabilities, leaves_groups, fails_at
        m = monkeypatch
        m.setattr(inspect_worker.os, "geteuid", lambda: self.uid)
        m.setattr(inspect_worker.os, "unshare", lambda flags: self._do("unshare", flags))
        m.setattr(inspect_worker.resource, "setrlimit", lambda which, lim: self._do("setrlimit", which, lim))
        m.setattr(inspect_worker.ctypes, "CDLL", lambda *a, **k: NS(prctl=lambda *args: self._do("prctl", *args) or 0))
        m.setattr(inspect_worker.seccomp, "install", lambda: self._do("seccomp") or True)
        m.setattr(inspect_worker.os, "chdir", lambda p: self._do("chdir"))
        m.setattr(inspect_worker.os, "setgroups", self._setgroups)
        m.setattr(inspect_worker.os, "setgid", self._setgid)
        m.setattr(inspect_worker.os, "setuid", self._setuid)
        m.setattr(inspect_worker.os, "getuid", lambda: self.uid)
        m.setattr(inspect_worker.os, "getgid", lambda: self.gid)
        m.setattr(inspect_worker.os, "getresuid", lambda: (self.uid,) * 3)
        m.setattr(inspect_worker.os, "getresgid", lambda: (self.gid,) * 3)
        m.setattr(inspect_worker.os, "getgroups", lambda: list(self.groups))
        m.setattr(inspect_worker, "_capabilities", lambda: self.capabilities)

    def _do(self, name, *args):
        if self.fails_at == name:
            raise OSError(1, f"{name} not permitted")
        self.calls.append(name)

    def _setgroups(self, groups):
        if self.uid != 0 and not self.can_regain:
            raise PermissionError  # only root may change its groups: a refused attempt is not a change
        self._do("setgroups")
        self.groups = list(groups)
        if self.leaves_groups and groups == []:
            self.groups = [0]

    def _setgid(self, gid):
        if self.uid != 0 and gid == 0 and not self.can_regain:
            raise PermissionError
        self._do("setgid")
        self.gid = gid

    def _setuid(self, uid):
        if self.uid != 0 and not self.can_regain and uid == 0:
            raise PermissionError
        self._do("setuid")
        self.uid = uid


def test_root_is_given_up_in_the_right_order_before_anything_is_read(monkeypatch):
    system = FakeSystem(monkeypatch)
    identity = inspect_worker.lock_down(901, 902)
    order = [c for c in system.calls if c in ("unshare", "prctl", "chdir", "setgroups", "setgid", "setuid", "seccomp")]
    # the namespaces are left and the limits set while still root; then groups, then group, then user; the syscall
    # filter goes in last, once nothing is left to do that it would forbid
    assert order == ["unshare", "prctl", "chdir", "setgroups", "setgid", "setuid", "seccomp"]
    assert system.calls.count("setrlimit") == 6
    assert identity == {"uid": 901, "gid": 902, "groups": [], "capabilities": 0, "sandboxed": True,
                        "no_new_privs": True, "no_processes": True, "seccomp": True}


def test_if_root_could_be_regained_the_reader_refuses_to_continue(monkeypatch):
    FakeSystem(monkeypatch, can_regain=True)
    with pytest.raises(RuntimeError, match="could be regained"):
        inspect_worker.lock_down(901, 902)


@pytest.mark.parametrize("kwargs", [{"capabilities": (1 << 21, 0)}, {"capabilities": (0, 1 << 5)}, {"leaves_groups": True}])
def test_leftover_capabilities_or_groups_are_caught(monkeypatch, kwargs):
    FakeSystem(monkeypatch, **kwargs)
    with pytest.raises(RuntimeError, match="still has rights"):
        inspect_worker.lock_down(901, 902)


@pytest.mark.parametrize("step", ["unshare", "setrlimit", "prctl", "seccomp"])
def test_a_lockdown_step_that_fails_stops_everything_before_the_file_is_opened(monkeypatch, step, tmp_path):
    FakeSystem(monkeypatch, fails_at=step)
    with pytest.raises(OSError):
        inspect_worker.lock_down(901, 902)


def test_a_reader_started_as_root_must_be_told_who_to_become(monkeypatch):
    FakeSystem(monkeypatch)
    for args in ((), (0, 0), (901, 0), (0, 902)):
        with pytest.raises(ValueError, match="must be told"):
            inspect_worker.lock_down(*args)


def test_main_never_opens_the_package_when_the_lockdown_fails(monkeypatch, tmp_path, capsys):
    pkg = builders.build_pkg(tmp_path, name="cygnus-test-pkg")
    opened = []
    import cygnus.core.detect as detect

    monkeypatch.setattr(detect, "detect_file", lambda p: opened.append(p))
    monkeypatch.setattr(inspect_worker, "lock_down", lambda *a: (_ for _ in ()).throw(OSError(1, "no")))
    assert inspect_worker.main(["x", str(pkg)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "sandbox_error" in out and opened == []


# -- the helper's side: the reader's answer is untrusted, too ------------------------------------------------------
GOOD_FACTS = {"name": "hello", "version": "1.0-1", "arch": "x86_64", "desc": "d", "depends": ["glibc"],
              "conflicts": [], "provides": [], "replaces": [], "has_scriptlet": False}
READER = {"uid": 901, "gid": 902, "groups": [], "capabilities": 0, "sandboxed": True, "no_new_privs": True,
          "no_processes": True, "seccomp": True}  # (what the account "cygnus-reader" looks like in these tests)


def _reader_says(monkeypatch, payload, *, root=False, account=True):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    seen = []
    monkeypatch.setattr("cygnus.core.util.proc.run", lambda argv, **kw: seen.append(argv) or NS(stdout=text, stderr="",
                                                                                                returncode=0))
    monkeypatch.setattr(actions.os, "geteuid", lambda: 0 if root else 1000)
    if account:
        monkeypatch.setattr(actions.pwd, "getpwnam", lambda name: NS(pw_uid=901, pw_gid=902))
    else:
        monkeypatch.setattr(actions.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr("cygnus.helper.seccomp.available", lambda: True)
    return seen


def test_a_good_answer_from_a_reader_that_was_the_unprivileged_account_is_accepted_when_the_helper_is_root(monkeypatch):
    _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": READER}, root=True)
    facts = actions.inspect_package("/staging/x.pkg.tar")
    assert facts["name"] == "hello" and facts["reader_uid"] == 901


@pytest.mark.parametrize("identity", [
    {"uid": 0, "sandboxed": False}, {}, {**READER, "uid": 0}, {**READER, "capabilities": 1},
    {**READER, "groups": [0]}, {**READER, "sandboxed": False}, {**READER, "uid": 1000}, {**READER, "uid": 65534},
    {**READER, "gid": 0}, {**READER, "gid": 65534}, {**READER, "no_new_privs": False}, {**READER, "no_processes": False},
    {**READER, "seccomp": False}, {k: v for k, v in READER.items() if k != "seccomp"},
])
def test_a_reader_that_did_not_give_up_root_is_refused_by_a_root_helper(monkeypatch, identity):
    _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": identity}, root=True)
    with pytest.raises(actions.PlanRefused, match="did not give up administrator rights"):
        actions.inspect_package("/staging/x.pkg.tar")


def test_the_reader_is_told_exactly_which_account_to_become(monkeypatch):
    seen = _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": READER}, root=True)
    actions.inspect_package("/staging/x.pkg.tar")
    assert seen[0][-3:] == ["/staging/x.pkg.tar", "901", "902"]


def test_without_the_reader_account_nothing_is_read(monkeypatch):
    seen = _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": READER}, root=True, account=False)
    with pytest.raises(actions.PlanRefused, match="'cygnus-reader'.*does not exist; reinstall Cygnus"):
        actions.inspect_package("/staging/x.pkg.tar")
    assert seen == []  # the reader was never even started


@pytest.mark.parametrize("ids", [(0, 902), (901, 0)])
def test_an_account_with_administrator_ids_is_refused(monkeypatch, ids):
    seen = _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": READER}, root=True)
    monkeypatch.setattr(actions.pwd, "getpwnam", lambda name: NS(pw_uid=ids[0], pw_gid=ids[1]))
    with pytest.raises(actions.PlanRefused, match="has administrator rights"):
        actions.inspect_package("/staging/x.pkg.tar")
    assert seen == []


def test_on_another_architecture_the_missing_filter_is_reported_not_demanded(monkeypatch):
    _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": {**READER, "seccomp": False}}, root=True)
    monkeypatch.setattr("cygnus.helper.seccomp.available", lambda: False)
    assert actions.inspect_package("/staging/x.pkg.tar")["name"] == "hello"


def test_a_refusal_for_a_file_that_is_not_a_package_does_not_show_internal_names(monkeypatch):
    _reader_says(monkeypatch, {"ok": False, "error": "DetectionError: 1d6cbad9e11e0988.pkg.tar: unrecognised file type"})
    with pytest.raises(actions.PlanRefused) as caught:
        actions.inspect_package("/var/cache/cygnus/staging/1d6cbad9e11e0988.pkg.tar")
    assert str(caught.value) == "pacman cannot read this package: unrecognised file type"


def test_a_refusal_that_names_the_file_in_the_middle_never_shows_the_staged_name(monkeypatch):
    _reader_says(monkeypatch, {"ok": False, "error": "error: could not open /var/cache/cygnus/staging/ab12.pkg.tar"})
    with pytest.raises(actions.PlanRefused) as caught:
        actions.inspect_package("/var/cache/cygnus/staging/ab12.pkg.tar")
    assert "ab12" not in str(caught.value) and "the file" in str(caught.value)


def test_a_link_instead_of_a_file_is_refused_in_plain_words(tmp_path):
    from cygnus.core.errors import CygnusError
    from cygnus.core.util.fs import open_regular

    (tmp_path / "real.pkg.tar.zst").write_bytes(b"x")
    (tmp_path / "link.pkg.tar.zst").symlink_to(tmp_path / "real.pkg.tar.zst")
    with pytest.raises(CygnusError, match="is a link to another file, not the file itself"):
        open_regular(tmp_path / "link.pkg.tar.zst", follow_symlinks=False)
    os.close(open_regular(tmp_path / "link.pkg.tar.zst"))  # following links stays possible where it is meant


def test_without_root_there_is_nothing_to_give_up(monkeypatch):
    _reader_says(monkeypatch, {"ok": True, "facts": GOOD_FACTS, "identity": {"uid": 1000, "sandboxed": False}})
    assert actions.inspect_package("/staging/x.pkg.tar")["name"] == "hello"


@pytest.mark.parametrize("payload, message", [
    ("not json at all", "crashed or was killed"), ("[1, 2]", "nonsense"), ("", "crashed or was killed"),
    ({"sandbox_error": "OSError: unshare failed"}, "could not lock itself down"),
    ({"ok": False, "refused": "only Arch packages (.pkg.tar.*) can be installed from a file", "identity": READER},
     "only Arch packages"),
    ({"ok": False, "error": "ArchiveError: truncated", "identity": READER}, "pacman cannot read this package"),
    ({"ok": True, "facts": "text", "identity": READER}, "nonsense"),
    ({"ok": True, "facts": {**GOOD_FACTS, "name": 5}, "identity": READER}, "bad name"),
    ({"ok": True, "facts": {**GOOD_FACTS, "version": "v" * 500}, "identity": READER}, "bad version"),
    ({"ok": True, "facts": {**GOOD_FACTS, "depends": "glibc"}, "identity": READER}, "bad depends"),
    ({"ok": True, "facts": {**GOOD_FACTS, "depends": [1]}, "identity": READER}, "bad depends"),
    ({"ok": True, "facts": {**GOOD_FACTS, "provides": ["p"] * 5000}, "identity": READER}, "bad provides"),
    ({"ok": True, "facts": {**GOOD_FACTS, "replaces": ["r" * 900]}, "identity": READER}, "bad replaces"),
    ({"ok": True, "facts": {**GOOD_FACTS, "has_scriptlet": "no"}, "identity": READER}, "bad has_scriptlet"),
    ({"ok": True, "facts": {k: v for k, v in GOOD_FACTS.items() if k != "arch"}, "identity": READER}, "bad arch"),
])
def test_whatever_the_reader_answers_is_checked_like_input_from_a_stranger(monkeypatch, payload, message):
    _reader_says(monkeypatch, payload, root=True)
    with pytest.raises(actions.PlanRefused, match=message):
        actions.inspect_package("/staging/x.pkg.tar")


def test_a_reader_that_cannot_even_start_or_runs_too_long_is_a_refusal(monkeypatch):
    def boom(argv, **kw):
        raise TimeoutError("took too long")

    monkeypatch.setattr("cygnus.core.util.proc.run", boom)
    with pytest.raises(actions.PlanRefused, match="could not be read"):
        actions.inspect_package("/staging/x.pkg.tar")


# -- the staged file ----------------------------------------------------------------------------------------------
def test_the_staged_file_can_be_read_by_the_reader_but_not_changed_by_anyone_else(tmp_path):
    import hashlib
    import stat

    pkg = builders.build_pkg(tmp_path, name="cygnus-test-pkg")
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    with open(pkg, "rb") as fh:
        staged, _ = actions.stage_local_package(fh.fileno(), digest, tmp_path / "staging")
    assert stat.S_IMODE(staged.stat().st_mode) == 0o644  # others may read it, nobody but its owner may change it
    assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o711  # others may enter the folder, not list or change it
    assert staged.stat().st_uid == os.getuid()  # owned by the helper's user (root in production), not by the reader account
    assert actions.inspect_package(str(staged))["name"] == "cygnus-test-pkg"


def test_a_staging_folder_left_over_with_a_tighter_mode_is_opened_up_for_the_reader(tmp_path):
    import hashlib
    import stat

    pkg = builders.build_pkg(tmp_path, name="cygnus-test-pkg")
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    staging = tmp_path / "staging"
    staging.mkdir()
    staging.chmod(0o700)  # what release 13 made: mkdir(exist_ok=True, mode=...) would leave it like this
    with open(pkg, "rb") as fh:
        staged, _ = actions.stage_local_package(fh.fileno(), digest, staging)
    assert stat.S_IMODE(staging.stat().st_mode) == 0o711 and stat.S_IMODE(staged.stat().st_mode) == 0o644


# -- the real lockdown, with real system calls, inside a user namespace ------------------------------------------
def _userns_available() -> bool:
    try:
        out = subprocess.run(["unshare", "--map-users=auto", "--map-groups=auto", "--setuid=0", "--setgid=0", "--",
                              "python3", "-c", "import os; print(os.getuid(), len(open('/proc/self/uid_map').read()))"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.split()[:1] == ["0"]


@pytest.fixture(scope="module")
def shared_tmp():
    """A folder the namespace's users can enter (pytest's own temporary folders are private to this user)."""
    import shutil
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="cygnus-ns-test-", dir="/tmp"))
    folder.chmod(0o755)
    shutil.copytree(Path(__file__).resolve().parent.parent / "cygnus", folder / "code" / "cygnus",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for sub in (folder / "code", folder / "pkgs"):
        sub.mkdir(exist_ok=True)
        sub.chmod(0o755)
    (folder / "scratch").mkdir()
    (folder / "scratch").chmod(0o1777)  # like /tmp: every user may create files, only the owner may delete them
    yield folder
    try:  # what the probes made inside the namespace belongs to the namespace's users, not to this one
        _in_namespace("rm", "-rf", str(folder / "scratch" / "rootdir"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    shutil.rmtree(folder, ignore_errors=True)


def _in_namespace(*argv):
    return subprocess.run(["unshare", "--map-users=auto", "--map-groups=auto", "--setuid=0", "--setgid=0", "--", *argv],
                          capture_output=True, text=True, timeout=120)


READER_IDS = ("65534", "65534")  # any ids inside the namespace's mapped range will do

# Run twice, in two separate processes: once as the namespace's root WITHOUT the lockdown (the control: every probe must
# work, or the probe proves nothing) and once after it. Only probes that flip from allowed to refused count.
PROBE = r"""
import json, os, socket, sys
mode, code, scratch, port, unix_path = sys.argv[1:6]
sys.path.insert(0, code)
sys.dont_write_bytecode = True
from cygnus.helper import inspect_worker

if mode == "locked":
    identity = inspect_worker.lock_down(65534, 65534)
else:
    identity = None
    # things only the control's own user can reach: the locked process, another user, must fail to use them
    with open(os.path.join(scratch, "secret"), "w") as fh:
        fh.write("secret")
    os.chmod(os.path.join(scratch, "secret"), 0o600)
    os.mkdir(os.path.join(scratch, "rootdir"), 0o755)

def allowed(fn):
    try:
        fn()
    except OSError:
        return False
    return True

def tcp_loopback():
    socket.create_connection(("127.0.0.1", int(port)), timeout=2).close()

def unix_socket():
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(unix_path)
    finally:
        s.close()

def fork():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)

def hostname():
    if mode == "control":
        os.unshare(os.CLONE_NEWUTS)  # the control owns a hostname of its own, so only the lockdown can be what stops it
    socket.sethostname("cygnus-probe")

def read_secret():
    open(os.path.join(scratch, "secret")).read()

def write_dir():
    open(os.path.join(scratch, "rootdir", "f"), "w").write("x")

def big_write():
    written = 0
    try:
        with open(os.path.join(scratch, mode + "-big"), "wb") as fh:
            for _ in range(3):
                fh.write(b"x" * 1024 * 1024)
                fh.flush()
                written += 1024 * 1024
    except OSError:
        pass
    return os.path.getsize(os.path.join(scratch, mode + "-big")) if os.path.exists(os.path.join(scratch, mode + "-big")) else 0

results = {
    "tcp_loopback": allowed(tcp_loopback), "unix_socket": allowed(unix_socket), "fork": allowed(fork),
    "regain_root": allowed(lambda: os.setuid(0)), "hostname": allowed(hostname), "read_secret": allowed(read_secret),
    "write_dir": allowed(write_dir),
    # last: it changes who the process is
    "new_user_namespace": allowed(lambda: os.unshare(os.CLONE_NEWUSER)),
}
print(json.dumps({"identity": identity, "allowed": results, "bytes_written": big_write() if mode == "locked" else None}))
"""


def _listeners(folder):
    import socket

    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(8)
    unix_path = folder / "scratch" / "service.sock"
    unix = socket.socket(socket.AF_UNIX)
    unix.bind(str(unix_path))
    unix_path.chmod(0o666)  # like the system bus: anyone may connect
    unix.listen(8)
    return tcp, unix, unix_path


@pytest.mark.needs_tool("unshare")
def test_the_real_reader_really_gives_up_everything_a_hostile_file_would_want(shared_tmp):
    if not _userns_available():
        pytest.skip("this machine does not allow a user namespace with a mapped ID range")
    probe = shared_tmp / "probe.py"
    probe.write_text(PROBE)
    probe.chmod(0o644)
    tcp, unix, unix_path = _listeners(shared_tmp)
    try:
        def run(mode):
            out = _in_namespace("python3", "-B", "-I", str(probe), mode, str(shared_tmp / "code"),
                                str(shared_tmp / "scratch"), str(tcp.getsockname()[1]), str(unix_path))
            assert out.returncode == 0, out.stderr
            return json.loads(out.stdout)

        control, locked = run("control"), run("locked")
    finally:
        tcp.close()
        unix.close()
    assert locked["identity"]["uid"] == 65534 and locked["identity"]["capabilities"] == 0
    # a probe that fails in the control proves nothing, so the control must be able to do every one of them
    assert all(control["allowed"].values()), control["allowed"]
    flipped = {name for name, ok in locked["allowed"].items() if not ok}
    assert flipped == set(control["allowed"]), {n: locked["allowed"][n] for n in control["allowed"] if n not in flipped}
    assert locked["bytes_written"] == inspect_worker.WRITE_LIMIT  # it could start a write, and the limit stopped it


@pytest.mark.needs_tool("unshare")
def test_the_real_reader_reads_a_package_as_the_unprivileged_account_it_was_told_to_be(shared_tmp):
    if not _userns_available():
        pytest.skip("this machine does not allow a user namespace with a mapped ID range")
    pkg = builders.build_pkg(shared_tmp / "pkgs", name="cygnus-test-pkg", install_script="post_install() { :; }\n")
    pkg.chmod(0o644)
    worker = shared_tmp / "code" / "cygnus" / "helper" / "inspect_worker.py"
    out = _in_namespace("python3", "-B", "-I", str(worker), str(pkg), *READER_IDS)
    data = json.loads(out.stdout)
    assert out.returncode == 0 and data["ok"] is True and data["facts"]["name"] == "cygnus-test-pkg", out.stdout
    assert data["identity"] == {"uid": 65534, "gid": 65534, "groups": [], "capabilities": 0, "sandboxed": True,
                                "no_new_privs": True, "no_processes": True, "seccomp": seccomp.available()}
    # without being told who to become, a root reader opens nothing
    out = _in_namespace("python3", "-B", "-I", str(worker), str(pkg))
    data = json.loads(out.stdout)
    assert out.returncode == 1 and "must be told" in data["sandbox_error"] and "facts" not in data
