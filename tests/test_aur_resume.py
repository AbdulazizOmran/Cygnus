"""Leaving the install page half-way through an AUR chain must not lose track of what was left (gui 8)."""

import pytest

from cygnus.gui import fixes, service


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    fixes._CHAINS.clear()
    calls = []
    monkeypatch.setattr(service, "record_aur_install", lambda *a, **k: calls.append((a, k)) or "id")
    yield calls
    fixes._CHAINS.clear()


DEP = {"kind": "aur", "pkgbase": "lib", "name": "lib", "commit": "c1", "version": "1-1", "names": ["lib", "lib-docs"],
       "dependency_of": "app"}
MAIN = {"kind": "aur", "pkgbase": "app", "name": "app", "commit": "c2", "version": "2-1"}


def test_after_a_dependency_is_installed_the_unfinished_package_is_remembered():
    fixes._recording(DEP)()
    assert fixes.interrupted_aur_chains() == [{"target": "app", "dependencies": ["lib"]}]


def test_installing_the_package_itself_finishes_the_chain():
    fixes._recording(DEP)()
    fixes._recording(MAIN)()
    assert fixes.interrupted_aur_chains() == []


def test_two_dependencies_are_listed_once_each_in_install_order():
    fixes._recording(DEP)()
    fixes._recording({**DEP, "pkgbase": "lib2", "name": "lib2", "names": ["lib2"]})()
    fixes._recording(DEP)()  # the same dependency again (a retry)
    assert fixes.interrupted_aur_chains() == [{"target": "app", "dependencies": ["lib", "lib2"]}]


def test_a_record_that_fails_to_save_changes_nothing(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(service, "record_aur_install", boom)
    with pytest.raises(OSError):
        fixes._recording(DEP)()
    assert fixes.interrupted_aur_chains() == []


def test_a_package_without_dependencies_leaves_nothing_behind():
    fixes._recording(MAIN)()
    assert fixes.interrupted_aur_chains() == []


def test_dismissing_forgets_only_that_package():
    fixes._recording(DEP)()
    fixes._recording({**DEP, "dependency_of": "other", "name": "libx", "names": ["libx"], "pkgbase": "libx"})()
    fixes.dismiss_aur_chain("app")
    assert fixes.interrupted_aur_chains() == [{"target": "other", "dependencies": ["libx"]}]
    fixes.dismiss_aur_chain("app")  # already gone: nothing to do


def test_the_install_page_asks_for_unfinished_installs_and_offers_to_continue_from_a_fresh_review():
    from pathlib import Path

    qml = (Path(__file__).resolve().parent.parent / "cygnus" / "gui" / "qml" / "InstallPage.qml").read_text()
    assert "backend.interruptedAurChains()" in qml and "backend.dismissAurChain(" in qml
    resume = qml[qml.index("function resumeChain"):qml.index("function dismissChain")]
    assert 'appSpecField.text = "aur:" + chain.target' in resume and "analyse()" in resume
    assert "aurBuild" not in resume and "install()" not in resume  # continuing never skips the review


def test_an_unfinished_install_survives_closing_and_reopening_cygnus():
    fixes._chain_progress("dep-one", "bigapp")
    fixes._chain_progress("dep-two", "bigapp")
    fixes._CHAINS.clear()  # Cygnus is closed ...
    fixes._reset_loaded()  # ... and opened again: the state is read from its file
    assert fixes.interrupted_aur_chains() == [{"target": "bigapp", "dependencies": ["dep-one", "dep-two"]}]
    fixes.dismiss_aur_chain("bigapp")
    fixes._CHAINS.clear()
    fixes._reset_loaded()
    assert fixes.interrupted_aur_chains() == []  # dismissed for good, not just for this run


def test_a_finished_install_is_forgotten_by_the_file_too():
    fixes._chain_progress("dep-one", "bigapp")
    fixes._chain_progress("bigapp", None)  # the package itself went in
    fixes._CHAINS.clear()
    fixes._reset_loaded()
    assert fixes.interrupted_aur_chains() == []


def test_a_damaged_state_file_is_ignored_not_fatal():
    from cygnus.core import paths

    (paths.state_dir()).mkdir(parents=True, exist_ok=True)
    (paths.state_dir() / "aur-chains.json").write_text('{"a": "not a list", "b": [1, 2], "c": ["ok"]}')
    fixes._CHAINS.clear()
    fixes._reset_loaded()
    assert fixes.interrupted_aur_chains() == [{"target": "c", "dependencies": ["ok"]}]
