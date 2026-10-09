"""Safe subprocess execution.

Every external command goes through `run()`: argv lists only (never a shell),
a sanitized environment with a fixed C locale so output can be parsed, a
timeout, and output capture that is bounded *while reading* (the child is
killed once the cap is reached, so hostile inputs cannot balloon memory).
"""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass

from cygnus.core.errors import CygnusError, ToolMissingError

# Variables passed through from the caller's environment. Everything else is
# dropped so that e.g. LD_PRELOAD or PYTHONPATH cannot influence tools.
_PASSTHROUGH = ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
MAX_OUTPUT = 16 * 1024 * 1024


class CommandTimeout(CygnusError):
    pass


@dataclass(frozen=True, slots=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    raw_stdout: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.truncated


def which(tool: str) -> str | None:
    return shutil.which(tool, path=_SAFE_PATH)


def require(tool: str, package: str | None = None) -> str:
    path = which(tool)
    if path is None:
        raise ToolMissingError(tool, package)
    return path


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update({"PATH": _SAFE_PATH, "LC_ALL": "C", "LANG": "C"})
    if extra:
        env.update(extra)
    return env


def run(
    argv: list[str],
    *,
    timeout: float = 60.0,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    input_bytes: bytes | None = None,
    max_output: int = MAX_OUTPUT,
) -> Result:
    """Run argv without a shell. Output beyond `max_output` bytes kills the child (truncated=True)."""
    if not argv or not isinstance(argv, list):
        raise ValueError("argv must be a non-empty list")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env(env), cwd=cwd)
    if input_bytes is not None:
        try:
            proc.stdin.write(input_bytes)  # type: ignore[union-attr]
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()  # type: ignore[union-attr]
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    sel = selectors.DefaultSelector()
    for stream in buffers:
        sel.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    truncated = False
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise CommandTimeout(f"{argv[0]} did not finish within {timeout:.0f}s")
            for key, _ in sel.select(timeout=min(remaining, 1.0)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                buf = buffers[key.fileobj]
                buf += chunk
                if sum(len(b) for b in buffers.values()) > max_output:
                    truncated = True
                    proc.kill()
                    for s in list(sel.get_map().values()):
                        sel.unregister(s.fileobj)
                    break
        returncode = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise CommandTimeout(f"{argv[0]} did not finish within {timeout:.0f}s") from exc
    finally:
        sel.close()
        for stream in buffers:
            stream.close()
    return Result(argv=tuple(argv), returncode=returncode,
                  stdout=bytes(buffers[proc.stdout][:max_output]).decode("utf-8", "replace"),
                  stderr=bytes(buffers[proc.stderr][:max_output]).decode("utf-8", "replace"),
                  truncated=truncated, raw_stdout=bytes(buffers[proc.stdout][:max_output]))
