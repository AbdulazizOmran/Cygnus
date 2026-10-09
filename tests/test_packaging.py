"""Packaging stays consistent: one version everywhere, both recipes install the same files, which exist."""

import re
from pathlib import Path

import cygnus

ROOT = Path(__file__).resolve().parent.parent
INTREE = ROOT / "packaging/arch/PKGBUILD"
RELEASE = ROOT / "packaging/arch/release/PKGBUILD"


def _installs(pkgbuild: Path) -> list[str]:
    return sorted(l.strip() for l in pkgbuild.read_text().splitlines() if l.strip().startswith(("install -D", "for size")))


def test_both_recipes_install_the_same_files():
    assert _installs(INTREE) == _installs(RELEASE)


def test_every_installed_file_exists():
    app_id = cygnus.APP_ID
    for line in _installs(INTREE):
        if not line.startswith("install"):
            continue
        src = line.split()[2].strip('"').replace("$id", app_id)
        assert (ROOT / src).is_file(), src


def test_one_version_everywhere():
    for recipe in (INTREE, RELEASE):
        assert re.search(r"^pkgver=(.+)$", recipe.read_text(), re.M).group(1) == cygnus.__version__
    meta = (ROOT / "data/metainfo/io.github.omranabdulaziz.Cygnus.metainfo.xml").read_text()
    assert f'<release version="{cygnus.__version__}"' in meta
    assert 'dynamic = ["version"]' in (ROOT / "pyproject.toml").read_text()


def test_menu_entry_uses_the_shipped_icon():
    desktop = (ROOT / "data/applications/io.github.omranabdulaziz.Cygnus.desktop").read_text()
    assert f"Icon={cygnus.APP_ID}\n" in desktop
    assert (ROOT / f"cygnus/gui/icons/{cygnus.APP_ID}.svg").is_file()
