"""Optional features you do not want are no longer checked or suggested; essential ones cannot be dismissed."""

import pytest

from cygnus.core import preferences
from cygnus.core.errors import CygnusError
from cygnus.core.health.engine import Health, InstallContext, dismissible, evaluate
from cygnus.core.health.probes import ProbeOutcome, ProbeStatus
from cygnus.core.manifest import catalog
from cygnus.gui import service

WP = catalog.bundled_manifests()["org.whatpulse.WhatPulse"].manifest


def _probe(spec):
    # Everything works except the browser extension.
    ok = spec["probe"] != "browser_extension_present"
    return ProbeOutcome(probe=spec["probe"], status=ProbeStatus.PASS if ok else ProbeStatus.FAIL, evidence="x")


def _ctx(tmp_path):
    app = tmp_path / "wp.AppImage"
    app.write_text("x")
    app.chmod(0o755)
    return InstallContext(source_id="appimage", format="appimage", payload_path=str(app))


def test_only_optional_components_are_dismissible():
    assert dismissible(WP, "web-insights")
    assert not dismissible(WP, "input-access") and not dismissible(WP, "pcap-service")


def test_a_dismissed_feature_is_neither_checked_nor_suggested(tmp_path):
    before = evaluate(WP, _ctx(tmp_path), probe=_probe)
    assert before.overall is Health.OPTIONAL_UNAVAILABLE and any(i.code == "BROWSER_EXT_MISSING" for i in before.issues)
    probed = []

    def probe(spec):
        probed.append(spec["probe"])
        return _probe(spec)

    after = evaluate(WP, _ctx(tmp_path), probe=probe, dismissed={"web-insights"})
    assert after.overall is Health.OK and after.issues == []
    assert next(f for f in after.features if f.id == "web").status is Health.DISMISSED
    assert "browser_extension_present" not in probed


def test_essential_components_ignore_a_dismissal(tmp_path):
    def probe(spec):
        return ProbeOutcome(probe=spec["probe"], status=ProbeStatus.FAIL if spec["probe"] == "group_membership"
                            else ProbeStatus.PASS, evidence="x")

    h = evaluate(WP, _ctx(tmp_path), probe=probe, dismissed={"input-access"})  # e.g. a hand-edited file
    assert h.overall is Health.MISSING_COMPONENT


def test_dismiss_command_round_trip(capsys):
    from cygnus.cli.main import main

    assert main(["dismiss", "WhatPulse", "web-insights"]) == 0
    assert preferences.dismissed("org.whatpulse.WhatPulse") == {"web-insights"}
    with pytest.raises(CygnusError, match="needed by the application"):
        service.dismiss_component("WhatPulse", "input-access", True)
    assert main(["dismiss", "--undo", "WhatPulse", "web-insights"]) == 0
    assert preferences.dismissed("org.whatpulse.WhatPulse") == frozenset()


def test_a_fix_for_an_unsigned_appimage_says_its_identity_is_self_declared(tmp_path):
    from types import SimpleNamespace as NS

    from cygnus.core.registry import open_registry
    from cygnus.gui import fixes

    app = NS(id="org.example.Hello", name="Hello", homepage="https://hello.example", vendor=NS(name="Hello Inc", domain=None))
    loaded = NS(can_drive_actions=True, authentic=True, manifest=NS(application=app, sources=[]))
    reg = open_registry()
    assert fixes.identity_caution(loaded, reg) == ""  # not installed as an AppImage: nothing to say
    reg.conn.execute("INSERT INTO application(id, display_name) VALUES ('org.example.Hello', 'Hello')")
    reg.conn.execute("INSERT INTO installation(id, application_id, format, origin) "
                     "VALUES ('i1', 'org.example.Hello', 'appimage', 'adopted')")
    note = fixes.identity_caution(loaded, reg)
    assert "not signed" in note and "https://hello.example" in note


@pytest.mark.parametrize("raw, expected", [
    ('{"dismissed": null}', frozenset()),
    ('{"dismissed": ["org.whatpulse.WhatPulse"]}', frozenset()),
    ('{"dismissed": {"org.whatpulse.WhatPulse": "web-insights"}}', frozenset()),      # a string, not a list
    ('{"dismissed": {"org.whatpulse.WhatPulse": null}}', frozenset()),
    ('{"dismissed": {"org.whatpulse.WhatPulse": ["web-insights", 5, null]}}', {"web-insights"}),
    ('["not", "a", "table"]', frozenset()),
    ('not json', frozenset()),
])
def test_malformed_preferences_never_crash_and_keep_valid_entries(raw, expected):
    path = preferences._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    assert preferences.dismissed("org.whatpulse.WhatPulse") == expected


def test_a_bad_entry_does_not_cost_the_user_their_other_dismissals():
    path = preferences._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"dismissed": {"org.whatpulse.WhatPulse": ["web-insights"], "org.other.App": "oops"}}')
    preferences.set_dismissed("org.example.App", "extra", True)
    assert preferences.dismissed("org.whatpulse.WhatPulse") == {"web-insights"}
    assert preferences.dismissed("org.example.App") == {"extra"}
