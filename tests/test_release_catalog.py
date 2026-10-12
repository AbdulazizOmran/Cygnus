"""The hand-written descriptions of the programs whose makers publish downloads on GitHub (Discover): every project has one, the Flathub ids that let
Discover show a release download and the Flatpak as one program are only the verified ones, and nothing is listed twice."""

import re

from cygnus.core import release_catalog, vendor_catalog
from cygnus.core.backends import release_feeds, vendor_feeds

FLATHUB_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*){2,}\Z")
VERIFIED = {  # checked against the real Flathub data on 2026-10-12 (all 21 are keys of entries in it); a change here needs a new check, not a guess
    "zen-browser": "app.zen_browser.zen", "obsidian": "md.obsidian.Obsidian", "heroic": "com.heroicgameslauncher.hgl",
    "localsend": "org.localsend.localsend_app", "rustdesk": "com.rustdesk.RustDesk", "zettlr": "com.zettlr.Zettlr", "logseq": "com.logseq.Logseq",
    "joplin": "net.cozic.joplin_desktop", "ferdium": "org.ferdium.Ferdium", "mailspring": "com.getmailspring.Mailspring",
    "beekeeper-studio": "io.beekeeperstudio.Studio", "dbeaver": "io.dbeaver.DBeaverCommunity", "anytype": "io.anytype.anytype",
    "cryptomator": "org.cryptomator.Cryptomator", "cherry-studio": "com.cherry_ai.CherryStudio", "appflowy": "io.appflowy.AppFlowy",
    "cemu": "info.cemu.Cemu", "losslesscut": "no.mifi.losslesscut", "vesktop": "dev.vencord.Vesktop", "ultimaker-cura": "com.ultimaker.cura",
    "freecad": "org.freecad.FreeCAD"}
NOT_ON_FLATHUB = {"helium", "balena-etcher", "tabby"}


def test_every_project_has_a_description_and_nothing_else_has():
    assert set(release_catalog._KNOWN) == set(release_feeds.PROJECTS)
    assert [r["key"] for r in release_catalog.releases()] == list(release_feeds.PROJECTS)


def test_the_flathub_ids_are_exactly_the_verified_ones():
    ids = {r["key"]: r["flathub_id"] for r in release_catalog.releases() if r["flathub_id"]}
    assert ids == VERIFIED
    assert all(FLATHUB_ID.match(i) and not i.endswith(".desktop") for i in ids.values())  # the key of the entry, which has no ".desktop"
    assert len(set(ids.values())) == len(ids)  # two projects must never claim one Flathub program
    assert {r["key"] for r in release_catalog.releases() if not r["flathub_id"]} == NOT_ON_FLATHUB


def test_a_program_is_listed_once_not_as_a_vendor_package_and_a_release_download():
    release_ids = {r["flathub_id"] for r in release_catalog.releases() if r["flathub_id"]}
    vendor_ids = {v["flathub_id"] for v in vendor_catalog.vendors() if v["flathub_id"]}
    assert not release_ids & vendor_ids
    assert not {r["key"] for r in release_catalog.releases()} & set(vendor_feeds.FEEDS)
    assert not {r["name"].lower() for r in release_catalog.releases()} & {v["name"].lower() for v in vendor_catalog.vendors()}


def test_formats_follow_what_the_project_has_a_pattern_for():
    for r in release_catalog.releases():
        assert r["formats"] == release_feeds.formats_of(release_feeds.PROJECTS[r["key"]]) and r["formats"]
    assert next(r for r in release_catalog.releases() if r["key"] == "logseq")["formats"] == ["appimage"]


def test_each_entry_is_complete_plain_and_safe_to_show():
    names, keys = set(), set()
    for r in release_catalog.releases():
        assert r["name"] and r["summary"] and r["categories"] and r["icon_name"] and r["developer"]
        assert r["name"] not in names and r["key"] not in keys
        names.add(r["name"])
        keys.add(r["key"])
        assert r["url"].startswith("https://") and "?" not in r["url"] and " " not in r["url"]
        assert all(c.isprintable() for c in r["name"] + r["summary"] + r["developer"])
        assert all(re.fullmatch(r"[A-Za-z0-9]+", c) for c in r["categories"])
        assert len(r["summary"]) <= 200 and r["summary"].endswith(".")
        project = release_feeds.PROJECTS[r["key"]]
        assert r["page"] == f"https://github.com/{project.owner}/{project.repo}/releases"


def test_the_unofficial_wrappers_and_the_unchecked_downloads_are_not_listed():
    keys = set(release_feeds.PROJECTS)
    assert not keys & {"github-desktop", "motrix", "session", "youtube-music", "teams-for-linux", "spotube", "jan"}
