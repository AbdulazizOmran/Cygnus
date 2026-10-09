import os

import pytest

from cygnus.core.health import probes
from cygnus.core.health.engine import Health, InstallContext, evaluate, render_text
from cygnus.core.health.probes import ProbeOutcome, ProbeStatus
from cygnus.core.manifest import catalog

WP = catalog.bundled_manifests()["org.whatpulse.WhatPulse"].manifest


def fake_probe(results):
    """results: {(probe, key): status} with key = group/unit/port/id/name; default PASS."""
    def run(spec):
        key = spec.get("group") or spec.get("unit") or spec.get("port") or spec.get("id") or spec.get("name") \
            or spec.get("path") or spec.get("key")
        status = results.get((spec["probe"], key), ProbeStatus.PASS)
        facts = {"relogin_required": True} if status == "relogin" else {}
        if status == "relogin":
            status = ProbeStatus.FAIL
        return ProbeOutcome(probe=spec["probe"], status=status, evidence=f"{spec['probe']}:{key}:{status}",
                            facts=facts)
    return run


@pytest.fixture
def appimage(tmp_path):
    p = tmp_path / "WhatPulse.AppImage"
    p.write_bytes(b"\x7fELF")
    p.chmod(0o755)
    return InstallContext(source_id="appimage", format="appimage", payload_path=str(p))


def test_all_good(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({}))
    assert h.overall is Health.OK and not h.issues
    assert all(f.status is Health.OK for f in h.features)


def test_missing_input_group(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({("group_membership", "input"): ProbeStatus.FAIL,
                                                 ("readable", "/dev/input/event*"): ProbeStatus.FAIL}))
    feats = {f.id: f.status for f in h.features}
    assert feats["keyboard"] is Health.MISSING_COMPONENT and feats["mouse"] is Health.MISSING_COMPONENT
    assert feats["core"] is Health.OK and h.overall is Health.MISSING_COMPONENT
    [issue] = [i for i in h.issues if i.code == "PERM_GROUP_MISSING"]
    [res] = issue.resolutions
    assert res.actions[0].kind == "group.add_user" and res.actions[0].params == {"group": "input"}
    assert res.privilege.value == "root" and res.safety.value == "approval"
    assert "log out" in res.explanation
    assert issue.facts["discouraged_vendor_instructions"]  # the vendor script is quoted, never run


def test_relogin_required(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({("group_membership", "input"): "relogin"}))
    [issue] = [i for i in h.issues if i.code == "PERM_GROUP_MISSING"]
    assert issue.resolutions[0].id == "relogin" and not issue.resolutions[0].actions


def test_service_installed_but_stopped_only_enables(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({
        ("systemd_unit", "whatpulse-pcap-service.service"): ProbeStatus.FAIL,
        ("journal_recent_match", "whatpulse-pcap-service.service"): ProbeStatus.FAIL,
        ("tcp_peer", 3499): ProbeStatus.FAIL}))
    [issue] = [i for i in h.issues if i.facts["component"] == "pcap-service"]
    kinds = [a.kind for a in issue.resolutions[0].actions]
    assert kinds == ["systemd.enable_now"]  # the package is installed: do not reinstall it


def test_service_missing_installs_then_enables(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({
        ("pacman_installed", "whatpulse-pcap-service"): ProbeStatus.FAIL,
        ("systemd_unit", "whatpulse-pcap-service.service"): ProbeStatus.FAIL}))
    [issue] = [i for i in h.issues if i.facts["component"] == "pcap-service"]
    actions = issue.resolutions[0].actions
    assert [a.kind for a in actions] == ["pacman.install_local", "systemd.enable_now"]
    assert actions[0].params["sha256"].startswith("54fe5e87")


def test_optional_feature_does_not_degrade_overall(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({
        ("browser_extension_present", "fnfhoihlmikplapbgegdmpifhgmaigbf"): ProbeStatus.FAIL,
        ("browser_extension_present", "webinsights@whatpulse.org"): ProbeStatus.FAIL}))
    web = next(f for f in h.features if f.id == "web")
    assert web.status is Health.OPTIONAL_UNAVAILABLE
    assert h.overall is Health.OPTIONAL_UNAVAILABLE


def test_extension_in_any_browser_is_enough(appimage):
    h = evaluate(WP, appimage, probe=fake_probe({
        ("browser_extension_present", "fnfhoihlmikplapbgegdmpifhgmaigbf"): ProbeStatus.FAIL}))
    assert next(f for f in h.features if f.id == "web").status is Health.OK


def test_missing_payload_is_broken(tmp_path):
    ctx = InstallContext(source_id="appimage", format="appimage", payload_path=str(tmp_path / "gone"))
    h = evaluate(WP, ctx, probe=fake_probe({}))
    assert h.overall is Health.BROKEN


def test_offline_is_not_broken():
    ctx = InstallContext(source_id="appimage", format="appimage", payload_path="/nope", location_online=False)
    h = evaluate(WP, ctx, probe=fake_probe({}))
    assert h.overall is Health.OFFLINE and all(f.status is Health.OFFLINE for f in h.features)


def test_render_text(appimage):
    out = render_text(evaluate(WP, appimage, probe=fake_probe({("group_membership", "input"): ProbeStatus.FAIL})))
    assert "Keyboard tracking" in out and "⚠" in out


# -- individual probes ----------------------------------------------------------------------------
def test_group_probe_states():
    gid = os.getgroups()[0]
    import grp

    name = grp.getgrgid(gid).gr_name
    assert probes.group_membership(name).status is ProbeStatus.PASS
    assert probes.group_membership("no-such-group-xyz").status is ProbeStatus.FAIL
    out = probes.group_membership(name, session_gids=[])
    assert out.status is ProbeStatus.FAIL


def test_readable_refuses_home_paths():
    assert probes.readable(os.path.expanduser("~/.ssh/*")).status is ProbeStatus.UNKNOWN


def test_tcp_parsing(tmp_path):
    net = tmp_path / "net"
    net.mkdir()
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (net / "tcp").write_text(
        header
        + "   0: 0100007F:0DAB 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 111 1\n"
        + "   1: 0100007F:0DAB 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 222 1\n")
    socks = probes.tcp_sockets(str(tmp_path))
    assert {s["state"] for s in socks} == {"LISTEN", "ESTABLISHED"}
    assert probes.tcp_listener(3499, sockets=socks).status is ProbeStatus.PASS
    assert probes.tcp_peer(3499, sockets=socks).status is ProbeStatus.PASS
    assert probes.tcp_listener(3488, sockets=socks).status is ProbeStatus.FAIL


def test_browser_extension_probe(tmp_path):
    prof = tmp_path / ".config/net.imput.helium/Default"
    good, other = "a" * 32, "b" * 32
    (prof / f"Extensions/{good}").mkdir(parents=True)
    (prof / "Preferences").write_text("{}")
    assert probes.browser_extension_present("chromium", good, home=tmp_path).status is ProbeStatus.PASS
    assert probes.browser_extension_present("chromium", other, home=tmp_path).status is ProbeStatus.FAIL
    assert probes.browser_extension_present("chromium", "/etc", home=tmp_path).status is ProbeStatus.UNKNOWN
    ff = tmp_path / ".config/mozilla/firefox/x.default"
    ff.mkdir(parents=True)
    (ff / "prefs.js").write_text("")
    (ff / "extensions.json").write_text('{"addons": [{"id": "a@b", "active": true}]}')
    assert probes.browser_extension_present("firefox", "a@b", home=tmp_path).status is ProbeStatus.PASS


def test_run_probe_unknown_type_is_unknown():
    assert probes.run_probe({"probe": "exec", "cmd": "id"}).status is ProbeStatus.UNKNOWN


@pytest.mark.parametrize("pattern", ["/proc/self/root/etc/*", "/proc/self/cwd/*", "/proc/1/root", "/proc/self/r*/etc",
                                     "/proc/*/root/*", "/dev/fd/0", "/dev/../etc/passwd", "/etc/shadow"])
def test_readable_probe_never_leaves_dev_sys_and_proc(pattern):
    from cygnus.core.health import probes

    out = probes.readable(pattern)
    assert out.status.value == "unknown" and "refusing" in out.evidence


def test_readable_probe_still_reads_what_it_may():
    from cygnus.core.health import probes

    assert probes.readable("/proc/self/status").status.value == "pass"


def _listen(addr, port=3499):
    return [{"state": "LISTEN", "laddr": addr, "lport": port, "raddr": "0.0.0.0", "rport": 0, "inode": 1}]


@pytest.mark.parametrize("bound, wanted, ok", [
    ("127.0.0.1", "127.0.0.1", True),
    ("::ffff:127.0.0.1", "127.0.0.1", True),         # a v4-mapped bind on a tcp6 socket
    ("0.0.0.0", "127.0.0.1", True),
    ("::", "127.0.0.1", True),                       # dual-stack wildcard
    ("::", "::1", True),
    ("::1", "::1", True),
    ("0.0.0.0", "::1", False),                       # an IPv4 listener is not reachable over IPv6
    ("0.0.0.0", "::", False),
    ("127.0.0.1", "0.0.0.0", False),                 # loopback-only is not "all interfaces"
    ("::1", "::", False),
    ("127.0.0.1", "::1", False),
    ("::1", "127.0.0.1", False),
    ("::", "0.0.0.0", True),
    ("0.0.0.0", "0.0.0.0", True),
])
def test_tcp_listener_matches_address_family_and_binding(bound, wanted, ok):
    status = probes.tcp_listener(3499, addr=wanted, sockets=_listen(bound)).status
    assert status is (ProbeStatus.PASS if ok else ProbeStatus.FAIL)


def test_tcp_peer_needs_the_same_address():
    conn = [{"state": "ESTABLISHED", "laddr": "::1", "lport": 3499, "raddr": "::1", "rport": 40000, "inode": 2}]
    assert probes.tcp_peer(3499, addr="::1", sockets=conn).status is ProbeStatus.PASS
    assert probes.tcp_peer(3499, addr="127.0.0.1", sockets=conn).status is ProbeStatus.FAIL


def _wp_core_requiring(*component_ids):
    import json

    from cygnus.core.manifest.schema import Manifest
    raw = json.loads((catalog.resources.files("cygnus.data") / "manifests/org.whatpulse.WhatPulse.json").read_text())
    next(f for f in raw["features"] if f.get("core"))["requires"] = list(component_ids)
    return Manifest.model_validate(raw)


def _core(health):
    return next(f for f in health.features if f.id == "core")


def test_a_failing_optional_component_does_not_turn_the_core_unknown(appimage):
    """Spec 6.4(2): the core takes the app's own state; an optional part that definitely failed is only an issue."""
    manifest = _wp_core_requiring("web-insights")
    passing = fake_probe({})

    def probe(spec):
        if spec["probe"] == "browser_extension_present":
            return ProbeOutcome(probe=spec["probe"], status=ProbeStatus.FAIL, evidence="extension not found")
        return passing(spec)

    h = evaluate(manifest, appimage, probe=probe)
    assert _core(h).status is Health.OK
    assert any(i.code == "BROWSER_EXT_MISSING" for i in h.issues)


def test_core_with_a_definitely_missing_hard_component_is_broken(appimage):
    h = evaluate(_wp_core_requiring("input-access"), appimage,
                 probe=fake_probe({("group_membership", "input"): ProbeStatus.FAIL}))
    assert _core(h).status is Health.BROKEN


def test_core_with_unobtainable_evidence_stays_unknown_not_ok_or_broken(appimage):
    h = evaluate(_wp_core_requiring("input-access"), appimage,
                 probe=fake_probe({("group_membership", "input"): ProbeStatus.UNKNOWN}))
    assert _core(h).status is Health.UNKNOWN


@pytest.mark.parametrize("file_state, expect_ok", [("enabled", True), ("enabled-runtime", True),
                                                  ("static", False), ("alias", False), ("disabled", False)])
def test_systemd_enabled_means_enabled(monkeypatch, file_state, expect_ok):
    from cygnus.core.util import proc

    out = (f"LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState={file_state}\n")
    monkeypatch.setattr(probes.proc, "run", lambda argv, **kw: proc.Result(tuple(argv), 0, out, ""))
    assert (probes.systemd_unit("x.service").status is ProbeStatus.PASS) is expect_ok
    # a unit that is only expected to be running does not care how it gets started
    assert probes.systemd_unit("x.service", expect=["active"]).status is ProbeStatus.PASS


@pytest.mark.parametrize("content, expected", [
    ("[]", ProbeStatus.UNKNOWN), ('"text"', ProbeStatus.UNKNOWN), ("null", ProbeStatus.UNKNOWN),
    ('{"addons": null}', ProbeStatus.UNKNOWN), ('{"addons": {"id": "a@b"}}', ProbeStatus.UNKNOWN),
    ("{not json", ProbeStatus.UNKNOWN), ("", ProbeStatus.UNKNOWN),
    ('{"addons": ["a@b", 5, null]}', ProbeStatus.FAIL),
    ('{"addons": ["junk", {"id": "a@b"}]}', ProbeStatus.PASS),
    ('{"addons": [{"id": "a@b", "active": false}]}', ProbeStatus.FAIL),
])
def test_odd_firefox_extension_files_never_crash_the_probe(tmp_path, content, expected):
    ff = tmp_path / ".config/mozilla/firefox/x.default"
    ff.mkdir(parents=True)
    (ff / "prefs.js").write_text("")
    (ff / "extensions.json").write_text(content)
    assert probes.browser_extension_present("firefox", "a@b", home=tmp_path).status is expected


@pytest.mark.parametrize("status", [ProbeStatus.FAIL, ProbeStatus.UNKNOWN])
def test_a_soft_component_never_changes_the_cores_state(appimage, status):
    """Less evidence must not make the app look worse: a failing and an unknowable optional part agree."""
    passing = fake_probe({})

    def probe(spec):
        if spec["probe"] == "browser_extension_present":
            return ProbeOutcome(probe=spec["probe"], status=status, evidence="no data")
        return passing(spec)

    h = evaluate(_wp_core_requiring("web-insights"), appimage, probe=probe)
    assert _core(h).status is Health.OK


# -- review round 4: the readable probe ------------------------------------------------------------------
def test_a_pattern_with_many_wildcards_cannot_keep_the_probe_busy():
    import time

    started = time.monotonic()
    out = probes.readable("/sys/" + "*/" * 12 + "zz")
    assert time.monotonic() - started < 10
    assert out.status is ProbeStatus.UNKNOWN and "too long" in out.evidence


@pytest.mark.parametrize("pattern", ["/dev/./fd/0", "/dev//stdin", "/dev/f*", "/dev/std*", "/dev/*/0",
                                     "/dev/f[d]/0", "/dev/./stdout"])
def test_readable_judges_the_path_as_the_system_reads_it(pattern):
    out = probes.readable(pattern)
    assert out.status is ProbeStatus.UNKNOWN and "refusing" in out.evidence


def test_the_bounded_walker_finds_what_glob_finds():
    import glob

    for pattern in ("/dev/nul*", "/dev/n?ll", "/proc/self/stat*", "/proc/[0-9]*/stat", "/dev/null", "/dev/nothing-here"):
        assert sorted(probes._bounded_glob(pattern, [10**6])) == sorted(glob.glob(pattern)), pattern
    assert probes.readable("/dev/null").status is ProbeStatus.PASS
    assert probes.readable("/proc/self/status").status is ProbeStatus.PASS


# -- review round 4: fixes are decided per probe, and a failure with no fix is not called "working" ----------
def _component(post, verify, **extra):
    from cygnus.core.manifest.schema import Component

    return Component.model_validate({"id": "svc", "name": "Service", "type": "system-service", "relation": "hard",
                                     "post": post, "verify": verify, **extra})


def _outcomes(*pairs):
    return [ProbeOutcome(probe=name, status=status, evidence=ev, facts=facts) for name, status, ev, facts in pairs]


def _health(comp, outcomes):
    from cygnus.core.health.engine import ComponentHealth

    return ComponentHealth(component=comp, status=Health.MISSING_COMPONENT, outcomes=outcomes)


def test_one_unit_that_is_fine_does_not_hide_another_that_is_not():
    from cygnus.core.health.engine import component_issue

    unit = lambda u: {"probe": "systemd_unit", "unit": u, "scope": "system", "expect": ["enabled", "active"]}  # noqa: E731
    comp = _component([{"kind": "systemd.enable_now", "unit": "a.service"},
                       {"kind": "systemd.enable_now", "unit": "b.service"}], [unit("a.service"), unit("b.service")])
    issue = component_issue(_health(comp, _outcomes(("systemd_unit", ProbeStatus.PASS, "a is running", {}),
                                                    ("systemd_unit", ProbeStatus.FAIL, "b is not running", {}))),
                            None)
    [res] = issue.resolutions
    assert [a.params["unit"] for a in res.actions] == ["b.service"]
    assert issue.explanation == "b is not running"


def test_every_failing_probe_is_reported_not_just_the_last_of_its_kind():
    from cygnus.core.health.engine import component_issue

    comp = _component([], [{"probe": "pacman_installed", "name": "pkg-a"}, {"probe": "pacman_installed", "name": "pkg-b"}],
                      action={"kind": "pacman.install_repo", "names": ["pkg-a", "pkg-b"]})
    issue = component_issue(_health(comp, _outcomes(("pacman_installed", ProbeStatus.FAIL, "pkg-a is not installed", {}),
                                                    ("pacman_installed", ProbeStatus.FAIL, "pkg-b is not installed", {}))),
                            None)
    assert issue.explanation == "pkg-a is not installed; pkg-b is not installed"
    assert issue.resolutions and issue.resolutions[0].actions  # nothing installed: the install is offered


def test_a_package_that_is_installed_for_one_probe_but_not_another_is_still_offered():
    from cygnus.core.health.engine import component_issue

    comp = _component([], [{"probe": "pacman_installed", "name": "pkg-a"}, {"probe": "pacman_installed", "name": "pkg-b"}],
                      action={"kind": "pacman.install_repo", "names": ["pkg-a", "pkg-b"]})
    issue = component_issue(_health(comp, _outcomes(("pacman_installed", ProbeStatus.PASS, "pkg-a is installed", {}),
                                                    ("pacman_installed", ProbeStatus.FAIL, "pkg-b is not installed", {}))),
                            None)
    assert issue.resolutions and issue.resolutions[0].actions  # used to be hidden by pkg-a's PASS


def test_a_failing_component_without_a_fix_is_not_reported_as_working(monkeypatch):
    from cygnus.gui import fixes

    monkeypatch.setattr(fixes, "run_probe", lambda spec: ProbeOutcome(probe=spec["probe"], status=ProbeStatus.FAIL,
                                                                       evidence="user namespaces are disabled"))
    plan = fixes.plan("Helium", "userns-sandbox")
    assert plan["kind"] == "info" and "is working" not in plan["message"]
    assert "not working" in plan["message"] and "user namespaces are disabled" in plan["message"]
    assert "no automatic fix" in plan["message"]
    monkeypatch.setattr(fixes, "run_probe", lambda spec: ProbeOutcome(probe=spec["probe"], status=ProbeStatus.PASS,
                                                                       evidence="ok"))
    assert "is working" in fixes.plan("Helium", "userns-sandbox")["message"]


def test_what_a_component_can_do_is_shown_before_approval(monkeypatch):
    from cygnus.core import planner
    from cygnus.gui import fixes

    wp = catalog.bundled_manifests()["org.whatpulse.WhatPulse"]
    comps = planner._components_from_manifest(wp, "appimage", None)
    pcap = next(c for c in comps if c.id == "pcap-service")
    assert "What it can do: root-service, CAP_NET_RAW, CAP_NET_ADMIN." in pcap.note
    assert "What it can do" not in next(c for c in comps if c.id == "web-insights").note  # declares none

    class Client:
        def plan_packages(self, **kw):
            from cygnus.core.privilege import Plan
            return Plan("p", {}, "Install whatpulse-pcap-service")

        def plan_unit(self, unit, action):
            from cygnus.core.privilege import Plan
            return Plan("u", {}, "Enable the service")

    monkeypatch.setattr(fixes, "HelperClient", Client)
    monkeypatch.setattr(fixes, "run_probe", lambda spec: ProbeOutcome(probe=spec["probe"], status=ProbeStatus.FAIL,
                                                                       evidence="missing"))
    plan = fixes.plan("WhatPulse", "pcap-service")
    assert plan["kind"] == "helper" and "CAP_NET_RAW" in plan["security_note"]


def test_a_requirement_that_could_not_be_checked_is_not_hidden_behind_a_missing_extra(appimage):
    """Spec 6.4(3): hard UNKNOWN plus a recommended FAIL is unknown, not 'optional unavailable'."""
    import json

    from cygnus.core.manifest.schema import Manifest

    raw = json.loads((catalog.resources.files("cygnus.data") / "manifests/org.whatpulse.WhatPulse.json").read_text())
    for c in raw["components"]:
        if c["id"] == "web-insights":
            c["relation"] = "recommended"
    feature = next(f for f in raw["features"] if f["id"] == "network")
    feature["requires"] = ["pcap-service", "web-insights"]
    manifest = Manifest.model_validate(raw)

    def probe(spec):
        status = ProbeStatus.UNKNOWN if spec["probe"] == "systemd_unit" else (
            ProbeStatus.FAIL if spec["probe"] == "browser_extension_present" else ProbeStatus.PASS)
        return ProbeOutcome(probe=spec["probe"], status=status, evidence=f"{spec['probe']} {status.value}")

    h = evaluate(manifest, appimage, probe=probe)
    network = next(f for f in h.features if f.id == "network")
    assert network.status is Health.UNKNOWN and network.explanation.startswith("Could not check")


def test_a_chromium_extension_that_is_switched_off_is_not_present(tmp_path):
    prof = tmp_path / ".config/net.imput.helium/Default"
    good = "a" * 32
    (prof / f"Extensions/{good}").mkdir(parents=True)
    (prof / "Preferences").write_text('{"extensions": {"settings": {"%s": {"state": 1}}}}' % good)
    assert probes.browser_extension_present("chromium", good, home=tmp_path).status is ProbeStatus.PASS
    (prof / "Preferences").write_text('{"extensions": {"settings": {"%s": {"state": 0}}}}' % good)
    assert probes.browser_extension_present("chromium", good, home=tmp_path).status is ProbeStatus.FAIL
    (prof / "Preferences").write_text("{ broken")  # settings unreadable: its files are there
    assert probes.browser_extension_present("chromium", good, home=tmp_path).status is ProbeStatus.PASS
    (prof / "Preferences").write_text("{}")
    assert probes.browser_extension_present("chromium", good, home=tmp_path).status is ProbeStatus.PASS


def test_one_unreadable_firefox_profile_makes_the_answer_unknown_unless_another_has_it(tmp_path):
    for name, content in (("a.default", "{broken"), ("b.default", '{"addons": []}')):
        ff = tmp_path / ".config/mozilla/firefox" / name
        ff.mkdir(parents=True)
        (ff / "prefs.js").write_text("")
        (ff / "extensions.json").write_text(content)
    assert probes.browser_extension_present("firefox", "a@b", home=tmp_path).status is ProbeStatus.UNKNOWN
    (tmp_path / ".config/mozilla/firefox/b.default/extensions.json").write_text('{"addons": [{"id": "a@b"}]}')
    assert probes.browser_extension_present("firefox", "a@b", home=tmp_path).status is ProbeStatus.PASS


def test_a_fix_that_needs_an_action_cygnus_cannot_carry_out_is_unavailable_not_approximated():
    from cygnus.core.health.engine import component_issue

    probe = [{"probe": "systemd_unit", "unit": "a.service", "scope": "user", "expect": ["enabled"]}]
    for post in ({"kind": "systemd.user.enable_now", "unit": "a.service"},
                 {"kind": "flatpak.override", "app": "org.example.App", "permissions": ["--share=network"]},
                 {"kind": "aur.build", "pkgbase": "foo"}):
        comp = _component([post], probe)
        issue = component_issue(_health(comp, _outcomes(("systemd_unit", ProbeStatus.FAIL, "not enabled", {}))), None)
        assert issue.resolutions == [] and issue.facts["unavailable_actions"] == [post["kind"]]
    comp = _component([{"kind": "systemd.enable_now", "unit": "a.service"}], probe)
    issue = component_issue(_health(comp, _outcomes(("systemd_unit", ProbeStatus.FAIL, "not enabled", {}))), None)
    assert issue.resolutions and "unavailable_actions" not in issue.facts


def test_a_sysctl_setting_that_only_some_kernels_have_passes_where_it_is_missing(monkeypatch, tmp_path):
    assert probes.sysctl("kernel.no_such_setting_xyz", expect="1").status is ProbeStatus.UNKNOWN
    out = probes.sysctl("kernel.no_such_setting_xyz", expect="1", absent_ok=True)
    assert out.status is ProbeStatus.PASS and "nothing restricts it" in out.evidence
    # where it exists, absent_ok changes nothing
    monkeypatch.setattr(probes, "Path", lambda *a: tmp_path / "v")
    (tmp_path / "v").write_text("0\n")
    assert probes.sysctl("kernel.unprivileged_userns_clone", expect="1", absent_ok=True).status is ProbeStatus.FAIL


def test_helium_checks_both_user_namespace_settings():
    helium = catalog.bundled_manifests()["net.imput.helium"].manifest
    keys = [v.key for c in helium.components for v in c.verify if v.probe == "sysctl"]
    assert keys == ["user.max_user_namespaces", "kernel.unprivileged_userns_clone"]
