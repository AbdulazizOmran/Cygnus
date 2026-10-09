"""Native alternatives for foreign packages: already installed, or published by the vendor for Arch."""

import builders
from cygnus.core.backends import foreign, sources
from cygnus.core.detect import detect_file
from cygnus.core.manifest import catalog


def _no_network(names):
    return {}


def test_vendor_arch_package_from_a_curated_manifest_component():
    alts = sources.from_manifest_components("whatpulse-pcap-service", catalog.bundled_manifests())
    [a] = alts
    assert a.kind == "manifest-component" and a.vendor_supported
    assert a.facts["url"].endswith("whatpulse-pcap-service-1.5.1-1-x86_64.pkg.tar.zst")
    assert a.facts["sha256"] == "54fe5e87ac6a09d6c6cc7bbecf64514ed3b916ff31a2577521315e8b4a42160c"
    assert sources.from_manifest_components("unrelated", catalog.bundled_manifests()) == []


def test_installed_package_ranks_first():
    info = {"hello": {"local": {"name": "hello", "version": "1.0-1"},
                      "sync": {"name": "hello", "repo": "extra", "version": "1.1-1"}}}
    alts, _ = sources.find("hello", repo_info=lambda names: info, aur_info=_no_network,
                           flathub_search=lambda q: [], catalog=catalog.bundled_manifests())
    assert [a.kind for a in alts] == ["installed", "repo"]


def test_foreign_verdict_says_not_needed_when_already_installed(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path))
    alts = [{"kind": "installed", "name": "hello", "version": "1.0-1", "label": "hello 1.0-1, already installed",
             "vendor_supported": False}]
    verdict = foreign.analyse(cand, find_alternatives=lambda c: alts, inspect_payload=False)
    assert verdict.strategy == "native-alternative" and verdict.summary.startswith("Not needed")
    assert verdict.issues[0].code == "ALREADY_INSTALLED_NATIVE"
    assert verdict.issues[0].title == "Already installed as an Arch package: hello 1.0-1"


# -- pinned signatures on adopt ----------------------------------------------------------------------------
import pytest  # noqa: E402

from cygnus.core.backends import appimage as ab  # noqa: E402
from cygnus.core.errors import CygnusError  # noqa: E402
from cygnus.core.ops import appimage_ops  # noqa: E402


def test_adopting_requires_the_pinned_vendor_signature(tmp_path, monkeypatch):
    helium = catalog.bundled_manifests()["net.imput.helium"]
    calls = []

    def verify(path, fpr):
        calls.append(fpr)
        return ab.SignatureResult(status="wrong-key", detail="signed by someone else")

    monkeypatch.setattr(ab, "verify_signature", verify)
    with pytest.raises(CygnusError, match="not signed by .* key \\(wrong-key\\)"):
        appimage_ops.require_pinned_signature(tmp_path / "helium.AppImage", helium)
    assert calls == ["BE677C1989D35EAB2C5F26C9351601AD01D6378E"]
    monkeypatch.setattr(ab, "verify_signature", lambda p, f: ab.SignatureResult(status="verified", detail=""))
    assert appimage_ops.require_pinned_signature(tmp_path / "helium.AppImage", helium) == "verified"
    assert appimage_ops.require_pinned_signature(tmp_path / "x", None) is None  # nothing pinned: nothing to check
