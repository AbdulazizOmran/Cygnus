"""Finding a program's own download on its GitHub release page: the file-name patterns against real release data, exactly-one-file selection,
and every way GitHub's answer could be wrong or hostile. No test asks GitHub anything; the answers are fakes (and one real-data fixture)."""

import json
import re
from pathlib import Path

import pytest

from cygnus.core.backends import release_feeds as rf
from cygnus.core.errors import CygnusError
from cygnus.core.util import http

REAL = json.loads((Path(__file__).parent / "data" / "release_assets_2026-10-12.json").read_text())["releases"]
PROJECT = rf.Project("demo", "acme", "demo-app", {"appimage": r"Demo-" + rf.V + r"-x86_64\.AppImage", "deb": r"demo_" + rf.V + r"_amd64\.deb"})


def _asset(name="Demo-1.2.3-x86_64.AppImage", *, tag="v1.2.3", digest="sha256:" + "ab" * 32, size=1000, url=None, **extra):
    return {"name": name, "size": size, "digest": digest, "state": "uploaded",
            "browser_download_url": url or f"https://github.com/acme/demo-app/releases/download/{tag}/{name}", **extra}


def _release(*assets, tag="v1.2.3", **extra):
    return {"tag_name": tag, "draft": False, "prerelease": False, "assets": list(assets), **extra}


def _fetcher(answer):
    seen = {}

    def fetch(url, **kw):
        seen.update(url=url, **kw)
        if isinstance(answer, Exception):
            raise answer
        return answer

    fetch.seen = seen
    return fetch


# -- the patterns against what GitHub really serves ---------------------------------------------------------------------------------
def test_every_pattern_matches_exactly_one_real_file():
    assert set(REAL) == {f"{p.owner}/{p.repo}" for p in rf.PROJECTS.values()}
    for key, project in rf.PROJECTS.items():
        names = [a["name"] for a in REAL[f"{project.owner}/{project.repo}"]["assets"]]
        for fmt, pattern in project.assets.items():
            assert len([n for n in names if re.fullmatch(pattern, n)]) == 1, (key, fmt)


def test_no_pattern_ever_matches_a_file_for_another_processor_a_signature_or_a_checksum():
    for key, project in rf.PROJECTS.items():
        for asset in REAL[f"{project.owner}/{project.repo}"]["assets"]:
            for fmt, pattern in project.assets.items():
                if re.fullmatch(pattern, asset["name"]):
                    lowered = asset["name"].lower()
                    assert not re.search(r"arm|aarch|riscv|i386|i686|ppc|s390|mips|sciter|suse|\.sig|\.asc|\.sha|\.blockmap|\.zsync|\.yml|debug", lowered), (key, asset["name"])


def test_the_real_files_are_the_kind_their_format_says():
    suffix = {"appimage": ".appimage", "deb": ".deb", "rpm": ".rpm", "archive": (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst"), "pkg": ".pkg.tar.zst"}
    for key, project in rf.PROJECTS.items():
        for fmt, pattern in project.assets.items():
            assert pattern.lower().rstrip("$").endswith(tuple(re.escape(s) for s in ((suffix[fmt],) if isinstance(suffix[fmt], str) else suffix[fmt]))), (key, fmt)


def test_the_real_download_addresses_are_exactly_the_form_the_check_demands():
    """The check compares the whole address; GitHub's real addresses for every selected file must pass it (one owner is spelled differently in the
    address than in the project list, which is allowed for the owner and repository only)."""
    for key, project in rf.PROJECTS.items():
        release = REAL[f"{project.owner}/{project.repo}"]
        for fmt, pattern in project.assets.items():
            [asset] = [a for a in release["assets"] if re.fullmatch(pattern, a["name"])]
            assert rf._is_own_release_file(asset["url"], project, release["tag"], asset["name"]), (key, fmt, asset["url"])
            assert not rf._is_own_release_file(asset["url"] + "x", project, release["tag"], asset["name"])


def test_the_project_list_is_well_formed():
    assert len(rf.PROJECTS) == 24
    for key, project in rf.PROJECTS.items():
        assert key == project.key and rf._KEY.fullmatch(key)
        assert rf._OWNER.fullmatch(project.owner) and rf._REPO.fullmatch(project.repo)
        assert project.assets and set(project.assets) <= set(rf.FORMATS)
        for pattern in project.assets.values():
            re.compile(pattern)
            assert "{v}" not in pattern and not pattern.endswith(".*") and not pattern.startswith(".*")  # a placeholder left in, or a wildcard end
    assert len({(p.owner.lower(), p.repo.lower()) for p in rf.PROJECTS.values()}) == len(rf.PROJECTS)


def test_formats_come_in_the_order_the_buttons_are_shown():
    assert rf.formats_of(rf.PROJECTS["rustdesk"]) == ["appimage", "deb", "rpm", "pkg"]
    assert rf.formats_of(rf.PROJECTS["helium"]) == ["appimage", "deb", "archive"]


# -- choosing the file --------------------------------------------------------------------------------------------------------------
def test_the_newest_release_gives_the_one_matching_file_with_its_checksum():
    fetch = _fetcher(_release(_asset(), _asset("Demo-1.2.3-arm64.AppImage"), _asset("demo_1.2.3_amd64.deb")))
    got = rf.newest(PROJECT, "appimage", fetch=fetch)
    assert (got.version, got.name, got.format, got.size) == ("v1.2.3", "Demo-1.2.3-x86_64.AppImage", "appimage", 1000)
    assert got.sha256 == "ab" * 32 and got.url == "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage"
    assert fetch.seen["url"] == "https://api.github.com/repos/acme/demo-app/releases/latest" and fetch.seen["limit"] == rf.MAX_ANSWER
    assert rf.newest(PROJECT, "deb", fetch=fetch).name == "demo_1.2.3_amd64.deb"


def test_no_matching_file_is_an_error_in_words():
    with pytest.raises(CygnusError, match="no AppImage file Cygnus recognises"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset("demo_1.2.3_amd64.deb"))))
    with pytest.raises(CygnusError, match="no AppImage file"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release()))


def test_several_matching_files_are_never_resolved_by_taking_the_first():
    two = _release(_asset("Demo-1.2.3-x86_64.AppImage"), _asset("Demo-1.2.4-x86_64.AppImage"))
    with pytest.raises(CygnusError, match="more than one file.*will not guess"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(two))


def test_a_format_the_project_has_no_pattern_for_is_refused_before_asking():
    fetch = _fetcher(_release())
    with pytest.raises(CygnusError, match="no .rpm download"):
        rf.newest(PROJECT, "rpm", fetch=fetch)
    with pytest.raises(CygnusError):
        rf.newest(PROJECT, "../etc", fetch=fetch)
    assert fetch.seen == {}


def test_the_whole_file_name_must_match():
    for name in ("xDemo-1.2.3-x86_64.AppImage", "Demo-1.2.3-x86_64.AppImage.zsync", "Demo-1.2.3-x86_64.AppImage\n", "Demo-1.2.3-x86_64.AppImage.sha256",
                 "Demo-1.2.3-rc1-x86_64.AppImage"):
        with pytest.raises(CygnusError, match="no AppImage file"):
            rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(name))))


@pytest.mark.parametrize("url", [
    "https://evil.example/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "http://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/other/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/other-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/archive/Demo-1.2.3-x86_64.AppImage",
    "https://github.com@evil.example/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage extra",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Other-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage\\..",
    "https://github.com/acme/demo-app/releases/download/" + "x" * 600, "", None, 5, ["https://github.com"],
    # an address that only BEGINS like the project's: dot segments, escapes, a query or a fragment in the middle reach another repository
    "https://github.com/acme/demo-app/releases/download/../../../../someone/else/raw/main/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/%2e%2e/%2e%2e/%2e%2e/someone/else/raw/main/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/../v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/x?/../../../../someone/else/raw/main/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/x#/../../../../someone/else/raw/main/Demo-1.2.3-x86_64.AppImage",
    "HTTPS://GITHUB.COM/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://GitHub.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/v9.9.9/Demo-1.2.3-x86_64.AppImage",  # another release's folder
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64%2EAppImage",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage/",
    "https://github.com/acme/demo-app/releases/download/v1.2.3//Demo-1.2.3-x86_64.AppImage",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage?token=1",
    "https://github.com/acme/demo-app/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage#x"])
def test_a_download_address_that_is_not_the_projects_own_release_file_is_refused(url):
    asset = _asset()
    asset["browser_download_url"] = url
    with pytest.raises(CygnusError, match="not that project's own release address"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(asset)))


def test_a_differently_cased_owner_in_the_address_is_the_same_project():
    asset = _asset(url="https://github.com/ACME/Demo-App/releases/download/v1.2.3/Demo-1.2.3-x86_64.AppImage")
    assert rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(asset))).name == "Demo-1.2.3-x86_64.AppImage"


@pytest.mark.parametrize("digest,expected", [("sha256:" + "ab" * 32, "ab" * 32), (None, None), ("", None), ("sha256:" + "AB" * 32, None), ("sha256:abc", None),
                                              ("sha1:" + "ab" * 20, None), ("sha256:" + "ab" * 32 + "\n", None), (5, None), ("md5:" + "ab" * 16, None)])
def test_only_a_well_formed_sha256_digest_is_a_checksum(digest, expected):
    got = rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(digest=digest))))
    assert got.sha256 == expected


@pytest.mark.parametrize("size", [None, 0, -5, True, False, "1000", 1.5, rf.MAX_FILE + 1, [], {}])
def test_a_missing_or_absurd_size_is_refused(size):
    with pytest.raises(CygnusError, match="size"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(size=size))))


def test_the_largest_allowed_file_is_allowed():
    assert rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(size=rf.MAX_FILE)))).size == rf.MAX_FILE


@pytest.mark.parametrize("tag", [None, 5, "", " v1", "v1\n", "../x", "v1 2", "x" * 80, ["v1"], "-v1", "v1;rm"])
def test_a_release_without_a_usable_version_is_refused(tag):
    with pytest.raises(CygnusError, match="no usable version"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(), tag=tag)))


@pytest.mark.parametrize("answer", [None, [], "text", 5, {"tag_name": "v1"}, {"tag_name": "v1", "assets": "x"}, {"tag_name": "v1", "assets": {}},
                                    {"tag_name": "v1", "assets": None}])
def test_an_answer_that_is_not_a_release_is_refused(answer):
    with pytest.raises(CygnusError):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(answer))


def test_a_draft_or_a_prerelease_is_never_offered():
    for flag in ("draft", "prerelease"):
        with pytest.raises(CygnusError, match="not a normal release"):
            rf.newest(PROJECT, "appimage", fetch=_fetcher({**_release(_asset()), flag: True}))


def test_a_file_that_is_still_uploading_is_refused():
    with pytest.raises(CygnusError, match="not fully uploaded"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(state="starter"))))


def test_junk_in_the_list_of_files_is_skipped():
    junk = [None, 5, "x", [], {}, {"name": 5}, {"name": None}]
    assert rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(*junk, _asset()))).name == "Demo-1.2.3-x86_64.AppImage"


def test_a_list_too_long_to_look_through_is_refused_rather_than_looked_at_in_part():
    """Found by review: only the first 300 files were looked at, so a second match further down would have been missed and the first one picked."""
    exactly = _release(*([{"name": "x"}] * (rf.MAX_ASSETS - 1)), _asset())
    assert rf.newest(PROJECT, "appimage", fetch=_fetcher(exactly)).name == "Demo-1.2.3-x86_64.AppImage"  # 300 files are looked through
    late = _release(*([{"name": "x"}] * rf.MAX_ASSETS), _asset())
    with pytest.raises(CygnusError, match="more files than Cygnus will look through"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(late))


def test_a_file_name_longer_than_any_real_one_never_matches():
    long_name = "Demo-" + "1" * 300 + "-x86_64.AppImage"
    with pytest.raises(CygnusError, match="no AppImage file"):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(_release(_asset(long_name))))


@pytest.mark.parametrize("status,words", [(403, "limiting"), (429, "limiting"), (404, "no release"), (500, "could not ask GitHub"), (None, "could not ask GitHub")])
def test_what_went_wrong_asking_github_is_said_in_words(status, words):
    with pytest.raises(CygnusError, match=words):
        rf.newest(PROJECT, "appimage", fetch=_fetcher(http.HttpError("HTTP failure", status)))


def test_a_project_whose_address_is_not_well_formed_is_not_asked_about():
    fetch = _fetcher(_release(_asset()))
    for owner, repo in (("a/b", "x"), ("a", "../x"), ("", "x"), ("a", ""), ("a b", "x"), ("a", "x?y=1")):
        with pytest.raises(CygnusError, match="does not look right"):
            rf.newest(rf.Project("demo", owner, repo, PROJECT.assets), "appimage", fetch=fetch)
    assert fetch.seen == {}


def test_a_tag_is_shown_only_as_plain_text():
    assert rf._plain("v1\x1b[31m\nred") == "v1 [31m red"
    assert len(rf._plain("x" * 500)) == 80


def test_a_release_download_may_only_be_on_githubs_own_hosts():
    for host in ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"):
        assert rf.github_host(host)
    for host in ("", "evil.example", "github.com.evil.example", "githubusercontent.com.evil.example", "notgithub.com", "api.github.com.evil.example",
                 "evilgithubusercontent.com", "raw.githubusercontent.com.evil.example"):
        assert not rf.github_host(host)
