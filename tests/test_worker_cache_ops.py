"""The worker's two operations for cleaning the package cache: libalpm's version order, and what is installed."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pyalpm
import pytest

import builders
from cygnus.core.backends import alpm_worker as w
from tests.test_packages_worker import _run, _scratch


def test_versions_are_sorted_newest_first_by_libalpms_rules():
    answer = w.op_sort_versions(None, {"groups": [["1.9-1", "1.10-1", "1:0.1-1", "1.2rc1-1", "1.2-1"], [], ["1-1"]]})
    assert answer == {"ok": True, "orders": [["1:0.1-1", "1.10-1", "1.9-1", "1.2-1", "1.2rc1-1"], [], ["1-1"]]}
    versions = ["2.0-1", "10.0-1", "2.0-2", "2.0~rc1-1", "2.0.1-1"]
    assert w.op_sort_versions(None, {"groups": [versions]})["orders"][0] == sorted(versions, key=__import__("functools").cmp_to_key(pyalpm.vercmp), reverse=True)


@pytest.mark.parametrize("bad", [{}, {"groups": "x"}, {"groups": ["1-1"]}, {"groups": [[1]]}, {"groups": [["1-1", None]]}, {"groups": [["1-1"] * (w.MAX_VERSIONS + 1)]},
                                 {"groups": [["1-1"]] * (w.MAX_VERSIONS + 1)}])
def test_a_request_that_is_not_groups_of_version_texts_is_refused_not_crashed(bad):
    assert w.op_sort_versions(None, bad)["ok"] is False


def test_the_installed_versions_are_a_name_to_version_map():
    local = SimpleNamespace(pkgcache=[SimpleNamespace(name="a", version="1-1"), SimpleNamespace(name="b", version="1:2-3")])
    h = SimpleNamespace(get_localdb=lambda: local)
    assert w.op_installed_versions(h, {}) == {"ok": True, "versions": {"a": "1-1", "b": "1:2-3"}}


def test_the_repository_names_are_every_name_in_every_list_once_and_sorted():
    core = SimpleNamespace(pkgcache=[SimpleNamespace(name="zsh"), SimpleNamespace(name="bash")])
    extra = SimpleNamespace(pkgcache=[SimpleNamespace(name="bash"), SimpleNamespace(name="alpha")])
    h = SimpleNamespace(get_syncdbs=lambda: [core, extra])
    assert w.op_repo_names(h, {}) == {"ok": True, "names": ["alpha", "bash", "zsh"]}
    assert w.op_repo_names(SimpleNamespace(get_syncdbs=lambda: []), {}) == {"ok": True, "names": []}


@pytest.mark.needs_tool("bsdtar", "zstd", "repo-add")
def test_the_real_worker_answers_both(tmp_path):
    base = _scratch(tmp_path)
    assert _run(base, op="installed_versions")["versions"] == {"alpha": "1.0-1", "libbeta": "1.0-1", "leftover": "1.0-1", "handmade": "3-1"}
    assert _run(base, op="sort_versions", groups=[["1.0-1", "2.0-1", "1.5-1"]])["orders"] == [["2.0-1", "1.5-1", "1.0-1"]]


def _local(*pkgs):
    return SimpleNamespace(get_localdb=lambda: SimpleNamespace(get_pkg=lambda name: next((p for p in pkgs if p.name == name), None)))


def _pkg(name, reason=0, required_by=()):
    return SimpleNamespace(name=name, reason=reason, compute_requiredby=lambda: list(required_by))


def test_removal_facts_say_per_package_whether_it_is_installed_why_and_what_needs_it(monkeypatch):
    h = _local(_pkg("lib", reason=1, required_by=["app", "other"]), _pkg("app", reason=0))
    got = w.op_removal_facts(h, {"names": ["lib", "app", "ghost"]})
    assert got == {"ok": True, "packages": {"lib": {"installed": True, "reason": "dependency", "required_by": ["app", "other"], "required_by_more": 0},
                                            "app": {"installed": True, "reason": "explicit", "required_by": [], "required_by_more": 0},
                                            "ghost": {"installed": False}}}


def test_the_list_of_what_needs_a_package_is_capped_and_says_how_many_more(monkeypatch):
    monkeypatch.setattr(w, "LIST_LIMIT", 3)
    got = w.op_removal_facts(_local(_pkg("lib", required_by=[f"r{i}" for i in range(10)])), {"names": ["lib"]})["packages"]["lib"]
    assert got["required_by"] == ["r0", "r1", "r2"] and got["required_by_more"] == 7


@pytest.mark.parametrize("bad", [{}, {"names": "lib"}, {"names": [1]}, {"names": ["a", None]}, {"names": ["x"] * (w.MAX_FACT_NAMES + 1)}])
def test_removal_facts_refuse_what_is_not_a_list_of_names(bad):
    assert w.op_removal_facts(_local(), bad)["ok"] is False


@pytest.mark.needs_tool("bsdtar", "zstd", "repo-add")
def test_the_real_worker_answers_removal_facts(tmp_path):
    base = _scratch(tmp_path)
    got = _run(base, op="removal_facts", names=["libbeta", "alpha", "leftover", "nothing"])["packages"]
    assert got["libbeta"] == {"installed": True, "reason": "dependency", "required_by": ["alpha"], "required_by_more": 0}
    assert got["alpha"]["required_by"] == [] and got["leftover"]["reason"] == "dependency" and got["nothing"] == {"installed": False}


@pytest.mark.needs_tool("bsdtar", "zstd", "repo-add")
def test_the_real_worker_lists_the_repositories_names(tmp_path):
    assert _run(_scratch(tmp_path), op="repo_names")["names"] == ["alpha", "collide", "delta", "gamma", "libbeta"]  # "collide" is in two lists and named once
