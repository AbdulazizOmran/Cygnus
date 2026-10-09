"""Interrupted operations: listed once the owning process is gone, then finished or undone."""

import pytest

import builders
from cygnus.core.desktop import integrate
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("mksquashfs", "unsquashfs")


@pytest.fixture
def interrupted_adopt(tmp_path):
    app = builders.build_appimage(tmp_path)

    def power_loss():
        raise KeyboardInterrupt  # the process dies after the menu files were written

    # A scoped patch: monkeypatch.undo() would also undo conftest's XDG isolation.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrate, "refresh_caches", power_loss)
        with pytest.raises(KeyboardInterrupt):
            service.adopt_appimage(str(app), lambda _: None)
    return app


def test_interrupted_operation_is_offered(interrupted_adopt):
    [op] = service.interrupted_operations()
    assert op["title"] == "Adding Hello World to Cygnus" and op["state"] == "running"
    assert op["progress"].startswith(f"{len(op['steps']) - 2} of {len(op['steps'])}")
    assert not service.is_managed(str(interrupted_adopt))


def test_finish_it(interrupted_adopt):
    [op] = service.interrupted_operations()
    lines = []
    assert service.recover(op["id"], "resume", lines.append)["ok"]
    assert service.is_managed(str(interrupted_adopt))
    assert (integrate.xdg_data_home() / "applications/hello.desktop").exists()
    assert service.interrupted_operations() == []


def test_undo_it(interrupted_adopt):
    [op] = service.interrupted_operations()
    assert service.recover(op["id"], "rollback", lambda _: None)["ok"]
    assert not service.is_managed(str(interrupted_adopt))
    assert not (integrate.xdg_data_home() / "applications/hello.desktop").exists()
    assert interrupted_adopt.exists()  # the user's file is never touched by an undo
    assert service.interrupted_operations() == []


def test_cli_recover(interrupted_adopt, capsys, monkeypatch):
    from cygnus.cli.main import main

    assert main(["recover"]) == 1
    op_id = capsys.readouterr().out.split()[0]
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError))
    assert main(["recover", "--undo", op_id]) == 1  # asks first; no answer means no
    assert "cancelled" in capsys.readouterr().out
    assert main(["recover"]) == 1  # still there
    assert main(["recover", "--finish", "nonexistent"]) == 2
    assert main(["recover", "--undo", op_id, "--yes"]) == 0
    assert main(["recover"]) == 0
    assert "no interrupted operations" in capsys.readouterr().out
