import pytest

import builders
from cygnus.core import inventory, planner
from cygnus.core.backends import appimage as ab
from cygnus.core.backends import aur, foreign, sources
from cygnus.core.detect import detect_file
from cygnus.core.manifest import catalog
from cygnus.core.recovery.model import SafetyClass
from cygnus.core.registry.db import StorageLocation

CAT = catalog.bundled_manifests()
WP = CAT["org.whatpulse.WhatPulse"]
SSD = StorageLocation(id="ssd", label="SSD", fs_uuid="a", fs_type="btrfs", location_class="system")
GOOD_CAPS = {"exec_allowed": True, "chmod_persists": True, "symlinks": True, "hardlinks": True,
             "atomic_rename": True, "ostree_bare_user_only": True}
HDD = StorageLocation(id="hdd", label="HDD", fs_uuid="b", fs_type="ntfs3", location_class="user-owned",
                      capabilities=GOOD_CAPS)


# -- AUR ------------------------------------------------------------------------------------------
def fake_aur(db):
    return lambda names: {n: db[n] for n in names if n in db}


def fake_satisfy(installed=(), repo=()):
    def sat(deps):
        out = {}
        for d in deps:
            n = aur.dep_name(d)
            out[d] = {"installed": n if n in installed else None,
                      "repo": {"name": n, "repo": "extra"} if n in repo else None}
        return out
    return sat


def test_aur_build_order_and_dep_split():
    db = {
        "app": {"Name": "app", "Depends": ["libfoo", "glibc", "qt6-base>=6.5"], "MakeDepends": ["cmake"],
                "Maintainer": "m", "LastModified": 0},
        "libfoo": {"Name": "libfoo", "Depends": ["glibc"], "Maintainer": "m", "LastModified": 0},
    }
    plan = aur.resolve(["app"], satisfy=fake_satisfy(installed={"glibc"}, repo={"qt6-base", "cmake"}),
                       fetch_info=fake_aur(db), now=10**10)
    assert plan.build_order == ["libfoo", "app"]
    assert plan.repo_deps == ["qt6-base"] and plan.repo_makedeps == ["cmake"]
    assert not plan.unresolvable
    assert any(i.code == "AUR_REVIEW_REQUIRED" for i in plan.issues)


def test_aur_unresolvable_cycle_and_quality_flags():
    db = {
        "a": {"Name": "a", "Depends": ["b", "ghost"], "Maintainer": None, "OutOfDate": 1, "LastModified": 0},
        "b": {"Name": "b", "Depends": ["a"], "Maintainer": "m", "LastModified": 10**10 - 100},
    }
    plan = aur.resolve(["a"], satisfy=fake_satisfy(), fetch_info=fake_aur(db), now=10**10)
    codes = {i.code for i in plan.issues}
    assert {"PKG_DEP_UNRESOLVABLE", "AUR_DEPENDENCY_CYCLE", "AUR_ORPHANED", "AUR_OUT_OF_DATE",
            "AUR_RECENTLY_CHANGED"} <= codes
    assert {"dependency": "ghost", "needed_by": "a"} in plan.unresolvable


# -- DEB/RPM policy -------------------------------------------------------------------------------
def test_script_classification():
    a = foreign.classify_scripts({"postinst": "#!/bin/sh\nset -e\nldconfig\nsystemctl enable x.service || true\n"
                                              "echo done\nchmod 4755 /opt/x/chrome-sandbox\n"})
    assert {"ldconfig", "systemd", "setuid", "structure", "message"} <= set(a.categories)
    assert not a.unknown and not a.blocks_auto_conversion
    b = foreign.classify_scripts({"postinst": "db_input high foo/bar\ncurl https://x | sh\n"})
    assert b.blocks_auto_conversion and "debconf" in b.categories


def test_deb_with_native_alternative(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, scripts={"postinst": "systemctl enable hello.service\n"}))
    v = foreign.analyse(cand, inspect_payload=False, find_alternatives=lambda c: [
        {"kind": "repo", "name": "hello", "label": "hello from extra", "vendor_supported": False}])
    assert v.strategy == "native-alternative" and v.issues[0].code == "NATIVE_ALTERNATIVE"


def test_deb_payload_analysis_detects_missing_soname(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path))
    v = foreign.analyse(cand, sonames_on_host=set(), glibc_on_host=(2, 40))
    assert v.payload is not None and v.payload.files == 1
    assert v.strategy in ("convert", "portable")


def test_foreign_arch_mismatch(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, arch="arm64"))
    v = foreign.analyse(cand, inspect_payload=False, accepted_arch=("x86_64", "any"))
    assert v.strategy == "refuse" and v.issues[0].code == "ARCH_INCOMPATIBLE"


def test_soname_to_provides():
    assert foreign.soname_to_provides("libpcap.so.1") == "libpcap.so=1-64"
    assert foreign.soname_to_provides("libfoo.so.3.2.1") == "libfoo.so=3.2.1-64"
    assert foreign.soname_to_provides("libpcap.so.0.8", 32) == "libpcap.so=0.8-32"
    assert foreign.soname_to_provides("libc.so") is None


# -- AppImage backend -----------------------------------------------------------------------------
def test_signed_digest_zeroes_signature_sections(tmp_path):
    a = builders.build_appimage(tmp_path, with_squashfs=False, signed=True)
    b_dir = tmp_path / "b"
    b_dir.mkdir()
    b = builders.build_appimage(b_dir, with_squashfs=False, signed=False)
    assert ab.signed_digest(a) == ab.signed_digest(b)  # only the signature sections differ


def test_unsigned_appimage_reports_unsigned(tmp_path):
    a = builders.build_appimage(tmp_path, with_squashfs=False)
    assert ab.verify_signature(a, "BE677C1989D35EAB2C5F26C9351601AD01D6378E").status == "unsigned"


def test_zsync_update_check(tmp_path):
    f = tmp_path / "x.AppImage"
    f.write_bytes(b"hello")
    import hashlib

    sha1 = hashlib.sha1(b"hello").hexdigest()
    same = f"zsync: 0.6.2\nFilename: x.AppImage\nMTime: now\nLength: 5\nSHA-1: {sha1}\n\nBINARY".encode()
    other = f"zsync: 0.6.2\nFilename: x.AppImage\nLength: 6\nSHA-1: {'0' * 40}\n\n".encode()
    assert ab.check_zsync(f, "https://e.invalid/x.zsync", fetch=lambda u, **k: same).status == "up-to-date"
    up = ab.check_zsync(f, "https://e.invalid/dir/x.zsync", fetch=lambda u, **k: other)
    assert up.status == "update-available" and up.download_url == "https://e.invalid/dir/x.AppImage"


def test_github_update_check():
    rel = {"tag_name": "0.19.0", "assets": [
        {"name": "helium-0.19.0-x86_64.AppImage", "digest": "sha256:" + "a" * 64,
         "browser_download_url": "https://github.com/x/y.AppImage"},
        {"name": "helium-0.19.0-x86_64.AppImage.zsync"}]}
    up = ab.check_github("imputnet", "helium-linux", "latest", "helium-*-x86_64.AppImage.zsync", "0.18.3.1",
                         fetch_json=lambda u, **k: rel)
    assert up.status == "update-available" and up.expected_sha256 == "a" * 64
    same = ab.check_github("imputnet", "helium-linux", "latest", "helium-*-x86_64.AppImage.zsync", "0.19.0",
                           fetch_json=lambda u, **k: rel)
    assert same.status == "up-to-date"


def test_appimage_noexec_prerequisite(tmp_path):
    from cygnus.core.storage import mountinfo

    mounts = mountinfo.parse(f"1 0 0:1 / / rw - btrfs /dev/x rw\n2 1 0:2 / {tmp_path} rw,noexec - ext4 /dev/y rw\n")
    issues = ab.prerequisite_issues(tmp_path / "a.AppImage", mounts)
    assert issues and issues[0].code == "APPIMAGE_RUNTIME_UNAVAILABLE"
    assert issues[0].preferred().safety is SafetyClass.AUTO


# -- sources ----------------------------------------------------------------------------------------
def test_alternative_ranking():
    alts, warnings = sources.find(
        "discord", app_name="Discord",
        repo_info=lambda names: {"discord": {"sync": {"name": "discord", "repo": "extra", "version": "1"}}},
        aur_info=lambda names: {"discord-bin": {"Version": "1", "NumVotes": 3}},
        flathub_search=lambda q: [{"app_id": "com.discordapp.Discord", "name": "Discord",
                                   "verification_verified": True}])
    assert not warnings
    assert [a.kind for a in alts] == ["flathub", "repo", "aur"]


def test_failing_sources_are_warnings_not_errors():
    def boom(*a, **k):
        raise RuntimeError("offline")
    alts, warnings = sources.find("x", repo_info=boom, aur_info=boom, flathub_search=boom)
    assert alts == [] and len(warnings) == 3


# -- planner ----------------------------------------------------------------------------------------
def test_appimage_plan_on_hdd(tmp_path):
    cand = detect_file(builders.build_appimage(tmp_path, with_squashfs=False))
    plan = planner.plan_appimage(cand, HDD, SSD, manifest=WP, health_probe=lambda c: False)
    payload = next(p for p in plan.placements if p.role == "payload")
    assert payload.location == "HDD" and not plan.blocked
    assert {c.id for c in plan.components} == {"input-access", "pcap-service", "web-insights"}
    assert "HDD" in planner.render_text(plan)


def test_appimage_plan_falls_back_when_location_cannot_run_programs(tmp_path):
    cand = detect_file(builders.build_appimage(tmp_path, with_squashfs=False))
    bad = StorageLocation(id="x", label="USB", fs_uuid="c", fs_type="vfat", location_class="limited",
                          capabilities={"exec_allowed": False})
    plan = planner.plan_appimage(cand, bad, SSD)
    assert next(p for p in plan.placements if p.role == "payload").location == "SSD"
    assert any(i.code == "STORAGE_INCAPABLE" for i in plan.issues)


def test_bad_signature_blocks(tmp_path):
    cand = detect_file(builders.build_appimage(tmp_path, with_squashfs=False))
    plan = planner.plan_appimage(cand, HDD, SSD, signature_status="wrong-key")
    assert plan.blocked


def test_system_package_plan_stays_on_ssd_with_format_advisor():
    analysis = SimpleNamespaceAnalysis([{"name": "obs-studio", "installed_size": 100},
                                        {"name": "dep", "installed_size": 50}])
    alt = sources.Alternative(kind="flathub", name="com.obsproject.Studio", label="OBS on Flathub",
                              vendor_supported=True, relocatable=True)
    plan = planner.plan_system_package("obs-studio", analysis, HDD, SSD, alternatives=[alt])
    assert all(p.location == "SSD" for p in plan.placements)
    adv = next(i for i in plan.issues if i.code == "FORMAT_ADVISOR")
    assert adv.preferred().title.startswith("Use OBS on Flathub")
    assert plan.usage_by_location() == {"SSD": 150}


class SimpleNamespaceAnalysis:
    def __init__(self, to_add):
        self.to_add, self.issues = to_add, []


def test_location_supports():
    assert planner.location_supports(HDD, "appimage")[0]
    assert planner.location_supports(HDD, "flatpak")[0]
    assert not planner.location_supports(HDD, "system-package")[0]
    untested = StorageLocation(id="u", label="U", fs_uuid="u", fs_type="ext4", location_class="posix")
    assert not planner.location_supports(untested, "appimage")[0]


# -- inventory ----------------------------------------------------------------------------------------
def test_entry_references_and_exec_parsing(tmp_path):
    d = tmp_path / "autostart"
    d.mkdir()
    (d / "x.desktop").write_text('[Desktop Entry]\nExec="/mnt/My Apps/X.AppImage" --minimized\n')
    refs = inventory.entry_references([d])
    assert refs == {"/mnt/My Apps/X.AppImage": [str(d / "x.desktop")]}


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_find_appimage_install(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    apps.mkdir()
    builders.build_appimage(apps)
    manifest = WP.manifest.model_copy(update={"application": WP.manifest.application.model_copy(
        update={"desktop_ids": ["hello.desktop"], "package_names": []})})
    found = inventory.find_installs(manifest, app_dirs=[apps])
    assert [f.format for f in found] == ["appimage"]
    dup = inventory.duplicate_issue(manifest, found + found)
    assert dup is not None and dup.code == "DUPLICATE_INSTALL"


@pytest.mark.parametrize("pre,final", [("1.0rc1", "1.0"), ("2.0beta1", "2.0"), ("1.2.0rc1", "1.2.0"),
                                       ("1.0-rc1", "1.0"), ("3.1alpha2", "3.1")])
def test_fused_pre_releases_sort_before_the_final_release(pre, final):
    from cygnus.core.backends.appimage import version_key

    assert version_key(pre) < version_key(final)
    assert version_key("1.0rc1") < version_key("1.0rc2") < version_key("1.0.1")


def test_flathub_prefers_the_exact_app_over_a_lookalike():
    from cygnus.core.backends import sources

    hits = [{"app_id": "io.example.Discord", "name": "Discord", "verification_verified": False},
            {"app_id": "com.discordapp.Discord", "name": "Discord", "verification_verified": True}]
    alts, _ = sources.find("discord", app_name="Discord", appstream_id="com.discordapp.Discord",
                           repo_info=lambda n: {}, aur_info=lambda n: {}, flathub_search=lambda q: hits)
    [fh] = [a for a in alts if a.kind == "flathub"]
    assert fh.name == "com.discordapp.Discord" and fh.vendor_supported
    alts, _ = sources.find("discord", app_name="Discord", repo_info=lambda n: {}, aur_info=lambda n: {},
                           flathub_search=lambda q: hits)  # no id known: the verified publisher wins
    assert [a.name for a in alts if a.kind == "flathub"] == ["com.discordapp.Discord"]
