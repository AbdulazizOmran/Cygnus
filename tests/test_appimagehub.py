"""AppImageHub's list read as hostile data: only entries that name a valid GitHub project become records, every field is cut to plain text, and
nothing from the file is ever followed as an address. No test asks the internet; the feed is a fixture."""

import json

import pytest

from cygnus.core.backends import appimagehub as hub
from cygnus.core.errors import CygnusError


def item(name="Tool", repo="acme/tool", *, description="Does things", categories=("Utility",), authors=({"name": "Acme"},), links=None, **extra):
    return {"name": name, "description": description, "categories": list(categories), "authors": list(authors),
            "links": links if links is not None else [{"type": "GitHub", "url": repo}, {"type": "Download", "url": f"https://github.com/{repo}/releases"}], **extra}


def test_an_entry_that_names_a_github_project_becomes_a_record_of_plain_fields():
    [rec] = hub.parse({"items": [item("Tool", "acme/tool", description="Does   things\n", categories=["Utility", "Qt"])]})
    assert rec == {"repo": "acme/tool", "name": "Tool", "summary": "Does things", "categories": ["Utility", "Qt"], "developer": "Acme"}


@pytest.mark.parametrize("links", [[], None, "x", [{"type": "Download", "url": "https://github.com/a/b/releases"}], [{"type": "GitHub"}], [{"type": "GitHub", "url": 5}],
                                   [{"type": "GitLab", "url": "a/b"}], ["a/b"], [{"type": "GitHub", "url": "https://github.com/a/b"}]])
def test_an_entry_without_a_valid_github_link_is_left_out(links):
    assert hub.parse({"items": [{**item(), "links": links}]}) == []


@pytest.mark.parametrize("repo", ["acme/..", "acme/.", "../tool", "ac me/tool", "acme/to ol", "acme/tool/extra", "-acme/tool", "acme/tool.git", "acme/", "/tool", "acme",
                                  "acme/" + "x" * 101, "a" * 40 + "/tool", "ac!me/tool", "acme/to\x00ol", "acme/tool\n", "https://evil.example/a/b"])
def test_an_owner_or_repository_name_that_github_could_not_have_is_left_out(repo):
    assert hub.parse({"items": [item(repo=repo, links=[{"type": "GitHub", "url": repo}])]}) == []


def test_a_project_listed_twice_is_one_record_the_first_wins_whatever_the_capitals():
    out = hub.parse({"items": [item("First", "Acme/Tool"), item("Second", "acme/tool"), item("Other", "acme/other")]})
    assert [(r["name"], r["repo"]) for r in out] == [("First", "Acme/Tool"), ("Other", "acme/other")]


def test_a_project_listed_again_in_another_spelling_is_still_one_record_whichever_spelling_came_first():
    for first, second in (("acme/tool", "Acme/Tool"), ("Acme/Tool", "acme/tool"), ("Acme/Tool", "Acme/Tool"), ("acme/tool", "acme/tool")):
        out = hub.parse({"items": [item("First", first), item("Second", second)]})
        assert [(r["name"], r["repo"]) for r in out] == [("First", first)]


@pytest.mark.parametrize("name, shown", [("-_ICQ", "ICQ"), ("Dead_Rising_2_Case", "Dead Rising 2 Case"), ("Plain", "Plain"), ("  spaced  name ", "spaced name"),
                                         ("-_-", "tool"), ("___", "tool"), ("a‮b\x00c", "a b c")])
def test_the_name_is_made_readable_and_a_name_with_nothing_in_it_is_the_repository(name, shown):
    [rec] = hub.parse({"items": [item(name, "acme/tool")]})
    assert rec["name"] == shown


def test_text_fields_are_cut_and_made_plain_and_wrong_types_are_empty():
    [rec] = hub.parse({"items": [item("T" * 500, "acme/tool", description="d\x1b[31m" * 200, authors=[{"name": "A" * 500}])]})
    assert len(rec["name"]) <= hub.MAX_NAME and len(rec["summary"]) <= hub.MAX_SUMMARY and len(rec["developer"]) <= hub.MAX_DEVELOPER
    assert all(c.isprintable() for c in rec["name"] + rec["summary"] + rec["developer"])
    [odd] = hub.parse({"items": [{**item("T", "acme/tool"), "description": 5, "categories": "Utility", "authors": "Acme"}]})
    assert odd["summary"] == "" and odd["categories"] == [] and odd["developer"] == ""
    [skip] = hub.parse({"items": [item("T", "acme/tool", authors=[5, {"name": 3}, {"name": "Real"}])]})
    assert skip["developer"] == "Real"


def test_only_plain_category_words_are_kept_and_not_too_many():
    [rec] = hub.parse({"items": [item("T", "acme/tool", categories=["Utility", "../x", "a b", 5, None, "Net-work", "x" * 31, "9bad"] + [f"C{i}" for i in range(30)])]})
    assert rec["categories"][:2] == ["Utility", "Net-work"] and len(rec["categories"]) == 12 and all(c[0].isalpha() for c in rec["categories"])


@pytest.mark.parametrize("data", [None, [], "x", {"items": None}, {"items": {}}, {}, 5])
def test_a_file_that_is_not_a_list_of_entries_is_an_error_in_words(data):
    with pytest.raises(CygnusError, match="not in a form Cygnus understands"):
        hub.parse(data)


def test_entries_that_are_not_objects_are_skipped_and_the_rest_are_read():
    out = hub.parse({"items": [None, 5, "x", [], item("Real", "acme/real")]})
    assert [r["repo"] for r in out] == ["acme/real"]


def test_a_huge_list_is_read_only_up_to_a_limit(monkeypatch):
    monkeypatch.setattr(hub, "MAX_ITEMS", 3)
    out = hub.parse({"items": [item(f"T{i}", f"acme/t{i}") for i in range(10)]})
    assert [r["repo"] for r in out] == ["acme/t0", "acme/t1", "acme/t2"]


def test_records_read_back_from_the_cache_are_checked_again_as_strictly():
    good = {"repo": "acme/tool", "name": "Tool", "summary": "s", "categories": ["Utility"], "developer": "d"}
    out = hub.clean([good, {**good, "repo": "acme/.."}, {**good, "repo": "ACME/TOOL"}, {**good, "name": 5}, "x", None, {"repo": "a/b"},
                     {**good, "repo": "acme/other", "name": "N\x1b[0m" * 100, "summary": "s" * 1000, "categories": ["ok", "../x", 5]}])
    assert [r["repo"] for r in out] == ["acme/tool", "acme/other"]  # the capitals of ACME/TOOL are the same project
    assert len(out[1]["name"]) <= hub.MAX_NAME and len(out[1]["summary"]) <= hub.MAX_SUMMARY and out[1]["categories"] == ["ok"]
    assert hub.clean(None) == [] and hub.clean({"a": 1}) == []


def test_fetching_goes_only_to_the_directory_with_a_limit_and_reads_what_it_gets():
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return json.dumps({"items": [item("Tool", "acme/tool")]}).encode()

    lines = []
    out = hub.fetch(lines.append, get=get)
    assert [r["repo"] for r in out] == ["acme/tool"] and lines and seen["url"] == "https://appimage.github.io/feed.json"
    assert seen["limit"] == hub.MAX_FEED and seen["host_ok"]("appimage.github.io")
    assert not seen["host_ok"]("evil.example") and not seen["host_ok"]("github.io") and not seen["host_ok"]("x.appimage.github.io")


@pytest.mark.parametrize("body", [b"not json", b"", b"[" * 100000, b"\xff\xfe"])
def test_a_list_that_is_not_json_is_an_error_in_words(body):
    with pytest.raises(CygnusError, match="was not valid"):
        hub.fetch(get=lambda url, **kw: body)
