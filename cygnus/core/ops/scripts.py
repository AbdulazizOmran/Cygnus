"""Install scripts that come with a program, run only when the person chose to run them (the default is never).

A script is listed with what it mentions, shown in full, and run only if its box was ticked for it. What runs is exactly the bytes
that were shown (their SHA-256 is carried from the analysis and checked against the file again, and the text goes to the
interpreter on its command line, so there is no file to swap in between). A script runs once: an attempt that was interrupted is
never repeated by a resume. A script that fails, or is refused, never undoes the installation that already worked."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from cygnus.core import paths
from cygnus.core.util import scriptrun
from cygnus.core.util.scriptrun import ScriptChanged, ScriptRefused

_REMOVE = re.compile(r"(?i)^\.?(?:(?:pre|post)rm|uninstall)(?:\.(?:sh|bash))?$")
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_SESSION = ("XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")


def phase_of(name: str) -> str:
    """"remove" for a script that belongs to taking the program away (prerm, postrm, uninstall), "install" for the others."""
    return "remove" if _REMOVE.match(name.rsplit("/", 1)[-1]) else "install"


def new_run_id() -> str:
    return str(uuid.uuid4())


def user_env() -> dict[str, str]:
    """What a script run as you sees: your home, name and desktop session, a plain PATH, and nothing else of this process's
    environment (a token or a key in it is not handed to a script)."""
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError):  # no HOME and no entry for this user: a script is still run, with a home that is not anyone's
        home = "/"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/bin", "HOME": home, "USER": user, "LOGNAME": user, "SHELL": "/usr/bin/sh",
           "LANG": "C.UTF-8", "TERM": "dumb"}
    env.update({k: os.environ[k] for k in _SESSION if k in os.environ})
    return env


def _claim(run_id: str, sha: str, script: str) -> bool:
    """Take the one right to run this step. False when an earlier attempt already took it (it may have been interrupted half way:
    what a half-run script did is unknown, and running it again could do it twice)."""
    if not _RUN_ID.match(run_id or ""):
        raise ScriptRefused("this script step has no valid identity")
    folder = paths.state_dir() / "script-runs"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(folder / run_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump({"script": script[:200], "sha256": sha}, f)
    return True


def read_in_tree(root: Path, rel: str, expect_sha256: str) -> bytes:
    """The bytes of the script `rel` inside `root`, found without following a link anywhere on the way, and only if they are the ones
    whose checksum was shown."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise ScriptRefused("that is not a path inside the program's folder")
    parts = rel.split("/")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
    except OSError as exc:
        raise ScriptRefused("the script is not there any more, or something that is not a plain folder or file is in its way") from exc
    finally:
        os.close(fd)
    try:
        st = os.fstat(leaf)
        if not stat.S_ISREG(st.st_mode):
            raise ScriptRefused("the script is not a plain file any more")
        if st.st_size > scriptrun.MAX_SCRIPT_BYTES:
            raise ScriptRefused("the script is bigger than it was when it was shown")
        with os.fdopen(leaf, "rb", closefd=False) as f:
            data = f.read(scriptrun.MAX_SCRIPT_BYTES + 1)
    finally:
        os.close(leaf)
    if scriptrun.sha256(data) != expect_sha256:
        raise ScriptChanged("the script is not the one that was shown (it has changed since), so it was not run")
    return data


def run_as_user(data: bytes, *, name: str, cwd: Path, timeout: float = scriptrun.DEFAULT_TIMEOUT) -> dict[str, Any]:
    text = scriptrun.script_text(data)
    argv = scriptrun.command_for(text, name)
    return scriptrun.run_bounded(argv, env=user_env(), cwd=str(cwd), timeout=timeout)


def claim_and_run(*, root: Path, rel: str, sha: str, run_id: str, timeout: float = scriptrun.DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Read the script from `root`, check it, take the single right to run it, run it as the person. Never raises for a script
    problem: the answer says what happened ({"script", "sha256", "as", "ran", "ok", "exit", "timed_out", "output", "error"})."""
    result: dict[str, Any] = {"script": rel, "sha256": sha, "as": "user", "ran": False, "ok": False, "exit": None, "timed_out": False,
                              "output": "", "error": ""}
    try:
        data = read_in_tree(root, rel, sha)
        scriptrun.interpreter_for(scriptrun.script_text(data))  # refused before it is claimed: an unknown interpreter is not an attempt
        if not _claim(run_id, sha, rel):
            result["error"] = ("an earlier attempt to run this script was interrupted, so Cygnus does not run it a second time "
                               "(it may have done part of its work): run it yourself if you want it run again")
            return result
        ran = run_as_user(data, name=str(root / rel), cwd=root, timeout=timeout)
    except ScriptRefused as exc:
        result["error"] = str(exc)
        return result
    except OSError as exc:
        result["error"] = f"the script could not be started ({exc.strerror or exc})"
        return result
    except Exception as exc:  # noqa: BLE001 - whatever else goes wrong, it is an answer: an exception here would undo an install that worked
        result["error"] = f"the script could not be run ({type(exc).__name__})"
        return result
    result.update(ran=True, exit=ran["exit"], timed_out=ran["timed_out"], output=ran["output"], truncated=ran["truncated"],
                  ok=ran["exit"] == 0 and not ran["timed_out"])
    if ran["timed_out"]:
        result["error"] = f"it ran for more than {int(timeout)} seconds and was stopped"
    elif ran["exit"] is not None and ran["exit"] < 0:
        result["error"] = f"it was stopped by signal {-ran['exit']}"
    elif ran["exit"] != 0:
        result["error"] = f"it ended with exit status {ran['exit']}"
    return result
