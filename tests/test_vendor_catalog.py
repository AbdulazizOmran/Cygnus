"""The hand-written descriptions of the vendors' .deb/.rpm programs (Discover): every built-in source has one, the Flathub ids that
let Discover show a vendor's package and the Flatpak as one program are only the verified ones, and nothing can be merged by accident."""

import re

import pytest

from cygnus.core import preferences, vendor_catalog
from cygnus.core.backends import vendor_feeds

FLATHUB_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*){2,}\Z")
VERIFIED = {  # checked by hand against the real Flathub data (2026-10-11); a change here needs a new check, not a guess
    "google-chrome-stable": "com.google.Chrome", "google-chrome-unstable": "com.google.ChromeDev", "microsoft-edge-stable": "com.microsoft.Edge",
    "vivaldi-stable": "com.vivaldi.Vivaldi", "brave-browser": "com.brave.Browser", "opera-stable": "com.opera.Opera",
    "signal-desktop": "org.signal.Signal", "1password": "com.onepassword.OnePassword", "slack-desktop": "com.slack.Slack",
    "code": "com.visualstudio.code", "google-earth-pro-stable": "com.google.EarthPro"}


def test_every_built_in_source_has_a_description_and_nothing_else_has():
    assert set(vendor_catalog._KNOWN) == set(vendor_feeds.FEEDS)
    assert [v["package"] for v in vendor_catalog.vendors()] == list(vendor_feeds.FEEDS)


def test_the_flathub_ids_are_exactly_the_verified_ones():
    ids = {v["package"]: v["flathub_id"] for v in vendor_catalog.vendors() if v["flathub_id"]}
    assert ids == VERIFIED
    assert all(FLATHUB_ID.match(i) for i in ids.values())
    assert len(set(ids.values())) == len(ids)  # two packages must never claim one Flathub program


def test_the_beta_channel_is_never_merged_with_the_developer_channel():
    by = {v["package"]: v for v in vendor_catalog.vendors()}
    assert by["google-chrome-beta"]["flathub_id"] == ""  # ChromeDev is "unstable", not beta
    assert by["google-chrome-unstable"]["flathub_id"] == "com.google.ChromeDev"
    assert by["teamviewer"]["flathub_id"] == "" and by["code-insiders"]["flathub_id"] == ""  # not on Flathub


def test_formats_follow_what_the_vendor_publishes():
    for v in vendor_catalog.vendors():
        feed = vendor_feeds.FEEDS[v["package"]]
        assert v["formats"] == ["deb"] + (["rpm"] if feed.rpm else [])
    assert next(v for v in vendor_catalog.vendors() if v["package"] == "microsoft-edge-stable")["formats"] == ["deb"]
    assert next(v for v in vendor_catalog.vendors() if v["package"] == "google-chrome-stable")["formats"] == ["deb", "rpm"]


def test_each_entry_is_complete_plain_and_safe_to_show():
    names = set()
    for v in vendor_catalog.vendors():
        assert v["name"] and v["summary"] and v["categories"] and v["icon_name"]
        assert v["name"] not in names
        names.add(v["name"])
        assert v["url"].startswith("https://") and "?" not in v["url"] and " " not in v["url"]
        assert all(c.isprintable() for c in v["name"] + v["summary"] + v["developer"])
        assert all(re.fullmatch(r"[A-Za-z0-9]+", c) for c in v["categories"])
        assert len(v["summary"]) <= 200


def test_stable_vs_code_is_a_built_in_source():
    feed = vendor_feeds.FEEDS["code"]
    assert feed.index == "https://packages.microsoft.com/repos/code/dists/stable/main/binary-amd64/Packages.gz"
    assert feed.base == "https://packages.microsoft.com/repos/code/" and feed.rpm is None


def test_a_source_you_added_is_listed_plainly_after_the_built_in_ones():
    preferences.save({"vendor_feeds": {"zeta-app": {"index": "https://example.com/Packages.gz", "base": "https://example.com/"},
                                       "alpha-app": {"index": "https://example.com/a/Packages.gz", "base": "https://example.com/a/",
                                                     "rpm": "https://example.com/a/latest.rpm"}}})
    listed = vendor_catalog.vendors()
    assert [v["package"] for v in listed][-2:] == ["alpha-app", "zeta-app"]
    mine = listed[-2]
    assert mine["summary"] == vendor_catalog.USER_SOURCE_TEXT and mine["formats"] == ["deb", "rpm"] and mine["flathub_id"] == "" and mine["url"] == ""
    assert listed[-1]["formats"] == ["deb"]


def test_a_source_of_your_own_for_a_built_in_program_does_not_change_what_the_program_is():
    preferences.save({"vendor_feeds": {"brave-browser": {"index": "https://example.com/Packages.gz", "base": "https://example.com/"}}})
    brave = next(v for v in vendor_catalog.vendors() if v["package"] == "brave-browser")
    assert brave["name"] == "Brave" and brave["flathub_id"] == "com.brave.Browser"
    assert [v["package"] for v in vendor_catalog.vendors()].count("brave-browser") == 1


def test_a_malformed_source_in_the_preferences_is_ignored():
    preferences.save({"vendor_feeds": {"bad name": {"index": "https://example.com/Packages.gz", "base": "https://example.com/"},
                                       "../evil": {"index": "https://example.com/Packages.gz", "base": "https://example.com/"},
                                       "plain-http": {"index": "http://example.com/Packages.gz", "base": "http://example.com/"}}})
    assert [v["package"] for v in vendor_catalog.vendors()] == list(vendor_feeds.FEEDS)


def test_the_documented_number_of_vendors_matches():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    count = str(len(vendor_feeds.FEEDS))
    assert f"for {count} known vendors" in (root / "README.md").read_text()
    assert f"updates for {count} known vendors" in (root / "CHANGELOG.md").read_text()
