import json

import pytest

import builders
from cygnus.cli.main import main
from cygnus.core.desktop import integrate
from cygnus.core.registry import open_registry
from cygnus.core.registry.db import StorageLocation
from cygnus.core.storage import locations

pytestmark = pytest.mark.needs_tool("mksquashfs", "unsquashfs")


def test_adopt_list_uninstall(tmp_path, capsys):
    reg = str(tmp_path / "r.db")
    app = builders.build_appimage(tmp_path)
    assert main(["--registry", reg, "adopt", "--yes", str(app)]) == 0
    assert (integrate.xdg_data_home() / "applications/hello.desktop").exists()
    capsys.readouterr()
    assert main(["--registry", reg, "list", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["origin"] == "adopted" and row["name"] == "Hello World"
    assert main(["--registry", reg, "uninstall", "--yes", "Hello World"]) == 0
    assert not (integrate.xdg_data_home() / "applications/hello.desktop").exists()
    assert app.exists()


def test_install_to_location(tmp_path, monkeypatch, capsys):
    reg_path = tmp_path / "r.db"
    reg = open_registry(reg_path)
    reg.add_location(StorageLocation(id="hdd", label="HDD", fs_uuid="X", fs_type="ext4", location_class="posix",
                                     canonical_mount=str(tmp_path / "hdd"), user_apps_dir="Applications"))
    reg.close()
    (tmp_path / "hdd").mkdir()
    monkeypatch.setattr(locations, "resolve", lambda loc, cands=None: locations.ResolvedLocation(
        loc, True, str(tmp_path / "hdd")))
    src = tmp_path / "dl"
    src.mkdir()
    app = builders.build_appimage(src)
    assert main(["--registry", str(reg_path), "install", "--yes", str(app), "--to", "hdd"]) == 0
    installed = list((tmp_path / "hdd/Applications").rglob("*.AppImage"))
    assert len(installed) == 1
    assert main(["--registry", str(reg_path), "uninstall", "--yes", "--delete-file", "Hello World"]) == 0
    assert not installed[0].exists() and app.exists()


def test_uninstall_unknown(tmp_path, capsys):
    assert main(["--registry", str(tmp_path / "r.db"), "uninstall", "--yes", "nothing"]) == 2


def test_install_asks_for_a_location_only_where_there_is_a_choice(tmp_path, capsys):
    app = builders.build_appimage(tmp_path)
    assert main(["--registry", str(tmp_path / "r.db"), "install", "--yes", str(app)]) == 2
    assert "choose where to store the AppImage with --to" in capsys.readouterr().err
    from cygnus.cli.main import build_parser

    args = build_parser().parse_args(["install", "tool.deb"])  # no --to needed for system packages
    assert args.to is None
