"""Packages installed through the helper are recorded once, and can always be removed again."""

from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.gui import service


def test_converted_package_is_recorded():
    iid = service.record_package_install("hello", "1.0-1", origin="converted", source={"file": "/x/hello.deb"})
    [row] = regdb.list_installations(open_registry())
    assert row["id"] == iid and row["format"] == "pacman" and row["origin"] == "installed"
    assert row["source"]["made_by"] == "converted"
    assert service.package_names(iid) == ["hello"]


def test_reinstalling_updates_instead_of_duplicating():
    first = service.record_aur_install("foo", "foo", "a" * 40, "1.0-1")
    second = service.record_aur_install("foo", "foo", "b" * 40, "1.1-1")
    assert first == second
    [row] = regdb.list_installations(open_registry())
    assert row["version"] == "1.1-1" and row["source"]["reviewed_commit"] == "b" * 40


def test_split_packages_are_all_recorded_and_ghosts_dropped():
    old = service.record_package_install("foo", "1.0-1", origin="local", source={})
    iid = service.record_aur_install("foo-split", "foo", "c" * 40, "2.0-1", packages=["foo", "foo-docs"])
    assert sorted(service.package_names(iid)) == ["foo", "foo-docs"]
    assert [r["id"] for r in regdb.list_installations(open_registry())] == [iid]  # the empty entry is gone
    assert old != iid
