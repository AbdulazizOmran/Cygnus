import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_tool(name): skip unless the tool is installed")


def pytest_runtest_setup(item):
    for mark in item.iter_markers(name="needs_tool"):
        for tool in mark.args:
            if shutil.which(tool) is None:
                pytest.skip(f"requires '{tool}'")


@pytest.fixture(scope="session")
def _flatpak_system(tmp_path_factory):
    # libflatpak reads these once per process, so they must stay the same for the whole session.
    root = tmp_path_factory.mktemp("flatpak-system")
    (root / "config").mkdir()
    return root


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch, _flatpak_system):
    """Never let a test write to (or read) the real user's XDG directories or Flatpak installations."""
    for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_STATE_HOME", "state")):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / sub))
    runtime = tmp_path / "xdg" / "run"
    runtime.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("FLATPAK_USER_DIR", str(tmp_path / "xdg" / "flatpak-user"))
    monkeypatch.setenv("FLATPAK_SYSTEM_DIR", str(_flatpak_system / "system"))
    monkeypatch.setenv("FLATPAK_CONFIG_DIR", str(_flatpak_system / "config"))
