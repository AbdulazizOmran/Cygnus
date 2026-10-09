import json

import builders
from cygnus.cli.main import main


def test_detect_json(tmp_path, capsys):
    deb = builders.build_deb(tmp_path)
    pkg = builders.build_pkg(tmp_path)
    assert main(["detect", "--json", str(deb), str(pkg)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [o["format"] for o in out] == ["deb", "localpkg"]


def test_detect_reports_errors(tmp_path, capsys):
    bad = tmp_path / "x.bin"
    bad.write_bytes(b"\0\1\2")
    assert main(["detect", str(bad)]) == 1
    assert "unrecognised" in capsys.readouterr().out


def test_storage_list_empty_registry(tmp_path, capsys):
    assert main(["--registry", str(tmp_path / "r.db"), "storage", "list"]) == 0
    assert "no storage locations" in capsys.readouterr().out


def test_storage_probe(tmp_path, capsys):
    assert main(["storage", "probe", "--no-ostree", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "AppImages: supported" in out and "probe directory removed: yes" in out


def test_an_unexpected_error_is_reported_without_a_traceback(monkeypatch, capsys):
    from cygnus.cli import main as cli

    def boom(args):
        raise ValueError("something unforeseen")

    parser = cli.build_parser()
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda argv: type("A", (), {"func": staticmethod(boom)})())
    monkeypatch.delenv("CYGNUS_DEBUG", raising=False)
    assert cli.main([]) == 2
    assert "unexpected error (ValueError: something unforeseen)" in capsys.readouterr().err
