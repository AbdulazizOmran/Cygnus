"""The small per-user preferences file: a hand edit that breaks it must never cost the person everything in it."""

import json

from cygnus.core import paths, preferences


def test_a_file_that_cannot_be_read_is_kept_aside_when_a_new_one_is_written():
    path = paths.config_dir() / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    broken = '{"dismissed": {"org.a.B": ["x"]}, "handler_backup": {"application/x-rpm": "org.example.Previous.desktop"},}'
    path.write_text(broken)  # a trailing comma left by a hand edit
    assert preferences.load() == {}
    preferences.set_dismissed("org.c.D", "y", True)  # an ordinary change made afterwards
    assert json.loads(path.read_text()) == {"dismissed": {"org.c.D": ["y"]}}
    assert path.with_name("preferences.json.unreadable").read_text() == broken  # nothing was lost


def test_a_file_with_bytes_that_are_not_text_is_kept_aside_too():
    path = paths.config_dir() / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"a": "\xff\xfe"}')
    preferences.save({"b": 1})
    assert path.with_name("preferences.json.unreadable").read_bytes() == b'{"a": "\xff\xfe"}'


def test_a_readable_file_is_replaced_in_place_and_nothing_is_set_aside():
    preferences.save({"a": 1})
    preferences.save({"a": 2})
    assert preferences.load() == {"a": 2}
    assert not list(paths.config_dir().glob("preferences.json.unreadable"))


def test_a_second_unreadable_file_does_not_overwrite_the_first_one_kept_aside():
    path = paths.config_dir() / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"handler_backup": {"x": "first"},}')
    preferences.save({"a": 1})
    path.write_text('{"handler_backup": {"x": "second"},}')
    preferences.save({"a": 2})
    kept = sorted(p.name for p in path.parent.glob("preferences.json.unreadable*"))
    assert kept == ["preferences.json.unreadable", "preferences.json.unreadable.1"]
    assert "first" in path.with_name("preferences.json.unreadable").read_text()
    assert "second" in path.with_name("preferences.json.unreadable.1").read_text()


def test_a_file_that_cannot_be_read_by_permission_is_kept_aside_too():
    import os

    if os.geteuid() == 0:
        return  # root reads anything
    path = paths.config_dir() / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"handler_backup": {"x": "valuable"}}')
    os.chmod(path, 0)
    preferences.save({"a": 1})
    kept = path.with_name("preferences.json.unreadable")
    os.chmod(kept, 0o600)
    assert "valuable" in kept.read_text() and preferences.load() == {"a": 1}


def test_with_a_hundred_copies_already_kept_nothing_is_overwritten():
    import pytest as _pytest

    from cygnus.core.errors import CygnusError

    path = paths.config_dir() / "preferences.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for n in range(100):
        path.with_name("preferences.json.unreadable" + (f".{n}" if n else "")).write_text(f"copy {n}")
    path.write_text("{also bad")
    with _pytest.raises(CygnusError, match="100 copies"):
        preferences.save({"a": 1})
    assert path.read_text() == "{also bad"  # untouched
    assert path.with_name("preferences.json.unreadable.99").read_text() == "copy 99"
