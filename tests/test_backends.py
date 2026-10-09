from types import SimpleNamespace

import pytest

from cygnus.core.backends import flatpak as fb
from cygnus.core.backends import pacman as pm

CONF = """[options]
RootDir = /
DBPath = /var/lib/pacman/
CacheDir = /var/cache/pacman/pkg/
GPGDir = /etc/pacman.d/gnupg/
HoldPkg = pacman
Architecture = x86_64
Architecture = x86_64_v3
SigLevel = PackageRequired
[cachyos-v3]
Server = https://a.example/x86_64_v3/cachyos-v3
[extra]
Server = https://m.example/extra/os/x86_64
Server = https://n.example/extra/os/x86_64
"""


def test_parse_pacman_conf():
    c = pm.parse_pacman_conf(CONF)
    assert c.arch == ("x86_64", "x86_64_v3") and c.dbpath == "/var/lib/pacman"
    assert c.repos == ("cachyos-v3", "extra") and len(c.servers["extra"]) == 2


CFG = pm.parse_pacman_conf(CONF)


def make_worker(resolve=None, conflicts=(), info=None, satisfy=None, crash_on_resolve=False, outdated=(),
                local_conflicts=None):
    def worker(config, req):
        op = req["op"]
        if op == "outdated":
            return {"ok": True, "outdated": list(outdated)}
        if op == "local_conflicts":
            return local_conflicts or {"ok": True, "conflicts": [], "installed_version": None, "version_cmp": None}
        if op == "resolve":
            if crash_on_resolve:
                raise pm.WorkerCrash("segfault")
            return resolve
        if op == "conflicts":
            return {"ok": True, "conflicts": list(conflicts)}
        if op == "info":
            return {"ok": True, "packages": info or {n: {"sync": None, "local": None} for n in req["names"]}}
        if op == "satisfy":
            return {"ok": True, "satisfiers": satisfy}
    return worker


def pkg(name, version="1.0-1", installed=None, arch="x86_64"):
    return {"name": name, "version": version, "arch": arch, "repo": "extra", "download_size": 100,
            "installed_size": 300, "installed_version": installed}


def codes(a):
    return [i.code for i in a.issues]


def test_clean_install_plan():
    a = pm.analyse_install(["ncdu"], config=CFG, worker=make_worker({"ok": True, "to_add": [pkg("ncdu")],
                                                                      "to_remove": []}))
    assert not a.issues and not a.blocked and a.download_bytes == 100 and a.installed_bytes_delta == 300


def test_partial_upgrade_detected():
    a = pm.analyse_install(["app"], config=CFG, worker=make_worker(
        {"ok": True, "to_add": [pkg("app"), pkg("libfoo", "2.0-1", installed="1.0-1")], "to_remove": []}))
    assert codes(a) == ["PKG_PARTIAL_UPGRADE_RISK"] and a.blocked
    assert a.issues[0].preferred().id == "full-upgrade"


def test_newer_sync_db_than_system_is_partial_upgrade_risk():
    a = pm.analyse_install(["new-app"], config=CFG, worker=make_worker(
        {"ok": True, "to_add": [pkg("new-app")], "to_remove": []},
        outdated=[{"name": "libfoo", "installed": "1", "available": "2", "repo": "extra"}]))
    assert codes(a) == ["PKG_PARTIAL_UPGRADE_RISK"] and "newer than your installed system" in a.issues[0].explanation


def test_crash_during_sysupgrade_refuses_instead_of_partial_plan():
    a = pm.analyse_install(["x"], config=CFG, sysupgrade=True, worker=make_worker(crash_on_resolve=True))
    assert codes(a) == ["UNKNOWN_FAILURE"] and a.blocked


def test_protection_covers_kernels_firmware_and_holdpkg():
    assert pm.is_protected("linux-cachyos-bore") and pm.is_protected("linux-firmware-amdgpu")
    assert not pm.is_protected("linux-cachyos-headers") and not pm.is_protected("firefox")
    assert pm.is_protected("pacman", CFG)


def test_local_package_downgrade_and_conflict():
    from cygnus.core.models import Candidate, PackageFormat

    cand = Candidate(format=PackageFormat.LOCAL_PKG, source="x", name="svc", version="1-1", arch="x86_64",
                     metadata={"detached_signature": True})
    worker = make_worker(local_conflicts={"ok": True, "installed_version": "2-1", "version_cmp": -1,
                                          "conflicts": [{"installed": "other", "rule": "other", "declared_by": "svc"}]})
    found = {i.code for i in pm.analyse_local_package(cand, config=CFG, worker=worker)}
    assert found == {"PKG_DOWNGRADE", "PKG_CONFLICT"}


def test_sysupgrade_plan_has_no_partial_upgrade_issue():
    a = pm.analyse_install(["app"], config=CFG, sysupgrade=True, worker=make_worker(
        {"ok": True, "to_add": [pkg("app"), pkg("libfoo", "2.0-1", installed="1.0-1")], "to_remove": []}))
    assert "PKG_PARTIAL_UPGRADE_RISK" not in codes(a)


def test_not_found():
    a = pm.analyse_install(["zzz"], config=CFG, worker=make_worker(
        {"ok": False, "kind": "target_not_found", "error": {"data": ["zzz"]}}))
    assert codes(a) == ["PKG_NOT_FOUND"] and a.issues[0].preferred().id == "search-other-sources"


def test_unsatisfied_dependencies():
    a = pm.analyse_install(["x"], config=CFG, worker=make_worker(
        {"ok": False, "kind": "prepare_failed", "error": {"message": "could not satisfy dependencies",
                                                           "data": [["x", "libmissing>=2", None]]}}))
    assert codes(a) == ["PKG_DEP_UNRESOLVABLE"]
    assert a.issues[0].facts["unsatisfied"] == [{"target": "x", "dependency": "libmissing>=2"}]


def test_crash_falls_back_to_cli_and_metadata_conflicts(monkeypatch):
    monkeypatch.setattr(pm, "_cli_print_plan", lambda t: ([{"name": "dropbear-scp", "version": "1", "repo": "extra",
                                                            "download_size": 5}], None))
    worker = make_worker(crash_on_resolve=True, conflicts=[
        {"new": "dropbear-scp", "installed": "openssh", "rule": "openssh", "declared_by": "dropbear-scp"}])
    a = pm.analyse_install(["dropbear-scp"], config=CFG, worker=worker)
    assert a.method == "pacman-cli+metadata" and codes(a) == ["PKG_CONFLICT"]
    assert [r.id for r in a.issues[0].resolutions] == ["replace", "cancel"]


def test_protected_package_is_never_offered_for_replacement():
    worker = make_worker({"ok": True, "to_add": [pkg("evil-libc")], "to_remove": []}, conflicts=[
        {"new": "evil-libc", "installed": "glibc", "rule": "glibc", "declared_by": "evil-libc"}])
    a = pm.analyse_install(["evil-libc"], config=CFG, worker=worker)
    assert [r.id for r in a.issues[0].resolutions] == ["cancel"]


def test_arch_mismatch():
    a = pm.analyse_install(["x"], config=CFG, worker=make_worker(
        {"ok": True, "to_add": [pkg("x", arch="aarch64")], "to_remove": []}))
    assert "ARCH_INCOMPATIBLE" in codes(a)


def test_local_package_analysis():
    from cygnus.core.models import Candidate, PackageFormat

    cand = Candidate(format=PackageFormat.LOCAL_PKG, source="x.pkg.tar.zst", name="svc", version="1-1",
                     arch="x86_64", depends=["libpcap", "nothere"], metadata={"detached_signature": False})
    worker = make_worker(satisfy={"libpcap": {"installed": None, "repo": {"name": "libpcap", "repo": "core"}},
                                  "nothere": {"installed": None, "repo": None}},
                         local_conflicts={"ok": True, "conflicts": [], "installed_version": "0.9-1",
                                          "version_cmp": 1})
    found = {i.code for i in pm.analyse_local_package(cand, config=CFG, worker=worker)}
    assert found == {"PKG_DEP_UNRESOLVABLE", "PKG_DEP_MISSING", "PKG_UNSIGNED_LOCAL"}
    assert cand.metadata["installed_version"] == "0.9-1"


@pytest.mark.host
def test_real_worker_resolves_installed_package():
    """Read-only check against the real pacman databases (skipped if pacman is unavailable)."""
    import shutil

    if not shutil.which("pacman-conf"):
        pytest.skip("no pacman")
    a = pm.analyse_install(["pacman"], sysupgrade=False)
    assert not any(i.code == "UNKNOWN_FAILURE" for i in a.issues)


# -- Flatpak runtime algorithm with a fake environment -----------------------------------------------
class FakeEnv:
    def __init__(self, installed=(), installed_eol=None, remote=None, remote_urls=()):
        self.installed, self.installed_eol, self.remote = list(installed), installed_eol, remote
        self.remote_urls = remote_urls

    def runtime_status(self, runtime, query_remotes=True):
        st = fb.RuntimeStatus(ref=fb.RefParts.parse(runtime))
        st.installed_in, st.installed_eol = self.installed, self.installed_eol
        if self.remote is not None:
            st.remotes = {"flathub": self.remote}
        return st

    def remotes(self):
        return [(None, SimpleNamespace(get_url=lambda u=u: u)) for u in self.remote_urls]


RT = "org.freedesktop.Platform/x86_64/23.08"


def issues(env, **kw):
    return fb.analyse_runtime_requirement(RT, env, app_name="App", **kw)


def test_runtime_installed_and_supported():
    assert issues(FakeEnv(installed=["default"])) == []


def test_runtime_missing_available_supported():
    [i] = issues(FakeEnv(remote={"available": True, "download_size": 300 * 2**20}))
    assert i.code == "FP_RUNTIME_MISSING" and i.preferred().safety.value == "approval"


def test_runtime_eol_never_auto_and_prefers_alternatives():
    alts = [{"id": "appimage", "format_label": "AppImage"}]
    [i] = issues(FakeEnv(remote={"available": True, "eol": "EOL!", "download_size": 1}), alternatives=alts)
    assert i.code == "FP_RUNTIME_EOL" and i.severity.value == "blocker"
    assert all(r.safety.value != "auto" for r in i.resolutions)
    assert i.preferred().id == "alt-appimage"
    assert any(r.id == "install-eol-runtime" and not r.recommended for r in i.resolutions)


def test_newer_build_preferred_over_eol_runtime():
    newer = {"version": "7.0", "runtime": "org.freedesktop.Platform/x86_64/25.08", "url": "https://x/y.flatpak"}
    [i] = issues(FakeEnv(remote={"available": True, "eol": "EOL"}), newer_build=newer,
                 alternatives=[{"id": "appimage", "format_label": "AppImage"}])
    assert i.preferred().id == "newer-build"


def test_runtime_unavailable_is_not_recoverable_without_alternatives():
    [i] = issues(FakeEnv(remote={"available": False}))
    assert i.code == "FP_RUNTIME_UNAVAILABLE" and not i.recoverable


def test_missing_remote_from_runtime_repo():
    [i] = issues(FakeEnv(remote={"available": False}), runtime_repo="https://dl.example/x.flatpakrepo")
    assert i.code == "FP_REMOTE_MISSING"
    # a repository named by a downloaded file is never added by Cygnus: the suggestion only explains
    assert all(not r.actions for r in i.resolutions) and "https://dl.example/x.flatpakrepo" in i.resolutions[-1].explanation


def test_installed_eol_runtime_is_degraded():
    [i] = issues(FakeEnv(installed=["default"], installed_eol="EOL"))
    assert i.code == "FP_RUNTIME_EOL" and i.severity.value == "degraded"


def test_refparts():
    r = fb.RefParts.parse("app/org.x.Y/x86_64/stable")
    assert (r.kind, r.name, r.arch, r.branch) == ("app", "org.x.Y", "x86_64", "stable") and str(r) == "app/org.x.Y/x86_64/stable"
    with pytest.raises(ValueError):
        fb.RefParts.parse("org.x.Y")


def test_failed_up_to_date_check_blocks_instead_of_allowing_a_partial_upgrade():
    base = make_worker({"ok": True, "to_add": [pkg("new-app")], "to_remove": []})

    def worker(config, req):
        if req["op"] == "outdated":
            raise pm.WorkerCrash("libalpm worker died (exit -11)")
        return base(config, req)

    a = pm.analyse_install(["new-app"], config=CFG, worker=worker)
    assert codes(a) == ["PKG_PARTIAL_UPGRADE_RISK"] and a.blocked
    assert "could not check whether your system is up to date" in a.issues[0].explanation
