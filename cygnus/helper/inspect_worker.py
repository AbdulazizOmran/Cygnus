"""Reads an untrusted Arch package file and reports what it says about itself.

This is the one place where a file chosen by a local user is opened BEFORE anybody has authorized anything, so
it must not run as root. The root helper starts it with the numeric user and group of the dedicated, otherwise
unused account `cygnus-reader`, and it then prepares itself in this order:

  1. as root, while it has read nothing untrusted: import everything it will need;
  2. still as root: leave the network, IPC and hostname namespaces; set memory, CPU, open-file, file-size and
     process limits (no process or thread can be created); set "no new privileges";
  3. give root up completely: no supplementary groups, the reader's group and user;
  4. PROVE it: root can no longer be regained and no capability is left;
  5. install a syscall filter (see seccomp.py): no new sockets, namespaces, mounts, tracing, kernel access;
  6. only now open the package, and print one line of JSON: the facts, and who it was while reading.

The helper refuses the plan unless the report says it really was that user, with nothing left. When not started
as root (a development checkout, the tests) there is nothing to give up and steps 2 to 5 are skipped.

The staged file it reads is owned by root and cannot be written by the reader, so it cannot change what is later
installed.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import sys
import tempfile
from pathlib import Path

# Run as a script under `python -I` (no current folder on the path): find the package this file belongs to.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cygnus.helper import seccomp  # noqa: E402 - needs the path above

MEMORY_LIMIT = 4 * 1024**3  # address space: a streaming decompressor with a large window needs room
CPU_SECONDS = 300
OPEN_FILES = 64
WRITE_LIMIT = 1024 * 1024  # it writes nothing but its answer; this only stops a runaway
PR_SET_NO_NEW_PRIVS = 38


def _capabilities() -> tuple[int, int]:
    """(effective, permitted) capability masks of this process."""
    masks = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapPrm"):
            masks[key] = int(value.strip(), 16)
    return masks["CapEff"], masks["CapPrm"]


def lock_down(uid: int | None = None, gid: int | None = None) -> dict:
    """Give root up (see the module docstring). Returns what the process is afterwards, for the helper to check."""
    if os.geteuid() != 0:
        return {"uid": os.getuid(), "sandboxed": False}
    if not uid or not gid:
        raise ValueError("a reader started as root must be told which unprivileged user and group to become")
    os.unshare(os.CLONE_NEWNET | os.CLONE_NEWIPC | os.CLONE_NEWUTS)  # no network: not even loopback is up
    for which, limit in ((resource.RLIMIT_AS, MEMORY_LIMIT), (resource.RLIMIT_CPU, CPU_SECONDS),
                         (resource.RLIMIT_NOFILE, OPEN_FILES), (resource.RLIMIT_CORE, 0),
                         (resource.RLIMIT_FSIZE, WRITE_LIMIT), (resource.RLIMIT_NPROC, 0)):
        resource.setrlimit(which, (limit, limit))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set no-new-privileges")
    os.chdir("/")
    os.setgroups([])
    os.setgid(gid)  # the group first: after the user is changed root could no longer change it
    os.setuid(uid)
    # The proof: nothing of root is left.
    for regain in (lambda: os.setuid(0), lambda: os.setgid(0), lambda: os.setgroups([0])):
        try:
            regain()
        except PermissionError:
            continue
        raise RuntimeError("administrator rights could be regained")
    effective, permitted = _capabilities()
    if os.getresuid() != (uid,) * 3 or os.getresgid() != (gid,) * 3 or os.getgroups() or effective or permitted:
        raise RuntimeError("the reader still has rights it should have given up")
    filtered = seccomp.install()  # last: the steps above need calls the filter would not allow once it is in place
    return {"uid": os.getuid(), "gid": os.getgid(), "groups": [], "capabilities": 0, "sandboxed": True,
            "no_new_privs": True, "no_processes": True, "seccomp": filtered}


def _emit(data: dict) -> int:
    sys.stdout.write(json.dumps(data))
    sys.stdout.flush()
    return 0 if data.get("ok") else 1


def main(argv: list[str]) -> int:
    try:
        path = argv[1]
        uid, gid = (int(argv[2]), int(argv[3])) if len(argv) == 4 else (None, None)
        if len(argv) not in (2, 4):
            raise ValueError
    except (IndexError, ValueError):
        return _emit({"ok": False, "error": "usage: inspect_worker PACKAGE [UID GID]"})
    # 1. Everything this program needs is loaded while it is still root and has read nothing untrusted.
    try:
        import pyalpm

        from cygnus.core.backends import alpm_worker
        from cygnus.core.detect import detect_file
        from cygnus.core.models import PackageFormat
    except Exception as exc:  # noqa: BLE001 - reported, never guessed around
        return _emit({"ok": False, "error": f"cannot start: {exc}"})
    # 2-5. Become the reader account, prove it, and install the filter. A failure here means the file is NOT opened.
    try:
        identity = lock_down(uid, gid)
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "sandbox_error": f"{type(exc).__name__}: {exc}"})
    # 6. Now the untrusted file.
    try:
        cand = detect_file(path)
        if cand.format is not PackageFormat.LOCAL_PKG:
            return _emit({"ok": False, "refused": "only Arch packages (.pkg.tar.*) can be installed from a file",
                          "identity": identity})
        with tempfile.TemporaryDirectory(prefix="cygnus-inspect-") as scratch:  # made after the lockdown: owned by the reader
            os.mkdir(os.path.join(scratch, "local"))
            os.mkdir(os.path.join(scratch, "sync"))
            handle = pyalpm.Handle("/", scratch)
            handle.logfile = "/dev/null"
            [facts] = alpm_worker.op_load(handle, {"paths": [path]})["packages"]
    except Exception as exc:  # noqa: BLE001 - a damaged or hostile file is a refusal, not a crash
        return _emit({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}", "identity": identity})
    return _emit({"ok": True, "facts": facts, "identity": identity})


if __name__ == "__main__":
    sys.exit(main(sys.argv))
