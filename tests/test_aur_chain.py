"""AUR packages that need other AUR packages: every package reviewed, built and installed in order."""

import subprocess
from pathlib import Path

import pytest

from cygnus.core.backends import aur, pacman as pm
from cygnus.core.ops import aur_ops
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.gui import service

pytestmark = pytest.mark.needs_tool("git")


def _publish(root: Path, base: str, depends: str = ""):
    bare = root / f"{base}.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "master", str(bare)], check=True)
    work = root / f"work-{base}"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True, capture_output=True)
    (work / "PKGBUILD").write_text(f"pkgname={base}\npkgver=1.0\npkgrel=1\narch=(any)\nlicense=(MIT)\n"
                                   f"depends=({depends})\npackage() {{ :; }}\n")
    deps = "".join(f"\tdepends = {d}\n" for d in depends.split())
    (work / ".SRCINFO").write_text(f"pkgbase = {base}\n\tpkgver = 1.0\n\tpkgrel = 1\n\tarch = any\n{deps}\n"
                                   f"pkgname = {base}\n")
    for cmd in (["add", "-A"], ["commit", "--quiet", "-m", "v1"], ["push", "--quiet", "origin", "HEAD:master"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *cmd], cwd=work, check=True,
                       capture_output=True)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    root = tmp_path / "aur"
    root.mkdir()
    _publish(root, "hello-dep")
    _publish(root, "hello-app", depends="hello-dep")
    monkeypatch.setattr(aur_ops, "AUR_GIT", f"file://{root}")
    meta = {n: {"Name": n, "PackageBase": n, "Version": "1.0-1", "NumVotes": 1, "Maintainer": "t"}
            for n in ("hello-dep", "hello-app")}
    monkeypatch.setattr(aur, "info", lambda names, **kw: {n: meta[n] for n in names if n in meta})
    monkeypatch.setattr(aur, "resolve", lambda targets, **kw: aur.AurPlan(
        targets=targets, build_order=["hello-dep", "hello-app"], packages=meta))
    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    monkeypatch.setattr(pm, "read_config", lambda: cfg)
    monkeypatch.setattr(pm, "run_worker", lambda c, req: {"satisfiers": {d: {"installed": None, "repo": None}
                                                                         for d in req["deps"]}})


def test_the_whole_chain_is_reviewed(chain):
    r = service.aur_review("hello-app")
    assert r["aur_dependencies"] == ["hello-dep"] and r["buildable"]
    [dep] = r["dependencies"]
    assert dep["pkgbase"] == "hello-dep" and dep["names"] == ["hello-dep"] and "PKGBUILD" in dep["files"]


def test_an_incomplete_dependency_review_blocks_everything(chain, monkeypatch):
    real = aur_ops.review

    def review(checkout):
        r = real(checkout)
        if checkout.name == "hello-dep":
            r.complete, r.problems = False, ["it has 500 files"]
        return r

    monkeypatch.setattr(aur_ops, "review", review)
    assert not service.aur_review("hello-app")["buildable"]


def test_cli_builds_and_installs_dependencies_first(chain, monkeypatch, tmp_path, capsys):
    from cygnus.cli import main as cli
    from cygnus.core import privilege

    events = []
    monkeypatch.setattr(service, "aur_build", lambda base, commit, progress, name=None, names=None: (
        events.append(("build", base)) or {"packages": [str(tmp_path / f"{base}.pkg.tar.zst")]}))
    monkeypatch.setattr(service, "file_sha256", lambda p: "0" * 64)
    from cygnus.core.models import Candidate, PackageFormat

    monkeypatch.setattr("cygnus.core.detect.detect_file", lambda p: Candidate(
        format=PackageFormat.LOCAL_PKG, source=p, name=Path(p).name.split(".")[0]))

    class Client:
        def plan_packages(self, **kw):
            events.append(("plan", Path(kw["local_files"][0][0]).name.split(".")[0], kw.get("asdeps", False)))
            return privilege.Plan("p", {}, "Install")

        def commit(self, plan, on_progress=None):
            events.append(("commit",))
            return True, ""

    monkeypatch.setattr(privilege, "HelperClient", Client)
    r = service.aur_review("hello-app")
    args = ["aur", "install", "hello-app", "--yes", "--reviewed", r["commit"],
            "--reviewed", f"hello-dep={r['dependencies'][0]['commit']}"]
    assert cli.main(args) == 0, capsys.readouterr().out
    assert events == [("build", "hello-dep"), ("plan", "hello-dep", True), ("commit",),
                      ("build", "hello-app"), ("plan", "hello-app", False), ("commit",)]
    rows = {r["name"]: r for r in regdb.list_installations(open_registry())}
    assert rows["hello-dep"]["source"]["dependency_of"] == "hello-app" and "dependency_of" not in rows["hello-app"]["source"]


def test_cli_refuses_when_a_dependency_was_not_named_as_reviewed(chain, capsys):
    from cygnus.cli.main import main

    r = service.aur_review("hello-app")
    assert main(["aur", "install", "hello-app", "--yes", "--reviewed", r["commit"]]) == 2
    assert "review them again" in capsys.readouterr().err
