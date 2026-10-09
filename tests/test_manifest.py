import copy
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cygnus.core.manifest import catalog, signing
from cygnus.core.manifest.schema import Manifest, json_schema

WP = json.loads((catalog.resources.files("cygnus.data") / "manifests/org.whatpulse.WhatPulse.json").read_text())


def mutated(fn):
    d = copy.deepcopy(WP)
    fn(d)
    return d


def test_bundled_manifests_are_valid_and_curated():
    cat = catalog.bundled_manifests()
    assert {"org.whatpulse.WhatPulse", "net.imput.helium"} <= set(cat)
    assert all(m.trust_level == "curated" and m.can_drive_actions for m in cat.values())


@pytest.mark.parametrize("label,fn", [
    ("shell action", lambda d: d["components"][0].update(action={"kind": "shell.run", "cmd": "id"})),
    ("unknown field", lambda d: d["components"][0].update(command="rm -rf /")),
    ("plain http", lambda d: d["sources"][0].update(url="http://example.com/x")),
    ("unknown probe", lambda d: d["components"][0]["verify"].append({"probe": "exec", "cmd": "id"})),
    ("probe reads home", lambda d: d["components"][0]["verify"].append({"probe": "readable", "path": "/home/u/.ssh/id"})),
    ("dangling feature ref", lambda d: d["features"][1]["requires"].append("nope")),
    ("user data outside home", lambda d: d["user_data"]["paths"].append("/etc")),
    ("user data traversal", lambda d: d["user_data"]["paths"].append("~/../../etc")),
    ("user data wildcard", lambda d: d["user_data"]["paths"].append("~/.config/*")),
    ("pgp without fingerprint", lambda d: d["sources"][0].update(verification={"type": "openpgp-detached"})),
    ("detached pgp without signature url", lambda d: d["sources"][0].update(
        verification={"type": "openpgp-detached", "openpgp_fingerprint": "A" * 40})),
    ("pkgbuild pgp on an appimage source", lambda d: d["sources"][0].update(
        verification={"type": "pkgbuild-openpgp", "openpgp_fingerprint": "A" * 40})),
    ("pkgbuild pgp without fingerprint", lambda d: d["sources"][0].update(verification={"type": "pkgbuild-openpgp"})),
    ("two core features", lambda d: d["features"][1].update(core=True)),
    ("bad unit name", lambda d: d["components"][1]["post"][0].update(unit="foo; rm -rf /")),
    ("bad group", lambda d: d["components"][0]["action"].update(group="wheel;x")),
    ("bad sha", lambda d: d["components"][1]["platform_actions"]["arch"].update(sha256="abc")),
    ("bad regex", lambda d: d["components"][1]["verify"][2].update(regex="(")),
    ("browser ext without browsers", lambda d: d["components"][2].update(browsers={})),
    ("conflict unknown source", lambda d: d["conflicts"][0].update(between=["appimage", "nope"])),
])
def test_hostile_or_broken_manifests_are_rejected(label, fn):
    with pytest.raises(ValidationError):
        Manifest.model_validate(mutated(fn))


def test_json_schema_export():
    schema = json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    kinds = json.dumps(schema)
    assert "pacman.install_local" in kinds and "shell" not in kinds


def _override(*perms):
    from cygnus.core.manifest.schema import FlatpakOverride
    return FlatpakOverride.model_validate({"kind": "flatpak.override", "app": "org.example.App",
                                           "permissions": list(perms)})


@pytest.mark.parametrize("perm", ["--share=network", "--filesystem=xdg-documents/My Stuff:ro",
                                  "--filesystem=xdg-download", "--env=FOO=bar"])
def test_narrow_overrides_still_accepted(perm):
    assert _override(perm).permissions == [perm]


@pytest.mark.parametrize("perm", ["--share=network\n", "--env=FOO=bar\n", "--filesystem=xdg-documents\n",
                                  "--filesystem=xdg-documents/..", "--filesystem=xdg-documents/..:rw",
                                  "--filesystem=xdg-download/.", "--filesystem=xdg-music/../x"])
def test_override_rejects_trailing_newline_and_dot_segments(perm):
    with pytest.raises(ValidationError):
        _override(perm)


def test_newline_cannot_hide_in_strings_that_reach_the_helper():
    # Field(pattern=...) is evaluated by pydantic's own engine; a trailing newline must not slip past it.
    from cygnus.core.manifest.schema import AurBuild, GroupAddUser, SystemdEnableNow
    for cls, field, ok in [(GroupAddUser, "group", "input"), (SystemdEnableNow, "unit", "foo.service"),
                           (AurBuild, "pkgbase", "pfetch")]:
        kind = {GroupAddUser: "group.add_user", SystemdEnableNow: "systemd.enable_now", AurBuild: "aur.build"}[cls]
        cls.model_validate({"kind": kind, field: ok})
        with pytest.raises(ValidationError):
            cls.model_validate({"kind": kind, field: ok + "\n"})


# -- signatures -----------------------------------------------------------------------------------
@pytest.fixture
def keys():
    return signing.generate_keypair()


def test_sign_verify_roundtrip(keys):
    priv, pub = keys
    sig = signing.sign(b"hello", priv, pub, "trusted: yes")
    v = signing.verify(b"hello", sig, signing.parse_public_key(pub.serialize()))
    assert v.trusted_comment == "trusted: yes" and v.key_id_hex == pub.key_id_hex


@pytest.mark.parametrize("tamper", [
    lambda s: s.replace("trusted: yes", "trusted: no"),  # trusted comment is covered by the global signature
    lambda s: s.splitlines()[0] + "\n" + s.splitlines()[1][:-4] + "AAA=\n" + "\n".join(s.splitlines()[2:]),
    lambda s: "garbage",
])
def test_tampered_signatures_fail(keys, tamper):
    priv, pub = keys
    sig = signing.sign(b"hello", priv, pub, "trusted: yes")
    with pytest.raises(signing.SignatureError):
        signing.verify(b"hello", tamper(sig), pub)


def test_wrong_message_and_wrong_key(keys):
    priv, pub = keys
    _, other = signing.generate_keypair()
    sig = signing.sign(b"hello", priv, pub, "c")
    with pytest.raises(signing.SignatureError):
        signing.verify(b"hellO", sig, pub)
    with pytest.raises(signing.SignatureError):
        signing.verify(b"hello", sig, other)


def test_legacy_minisign_vector():
    """Interoperability: a legacy ('Ed', non-prehashed) signature made with the reference algorithm."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64

    priv = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    from cryptography.hazmat.primitives import serialization

    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = signing.PublicKey(key_id=b"\x01" * 8, raw=raw)
    sig = priv.sign(b"msg")
    trusted = "timestamp:0"
    text = ("untrusted comment: x\n" + base64.b64encode(b"Ed" + key.key_id + sig).decode() + "\n"
            f"trusted comment: {trusted}\n" + base64.b64encode(priv.sign(sig + trusted.encode())).decode() + "\n")
    assert signing.verify(b"msg", text, key).trusted_comment == trusted


# -- catalog trust decisions ----------------------------------------------------------------------
RAW = json.dumps(WP).encode()


def test_vendor_signed_manifest(keys):
    priv, pub = keys
    ts = catalog.TrustStore()
    ts.add(pub, "whatpulse.org", "vendor-signed")
    lm = catalog.load(RAW, origin="network", signature=signing.sign(RAW, priv, pub, "c"), trust=ts)
    assert lm.trust_level == "vendor-signed" and lm.signer == "whatpulse.org" and lm.can_drive_actions


def test_key_for_other_domain_is_not_trusted(keys):
    priv, pub = keys
    ts = catalog.TrustStore()
    ts.add(pub, "evil.example", "vendor-signed")
    lm = catalog.load(RAW, origin="network", signature=signing.sign(RAW, priv, pub, "c"), trust=ts)
    assert lm.trust_level == "unverified" and not lm.can_drive_actions


def test_unsigned_network_manifest_is_unverified():
    lm = catalog.load(RAW, origin="network")
    assert lm.trust_level == "unverified" and "not signed" in lm.warnings[0]


def test_tampered_manifest_raises(keys):
    priv, pub = keys
    ts = catalog.TrustStore()
    ts.add(pub, "whatpulse.org", "vendor-signed")
    with pytest.raises(signing.SignatureError):
        catalog.load(RAW.replace(b"input", b"wheel"), origin="network",
                     signature=signing.sign(RAW, priv, pub, "c"), trust=ts)


def test_rollback_refused_and_expiry_downgrades():
    with pytest.raises(catalog.ManifestError, match="rollback"):
        catalog.load(RAW, origin="bundled", last_serial=WP["serial"] + 1)
    lm = catalog.load(RAW, origin="bundled", now=datetime(2030, 1, 1, tzinfo=UTC))
    assert lm.trust_level == "unverified" and any("expired" in w for w in lm.warnings)


def test_oversized_manifest_rejected():
    with pytest.raises(catalog.ManifestError):
        catalog.load(b" " * (catalog.MAX_MANIFEST_BYTES + 1), origin="network")


def test_find_for_identities():
    cat = catalog.bundled_manifests()
    assert catalog.find_for({"flatpak_id": "org.whatpulse.WhatPulse"}, cat).manifest.application.name == "WhatPulse"
    assert catalog.find_for({"desktop_id": "helium.desktop"}, cat).manifest.application.name == "Helium"
    assert catalog.find_for({"name": "unrelated"}, cat) is None


def test_published_schema_and_spec_example_match_the_implementation():
    import re
    from pathlib import Path

    from cygnus.core.manifest import catalog

    docs = Path(__file__).resolve().parent.parent / "docs"
    assert (docs / "cam-1.schema.json").read_text().strip() == catalog.dumps_schema().strip(), \
        "regenerate with: python3 -m cygnus manifest schema > docs/cam-1.schema.json"
    example = re.search(r"```json\n(.*?)```", (docs / "manifest-spec.md").read_text(), re.S).group(1)
    assert catalog.load(example.encode(), origin="import").manifest.application.id == "org.example.Recorder"


# -- load_file reads defensively ------------------------------------------------------------------
def _bundled_text():
    return (catalog.resources.files("cygnus.data") / "manifests/org.whatpulse.WhatPulse.json").read_text()


def test_load_file_reads_a_manifest_and_its_signature(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(_bundled_text())
    lm = catalog.load_file(f)
    assert lm.trust_level == "unverified" and "not signed" in " ".join(lm.warnings)
    (tmp_path / "m.json.minisig").write_text("untrusted comment: x\nnot-base64\n")
    assert "signature present" in " ".join(catalog.load_file(f).warnings)


def test_load_file_refuses_oversized_special_and_linked_files(tmp_path):
    import os
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (catalog.MAX_MANIFEST_BYTES + 1))
    with pytest.raises(catalog.ManifestError, match="too large"):
        catalog.load_file(big)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(catalog.ManifestError, match="not a regular file"):   # must not block either
        catalog.load_file(fifo)
    real = tmp_path / "real.json"
    real.write_text(_bundled_text())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(catalog.ManifestError, match="cannot read"):
        catalog.load_file(link)
    with pytest.raises(catalog.ManifestError, match="does not exist"):
        catalog.load_file(tmp_path / "missing.json")


def test_load_file_signature_problems_are_cygnus_errors(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(_bundled_text())
    sig = tmp_path / "m.json.minisig"
    sig.write_bytes(b"\xff\xfe\x00 not utf-8")
    with pytest.raises(signing.SignatureError, match="not text"):
        catalog.load_file(f)
    sig.write_bytes(b"x" * (catalog.MAX_SIGNATURE_BYTES + 1))
    with pytest.raises(signing.SignatureError, match="too large"):
        catalog.load_file(f)


# -- the trust store pins a key on first use ------------------------------------------------------
def test_trust_store_refuses_to_reassign_a_key_id(keys):
    _, pub = keys
    ts = catalog.TrustStore()
    ts.add(pub, "whatpulse.org", "vendor-signed")
    ts.add(pub, "whatpulse.org", "vendor-signed")          # the same entry again is harmless
    with pytest.raises(catalog.ManifestError, match="already trusted for whatpulse.org"):
        ts.add(pub, "evil.example", "vendor-signed")
    with pytest.raises(catalog.ManifestError):
        ts.add(pub, "whatpulse.org", "curated")             # nor may a vendor key be promoted
    assert ts.keys[pub.key_id_hex].owner == "whatpulse.org"


def test_trust_store_refuses_a_different_key_with_the_same_id(keys):
    _, pub = keys
    other = signing.PublicKey(key_id=pub.key_id, raw=bytes(32))
    ts = catalog.TrustStore()
    ts.add(pub, "whatpulse.org", "vendor-signed")
    with pytest.raises(catalog.ManifestError):
        ts.add(other, "whatpulse.org", "vendor-signed")


def test_spec_stars_match_the_privileged_action_set():
    import re
    from pathlib import Path

    from cygnus.core.manifest.schema import PRIVILEGED_ACTIONS

    spec = (Path(__file__).resolve().parent.parent / "docs" / "manifest-spec.md").read_text()
    starred = set(re.findall(r"^\| `([a-z_.]+)` ✱", spec, re.M))
    assert starred == set(PRIVILEGED_ACTIONS)
    assert not any(k.startswith("flatpak.") for k in PRIVILEGED_ACTIONS)   # Flatpak runs as the user


def test_a_systemd_or_group_fix_needs_root_but_a_flatpak_one_does_not():
    from cygnus.core import planner
    from cygnus.core.recovery.model import Privilege

    cat = catalog.bundled_manifests()["org.whatpulse.WhatPulse"]
    comps = planner._components_from_manifest(cat, "appimage", None)
    assert any(c.privilege is Privilege.ROOT for c in comps)
    assert planner.PRIVILEGED_ACTIONS.isdisjoint({"flatpak.install", "flatpak.install_bundle", "flatpak.override"})


# -- review round 4: what a vendor-signed manifest may claim ---------------------------------------------
def test_a_vendor_signed_manifest_cannot_claim_an_application_through_a_hint(keys):
    """Package names and desktop-file names are not bound to the vendor's domain, so they match only for
    manifests that ship with Cygnus."""
    raw = json.loads(_bundled_text())
    raw["application"]["desktop_ids"] = ["firefox.desktop"]
    raw["application"]["package_names"] = ["firefox"]
    data = json.dumps(raw).encode()
    priv, pub = keys
    ts = catalog.TrustStore()
    ts.add(pub, raw["application"]["vendor"]["domain"], "vendor-signed")
    vendor = catalog.load(data, origin="network", signature=signing.sign(data, priv, pub, "c"), trust=ts)
    assert vendor.trust_level == "vendor-signed"
    assert catalog.find_for({"desktop_id": "firefox.desktop", "package": "firefox"}, {"v": vendor}) is None
    assert catalog.find_for({"flatpak_id": "org.whatpulse.WhatPulse"}, {"v": vendor}) is vendor  # its own ids still do
    bundled = catalog.bundled_manifests()
    assert catalog.find_for({"desktop_id": "helium.desktop"}, bundled).manifest.application.id == "net.imput.helium"


@pytest.mark.parametrize("field, value", [("appstream_ids", "org.whatpulse.Ａpp"), ("flatpak_ids", "org.what pulse.App"),
                                         ("flatpak_ids", "x" * 300 + ".y"), ("appstream_ids", "../etc/passwd")])
def test_the_identifier_lists_are_reverse_dns_names(field, value):
    d = copy.deepcopy(WP)
    d["application"][field] = [value]
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


@pytest.mark.parametrize("perm", ["--talk-name=org.freedesktop.login1", "--talk-name=org.kde.kwalletd6",
                                  "--talk-name=org.kde.KWallet", "--talk-name=org.freedesktop.impl.portal.desktop.kde",
                                  "--talk-name=org.gnome.keyring", "--talk-name=org.freedesktop.PolicyKit1.Foo",
                                  "--env=GIO_MODULE_DIR=/x", "--env=LD_AUDIT=/x.so", "--env=GTK_PATH=/x",
                                  "--env=QT_PLUGIN_PATH=/x", "--env=NODE_OPTIONS=--require", "--env=XDG_DATA_DIRS=/x",
                                  "--env=PATH=/x", "--env=LD_PRELOAD=/x.so", "--env=GIO_EXTRA_MODULES=/x"])
def test_overrides_cannot_reach_secrets_logind_portals_or_change_what_code_is_loaded(perm):
    with pytest.raises(ValidationError):
        _override(perm)


@pytest.mark.parametrize("perm", ["--env=FOO=bar", "--env=MOZ_ENABLE_WAYLAND=1", "--talk-name=org.freedesktop.Notifications",
                                  "--talk-name=org.kde.StatusNotifierWatcher"])
def test_harmless_overrides_are_still_accepted(perm):
    assert _override(perm).permissions == [perm]


@pytest.mark.parametrize("label,fn", [
    ("name too long", lambda d: d["application"].update(name="x" * 500)),
    ("note too long", lambda d: d["components"][0].update(notes="y" * 5000)),
    ("too many known issues", lambda d: d["sources"][0].update(known_issues=["i"] * 100)),
    ("domain with an empty label", lambda d: d["application"]["vendor"].update(domain="a..b.com")),
    ("domain starting with a dash", lambda d: d["application"]["vendor"].update(domain="-a.example.com")),
    ("package name starting with a dash", lambda d: d["components"][0]["action"].update(
        kind="pacman.install_repo", names=["--noconfirm"]) if d["components"][0].get("action") else
        d["components"][0].update(action={"kind": "pacman.install_repo", "names": ["--noconfirm"]})),
    ("aur pkgbase starting with a dash", lambda d: d["components"][0].update(
        action={"kind": "aur.build", "pkgbase": "-evil"})),
])
def test_manifest_text_is_bounded_and_names_cannot_look_like_options(label, fn):
    with pytest.raises(ValidationError):
        Manifest.model_validate(mutated(fn))
