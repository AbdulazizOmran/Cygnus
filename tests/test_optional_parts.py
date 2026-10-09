"""Cygnus's own optional tools: which are missing, and installing them only through the helper's usual plan."""

import pytest

from cygnus.core import optional_parts
from cygnus.core.errors import CygnusError
from cygnus.gui import fixes


def test_each_part_is_present_only_when_all_its_programs_and_files_are_there():
    found = {"fusermount3", "zsync", "gcc"}  # make is missing, so the build tools are not complete
    rows = {r["id"]: r for r in optional_parts.status(which=lambda t: f"/usr/bin/{t}" if t in found else None,
                                                      exists=lambda p: False)}
    assert rows["fuse3"]["present"] and rows["zsync"]["present"]
    assert not rows["build"]["present"] and not rows["kservice"]["present"] and not rows["fuse2"]["present"]
    assert rows["fuse2"]["package"] == "fuse2" and rows["build"]["package"] == "base-devel" and rows["fuse3"]["what"]
    again = optional_parts.status(which=lambda t: "/x", exists=lambda p: True)
    assert all(r["present"] for r in again)


def test_the_parts_are_the_ones_the_package_suggests():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "packaging/arch/PKGBUILD").read_text()
    block = text.split("optdepends=(\n")[1].split("\n)")[0]  # up to the line that closes the list (a description may hold a ")")
    suggested = {line.strip().strip("'").split(":")[0] for line in block.splitlines() if line.strip()}
    assert optional_parts.PACKAGES == suggested


class FakeClient:
    calls = []

    def plan_packages(self, **kw):
        FakeClient.calls.append(kw)
        from cygnus.core.privilege import Plan

        return Plan("p1", {}, "Install " + ", ".join(kw["install_repo"]))


def test_installing_parts_asks_the_helper_for_an_ordinary_install_of_exactly_those_packages(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(fixes, "HelperClient", FakeClient)
    out = fixes.plan_optional_parts(["zsync", "fuse3", "zsync"])
    assert FakeClient.calls == [{"install_repo": ["fuse3", "zsync"]}]  # explicit (not "as a dependency"), sorted, once each
    assert out["kind"] == "helper" and out["token"] in fixes._PENDING and out["message"] == "Install fuse3, zsync"
    fixes._PENDING.pop(out["token"], None)


@pytest.mark.parametrize("bad", [[], "fuse3", ["fuse3", "evil"], ["--overwrite=*"], ["base-devel", None], [["zsync"]], None])
def test_anything_that_is_not_one_of_the_optional_parts_is_refused_before_the_helper_is_asked(monkeypatch, bad):
    monkeypatch.setattr(fixes, "HelperClient", lambda: pytest.fail("the helper must not be asked"))
    with pytest.raises(CygnusError, match="not among Cygnus's optional parts"):
        fixes.plan_optional_parts(bad)


def test_the_appimage_message_points_at_the_place_that_installs_it():
    from cygnus.core.backends import appimage

    import inspect

    assert "Optional parts" in inspect.getsource(appimage.prerequisite_issues)
