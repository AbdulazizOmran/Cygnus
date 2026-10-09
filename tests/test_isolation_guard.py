"""Tests must never undo conftest's isolation (XDG, runtime and Flatpak dirs pointing at tmp)."""

import re
from pathlib import Path


def test_no_test_undoes_the_isolation():
    offenders = []
    for f in Path(__file__).parent.glob("test_*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bmonkeypatch\.undo\(\)", code):
                offenders.append(f"{f.name}:{n}")
    assert offenders == [], "use pytest.MonkeyPatch.context() instead: " + ", ".join(offenders)


def test_every_xdg_directory_points_into_the_test_folder(tmp_path):
    import os

    from cygnus.core import paths

    for d in (paths.data_dir(), paths.config_dir(), paths.cache_dir(), paths.state_dir(), paths.runtime_dir()):
        assert str(d).startswith(str(tmp_path)), d
    assert os.environ["FLATPAK_USER_DIR"].startswith(str(tmp_path))
