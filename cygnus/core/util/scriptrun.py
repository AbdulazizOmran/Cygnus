"""Running an install script that a person chose to run (Cygnus never runs one by default).

Used by the user side (a script run as you) and by the root helper (a script run as administrator), so both read a script the
same way. What is run is exactly the bytes whose SHA-256 was shown: the text goes to the interpreter on its command line
(`interpreter -c TEXT NAME`), so there is no file that could be swapped between the check and the run, and the script's standard
input stays free."""

from __future__ import annotations

import hashlib
import os
import select
import signal
import subprocess
import threading
import time
import unicodedata
from typing import Any, NamedTuple

from cygnus.core.errors import CygnusError

MAX_SCRIPT_BYTES = 64 * 1024  # one argument to a program may be 128 KiB: a script past this is not run
MAX_OUTPUT = 16 * 1024  # the end of what a script printed is kept for the report
DEFAULT_TIMEOUT = 300.0

# The interpreters a script's first line may name -> (program, the option that takes the script text, the one-letter options of its own
# a first line may pass). Anything else is not run: a name Cygnus does not know could be a program anywhere, and "run it yourself" is the
# honest answer for it. (dash and ash are not here: /usr/bin/sh is not necessarily either of them, so a script that asks for one would
# run under another shell than it names.)
_INTERPRETERS: dict[str, tuple[str, str, str]] = {
    "sh": ("/usr/bin/sh", "-c", "exuvfC"), "bash": ("/usr/bin/bash", "-c", "exuvfC"), "zsh": ("/usr/bin/zsh", "-c", "exuvfC"),
    "python3": ("/usr/bin/python3", "-c", "uOBEsS"), "python": ("/usr/bin/python3", "-c", "uOBEsS"),
    "perl": ("/usr/bin/perl", "-e", "wWX"),
}
_PROGRAM_DIRS = ("/bin", "/usr/bin")  # where the first line may say the interpreter is (they are the same place here)
INTERPRETER_PATHS = frozenset(path for path, _, _ in _INTERPRETERS.values())
INTERPRETER_FLAGS = frozenset(flag for _, flag, _ in _INTERPRETERS.values())
GRACE = 1.0  # after the script itself has ended, how long what it printed is still collected


class Interpreter(NamedTuple):
    program: str
    options: tuple[str, ...]  # the options the first line passes (a cluster such as -e or -ex), none for most scripts
    flag: str


class ScriptRefused(CygnusError):
    """The script is not one Cygnus will run, with the reason in words."""


class ScriptChanged(ScriptRefused):
    """The script is there but is not the one whose checksum was shown."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_problem(text: str) -> str | None:
    """Why a text cannot be shown exactly as it would run: characters that are invisible, that move other text around (direction marks),
    that look like something else (a no-break space), or that a terminal obeys (an escape). Only the newline and the tab are allowed."""
    for c in text:
        if c in "\n\t" or c == " ":
            continue
        if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Zs"):
            return ("the script contains characters that cannot be seen or that move text around (a carriage return, a zero-width or direction "
                    "mark, a no-break space, a control character), so what you would be shown is not what would run")
    return None


def script_text(data: bytes) -> str:
    """The script as text for a command line: exactly its bytes (a script that is not valid text is refused: it could not be shown as it runs)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ScriptRefused("the script is empty")
    if len(data) > MAX_SCRIPT_BYTES:
        raise ScriptRefused(f"the script is bigger than {MAX_SCRIPT_BYTES // 1024} KiB, which is more than Cygnus passes to a program")
    if b"\0" in data:
        raise ScriptRefused("the script contains a zero byte, so it is not a text script")
    try:
        text = bytes(data).decode("utf-8")  # strictly: a script with bytes that are not text cannot be shown exactly as it would run
    except UnicodeDecodeError as exc:
        raise ScriptRefused("the script is not valid text (UTF-8), so it cannot be shown to you exactly as it would run") from exc
    if text.startswith("-"):  # `sh -c -x NAME`: a first word that is an option makes the shell run the file called NAME, not this text
        raise ScriptRefused("the script starts with a dash, which a shell would read as an option")
    problem = text_problem(text)
    if problem:
        raise ScriptRefused(problem)
    return text


def runnable_problem(data: bytes) -> str | None:
    """Why Cygnus will not offer to run this script, in words, or None when it can be shown exactly and run exactly."""
    try:
        interpreter_for(script_text(data))
    except ScriptRefused as exc:
        return str(exc)
    return None


def interpreter_for(text: str) -> Interpreter:
    """The program that runs the script, as its first line says (`#!/bin/sh -e`, `#!/usr/bin/env bash`...). No first line means sh, like
    a shell reading a script without one. What the first line asks for is done as it says or not at all: the program must be in /bin or
    /usr/bin (or found by env, with no env options), and at most one option cluster of a short list may follow it; anything else is
    refused rather than run differently from what the person reads."""
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        program, flag, _ = _INTERPRETERS["sh"]
        return Interpreter(program, (), flag)
    words = first[2:].split()
    if not words:
        raise ScriptRefused("its first line (#!) names no program")
    directory, name = os.path.split(words[0])
    if directory not in _PROGRAM_DIRS:
        raise ScriptRefused(f"its first line names a program in {directory[:40] or 'the current folder'!r}, not in /bin or /usr/bin: run it yourself if you trust it")
    extra = words[1:]
    if name == "env":
        if not extra or extra[0].startswith("-") or "=" in extra[0]:
            raise ScriptRefused("its first line uses env in a way Cygnus does not follow")
        name, extra = extra[0], extra[1:]
        if extra:
            raise ScriptRefused("its first line passes options through env, which Cygnus does not follow")
    if name not in _INTERPRETERS:
        raise ScriptRefused(f"its first line names {name[:40]!r}, a program Cygnus does not run scripts with: run it yourself if you trust it")
    program, flag, letters = _INTERPRETERS[name]
    options: tuple[str, ...] = ()
    if extra:
        cluster = extra[0]
        if len(extra) > 1 or not (len(cluster) > 1 and cluster[0] == "-" and all(c in letters for c in cluster[1:])):
            raise ScriptRefused(f"its first line passes {' '.join(extra)[:40]!r} to {name}, which Cygnus does not follow: run it yourself if you trust it")
        options = (cluster,)
    return Interpreter(program, options, flag)


def command_for(text: str, name: str) -> list[str]:
    """The command line that runs the script: the interpreter, the options its first line asks for, the text, and `name` as the script's
    own name ($0)."""
    interp = interpreter_for(text)
    return [interp.program, *interp.options, interp.flag, text, name] if interp.flag == "-c" else [interp.program, *interp.options, interp.flag, text]


def printable(raw: bytes) -> str:
    """What a script printed, safe to show: no control characters or escape sequences (a script could draw over the screen)."""
    text = raw.decode("utf-8", "replace")
    return "".join(c if (c in "\n\t" or c.isprintable()) else "?" for c in text)


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


def _keep_draining(p: subprocess.Popen) -> None:
    """Something the script left running still holds the pipe: its output is read and thrown away, so that it is not stopped by a
    broken pipe, and nobody waits for it."""
    stream = p.stdout
    assert stream is not None
    fd = stream.fileno()

    def drain() -> None:
        try:
            while os.read(fd, 65536):
                pass
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    try:
        threading.Thread(target=drain, daemon=True).start()
    except RuntimeError:
        stream.close()


def _supervise(p: subprocess.Popen, timeout: float) -> dict[str, Any]:
    assert p.stdout is not None
    fd = p.stdout.fileno()
    deadline = time.monotonic() + timeout
    tail, seen = bytearray(), 0
    eof = timed_out = False
    ended_at: float | None = None
    while True:
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        if p.poll() is not None:
            ended_at = now if ended_at is None else ended_at
            if eof or now - ended_at >= GRACE:
                break
        if eof:  # everything was read: only the end of the script is waited for
            time.sleep(0.02)
            continue
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                eof = True
                continue
            seen += len(chunk)
            tail.extend(chunk)
            if len(tail) > MAX_OUTPUT:
                del tail[:len(tail) - MAX_OUTPUT]
    if timed_out:
        _kill_group(p)
        p.wait()
    if eof:
        p.stdout.close()
    else:
        _keep_draining(p)
    return {"exit": None if timed_out else p.returncode, "timed_out": timed_out, "output": printable(bytes(tail)), "truncated": seen > MAX_OUTPUT}


def run_bounded(argv: list[str], *, env: dict[str, str], cwd: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run `argv` with no input, its output kept to the last MAX_OUTPUT bytes, in a group of its own that is killed when the time is up.
    {"exit": int | None, "timed_out": bool, "output": str, "truncated": bool}. The time limit holds whatever the script leaves behind: a
    program it started and left running (even one in a session of its own) never makes this wait; the group is stopped only when the
    time is up, because a script that starts a program on purpose and ends is allowed to (its output is read and discarded)."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd,
                         start_new_session=True, close_fds=True)
    try:
        return _supervise(p, timeout)
    except BaseException:
        _kill_group(p)  # whatever went wrong here, the script is not left running unobserved
        try:
            p.wait(5)
        except Exception:  # noqa: BLE001
            pass
        raise
