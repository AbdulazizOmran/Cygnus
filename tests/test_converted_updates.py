"""A program Cygnus converted from a vendor's .deb/.rpm is updated by downloading the newer file and opening it for review."""

import gzip
import os

import pytest

from cygnus.core import paths, updates
from cygnus.core.errors import CygnusError
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.util.http import HttpError
from cygnus.gui import fixes, service

SHA = "ab" * 32


def _index(version, package="google-chrome-stable"):
    text = (f"Package: {package}\nVersion: {version}\nArchitecture: amd64\nFilename: pool/main/g/{package}/{package}_{version}_amd64.deb\n"
            f"Size: 100\nSHA256: {SHA}\n")
    return gzip.compress(text.encode())


def _install(version="155.0.8059.39-1", name="google-chrome-stable", **source):
    service.record_package_install(name, version, origin="converted", source={"file": "/x.deb", **source})
    return open_registry()


def _row(reg):
    return next(r for r in regdb.list_installations(reg) if r["format"] == "pacman")


def test_a_newer_version_in_the_vendors_list_is_an_update_with_its_address_and_checksum():
    reg = _install()
    st = updates.check(_row(reg), fetch=lambda url, **kw: _index("156.0.1-1"))
    assert (st.status, st.provider, st.available) == (updates.AVAILABLE, "vendor-feed", "156.0.1-1")
    assert st.facts["download_url"].endswith("google-chrome-stable_156.0.1-1_amd64.deb")
    assert st.facts["expected_sha256"] == SHA and st.facts["kind"] == "converted"


def test_the_same_or_an_older_version_is_up_to_date():
    reg = _install("155.0.8059.39-1")
    assert updates.check(_row(reg), fetch=lambda u, **kw: _index("155.0.8059.39-1")).status == updates.UP_TO_DATE
    assert updates.check(_row(reg), fetch=lambda u, **kw: _index("154.0.1-1")).status == updates.UP_TO_DATE


def test_a_program_the_vendor_list_does_not_cover_is_manual_and_says_how_to_update():
    reg = _install(name="some-tool")
    st = updates.check(_row(reg), fetch=lambda u, **kw: pytest.fail("nothing to fetch for an unknown program"))
    assert st.status == updates.MANUAL and "open it with Cygnus" in st.detail


def test_a_failed_check_is_unknown_never_up_to_date():
    reg = _install()

    def offline(url, **kw):
        raise HttpError("network error for https://dl.google.com: no route")

    st = updates.check(_row(reg), fetch=offline)
    assert st.status == updates.UNKNOWN and "no route" in st.detail
    assert updates.check(_row(reg), fetch=lambda u, **kw: b"junk").status == updates.UNKNOWN


def test_an_rpm_made_program_is_offered_the_vendors_rpm_and_the_name_the_vendor_uses_is_remembered():
    reg = _install(vendor_package="google-chrome-stable", vendor_format="rpm")
    st = updates.check(_row(reg), fetch=lambda u, **kw: _index("156.0.1-1"))
    assert st.status == updates.AVAILABLE and st.facts["download_url"].endswith("_current_x86_64.rpm")
    assert st.facts["expected_sha256"] is None and st.facts["format"] == "rpm"


def test_converted_programs_are_in_the_overview_and_the_stored_result_survives(monkeypatch):
    reg = _install()
    [st] = updates.check_all(reg, fetch=lambda u, **kw: _index("156.0.1-1"))
    assert st.status == updates.AVAILABLE
    [known] = [u for u in updates.last_known(reg) if u.format == "pacman"]
    assert known.status == updates.AVAILABLE and known.facts["kind"] == "converted" and known.checked_at
    # an ordinary pacman package (not converted) stays "updated with your system"
    service.record_package_install("hello", "1.0", origin="local", source={"file": "/h.pkg.tar.zst"})
    kinds = {u.name: u.status for u in updates.last_known(open_registry())}
    assert kinds["hello"] == updates.SYSTEM


def test_fetching_the_update_downloads_the_checked_file_and_keeps_only_that_one(tmp_path, monkeypatch):
    reg = _install()
    folder = paths.cache_dir() / "downloads" / "updates"
    folder.mkdir(parents=True)
    (folder / "old-update.deb").write_bytes(b"old")
    (folder / "dl-previous").mkdir()
    (folder / "dl-previous" / "big.deb").write_bytes(b"old")
    calls = []

    def download(url, dest, *, expected_sha256=None, progress=None):
        calls.append((url, expected_sha256))
        target = dest / "google-chrome-stable_156.0.1-1_amd64.deb"
        target.write_bytes(b"new")
        return target

    monkeypatch.setattr(updates, "_check_converted", lambda row, **kw: updates._status(
        row, updates.AVAILABLE, "x", provider="vendor-feed", available="156.0.1-1",
        facts={"download_url": "https://dl.google.com/x/google-chrome-stable_156.0.1-1_amd64.deb", "expected_sha256": SHA,
               "kind": "converted"}))
    result = service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=download)
    assert result["ok"] and result["path"].endswith("_156.0.1-1_amd64.deb") and result["checked"] and result["version"] == "156.0.1-1"
    assert calls == [("https://dl.google.com/x/google-chrome-stable_156.0.1-1_amd64.deb", SHA)]
    [mine] = list(folder.iterdir())  # the old download is gone; this one has a folder of its own
    assert mine.name.startswith("dl-") and [p.name for p in mine.iterdir()] == ["google-chrome-stable_156.0.1-1_amd64.deb"]
    assert result["file_url"] == "file://" + result["path"]


def test_there_is_nothing_to_fetch_when_up_to_date_and_never_for_a_program_that_is_not_converted(monkeypatch):
    reg = _install()
    monkeypatch.setattr(updates, "_check_converted", lambda row, **kw: updates._status(row, updates.UP_TO_DATE, "same",
                                                                                      provider="vendor-feed"))
    result = service.fetch_converted_update(_row(reg)["id"], lambda line: None,
                                            download=lambda *a, **k: pytest.fail("nothing to download"))
    assert result["ok"] is False and "No update to download" in result["error"]
    service.record_package_install("hello", "1.0", origin="local", source={"file": "/h.pkg.tar.zst"})
    other = next(r for r in regdb.list_installations(open_registry()) if r["name"] == "hello")
    with pytest.raises(CygnusError, match="not a program Cygnus converted"):
        service.fetch_converted_update(other["id"], lambda line: None)


def test_only_a_plausible_vendor_name_and_format_are_remembered_with_a_conversion():
    done = {}
    original = service.record_package_install
    try:
        service.record_package_install = lambda name, version, **kw: done.update(kw["source"])
        base = {"kind": "converted", "name": "x", "version": "1", "source": "/x.deb"}
        fixes._recording({**base, "vendor_package": "google-chrome-stable", "vendor_format": "deb"})()
        assert done == {"file": "/x.deb", "vendor_package": "google-chrome-stable", "vendor_format": "deb"}
        for bad in ({"vendor_package": "../x", "vendor_format": "deb"}, {"vendor_package": "ok", "vendor_format": "exe"},
                    {"vendor_package": 5, "vendor_format": "deb"}, {}):
            done.clear()
            fixes._recording({**base, **bad})()
            assert done == {"file": "/x.deb"}, bad
    finally:
        service.record_package_install = original


def _available(monkeypatch):
    monkeypatch.setattr(updates, "_check_converted", lambda row, **kw: updates._status(
        row, updates.AVAILABLE, "x", provider="vendor-feed", available="156.0.1-1",
        facts={"download_url": "https://dl.google.com/x/a_156.0.1-1_amd64.deb", "expected_sha256": SHA, "kind": "converted"}))


def _fake_download(url, dest, *, expected_sha256=None, progress=None):
    target = dest / "a.deb"
    target.write_bytes(b"new")
    return target


def test_a_download_folder_that_is_a_link_is_refused_and_nothing_behind_it_is_touched(tmp_path, monkeypatch):
    reg = _install()
    _available(monkeypatch)
    precious = tmp_path / "Documents"
    precious.mkdir()
    (precious / "thesis.txt").write_text("keep me")
    (precious / "folder").mkdir()
    (precious / "folder" / "inner.txt").write_text("and me")
    root = paths.cache_dir() / "downloads"
    root.mkdir(parents=True)
    (root / "updates").symlink_to(precious)
    with pytest.raises(CygnusError, match="not a plain folder"):
        service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=_fake_download)
    assert (precious / "thesis.txt").read_text() == "keep me" and (precious / "folder" / "inner.txt").exists()
    (root / "updates").unlink()
    (root.parent / "downloads-real").mkdir()
    root.rmdir()
    root.symlink_to(root.parent / "downloads-real")  # the parent being a link is refused too
    with pytest.raises(CygnusError, match="not a plain folder"):
        service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=_fake_download)


def test_a_stray_link_inside_the_download_folder_is_removed_itself_not_what_it_points_at(tmp_path, monkeypatch):
    reg = _install()
    _available(monkeypatch)
    target = tmp_path / "keep"
    target.mkdir()
    (target / "file").write_text("keep me")
    folder = paths.cache_dir() / "downloads" / "updates"
    folder.mkdir(parents=True)
    (folder / "dl-link").symlink_to(target)
    service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=_fake_download)
    assert (target / "file").read_text() == "keep me" and not (folder / "dl-link").exists()


def test_two_downloads_at_once_do_not_spoil_each_other(monkeypatch):
    reg = _install()
    _available(monkeypatch)
    answers = []

    def slow(url, dest, **kw):
        answers.append(service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=_fake_download))  # a second one starts
        return _fake_download(url, dest)

    first = service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=slow)
    assert first["ok"] and answers[0]["ok"] is False and "Another update is still being downloaded" in answers[0]["error"]
    assert service.fetch_converted_update(_row(reg)["id"], lambda line: None, download=_fake_download)["ok"]  # the lock was let go


def test_the_download_address_is_percent_encoded_so_odd_folder_names_survive():
    from pathlib import Path

    assert Path("/home/u/.cache/x#1/y z.deb").as_uri() == "file:///home/u/.cache/x%231/y%20z.deb"


def test_an_edge_program_converted_from_an_rpm_is_manual_not_unknown():
    reg = _install(name="microsoft-edge-stable", vendor_package="microsoft-edge-stable", vendor_format="rpm")
    st = updates.check(_row(reg), fetch=lambda u, **kw: pytest.fail("no address to look for: nothing to fetch"))
    assert st.status == updates.MANUAL and "open it with Cygnus" in st.detail


def test_a_broken_entry_in_the_list_is_unknown_with_the_right_provider_and_does_not_stop_the_whole_check():
    reg = _install()
    bad = gzip.compress((f"Package: google-chrome-stable\nVersion: 156.0.1-1\nArchitecture: amd64\nSize: \u00b2\n"
                         f"Filename: pool/x_amd64.deb\nSHA256: {SHA}\n").encode())
    [st] = updates.check_all(reg, fetch=lambda u, **kw: bad)
    assert st.status == updates.UNKNOWN and st.provider == "vendor-feed"
