"""Deterministic, unprivileged probes. Each returns a ProbeOutcome with evidence; none modify anything."""

from __future__ import annotations

import fnmatch
import grp
import ipaddress
import json
import os
import pwd
import re
import socket
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator

from cygnus.core.util import proc


class ProbeStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(slots=True, kw_only=True)
class ProbeOutcome:
    probe: str
    status: ProbeStatus
    evidence: str
    facts: dict[str, Any] = field(default_factory=dict)


def _ok(probe: str, evidence: str, **facts: Any) -> ProbeOutcome:
    return ProbeOutcome(probe=probe, status=ProbeStatus.PASS, evidence=evidence, facts=facts)


def _fail(probe: str, evidence: str, **facts: Any) -> ProbeOutcome:
    return ProbeOutcome(probe=probe, status=ProbeStatus.FAIL, evidence=evidence, facts=facts)


def _unknown(probe: str, evidence: str, **facts: Any) -> ProbeOutcome:
    return ProbeOutcome(probe=probe, status=ProbeStatus.UNKNOWN, evidence=evidence, facts=facts)


# -- identity / permissions -------------------------------------------------------------------------
def group_membership(group: str, effective: str = "session", *, user: str | None = None,
                     session_gids: list[int] | None = None) -> ProbeOutcome:
    name = "group_membership"
    try:
        gr = grp.getgrnam(group)
    except KeyError:
        return _fail(name, f"the group '{group}' does not exist on this system", group=group, configured=False)
    user = user or pwd.getpwuid(os.getuid()).pw_name
    try:
        primary = pwd.getpwnam(user).pw_gid
    except KeyError:
        primary = None
    configured = user in gr.gr_mem or primary == gr.gr_gid
    session = gr.gr_gid in (session_gids if session_gids is not None else os.getgroups())
    facts = {"group": group, "gid": gr.gr_gid, "configured": configured, "session": session}
    if effective == "configured":
        return (_ok if configured else _fail)(name, f"{user} {'is' if configured else 'is not'} a member of "
                                                    f"'{group}'", **facts)
    if session:
        return _ok(name, f"your session has the '{group}' group", **facts)
    if configured:
        return _fail(name, f"{user} was added to '{group}', but this session started before that: log out "
                           "and back in", relogin_required=True, **facts)
    return _fail(name, f"{user} is not a member of '{group}'", **facts)


_PROBE_ROOTS = ("/dev/", "/sys/", "/proc/")
# Entries that lead out of /dev, /sys and /proc into the rest of the filesystem (or another process's).
_PROC_ESCAPES = {"root", "cwd", "exe", "fd", "fdinfo", "map_files", "task", "ns", "attr"}
_DEV_ESCAPES = {"fd", "stdin", "stdout", "stderr"}
MAX_READABLE_MATCHES = 4096


def _confined(pattern: str) -> bool:
    if ".." in pattern.split("/") or not os.path.normpath(pattern).startswith(_PROBE_ROOTS):
        return False
    # Judge the path as the system will read it: "/dev/./fd/0" and "/dev//stdin" are "/dev/fd/0" and "/dev/stdin".
    parts = os.path.normpath(pattern).split("/")
    # In /proc only the process id may be a wildcard: "/proc/self/r*" would otherwise reach "root".
    if parts[1] == "proc" and len(parts) > 3 and (set(parts[3:]) & _PROC_ESCAPES
                                                  or any(c in "/".join(parts[3:]) for c in "*?[")):
        return False
    # In /dev nothing that leads out of it may be named, nor matched by a wildcard ("/dev/f*", "/dev/*/0").
    if parts[1] == "dev" and len(parts) > 2 and any(fnmatch.fnmatchcase(esc, parts[2]) for esc in _DEV_ESCAPES):
        return False
    return True


MAX_GLOB_ENTRIES = 20_000  # directory entries looked at in total while expanding one pattern


class _TooManyEntries(Exception):
    pass


def _bounded_glob(pattern: str, budget: list[int]) -> Iterator[str]:
    """Expand a glob pattern like glob.glob does (hidden names are not matched by a leading wildcard) but look at
    a bounded number of directory entries, so a pattern with many wildcards cannot keep a probe busy."""
    def walk(parts: list[str], base: str) -> Iterator[str]:
        if not parts:
            yield base
            return
        seg, rest = parts[0], parts[1:]
        if not any(c in seg for c in "*?["):
            nxt = os.path.join(base, seg)
            if os.path.lexists(nxt):
                yield from walk(rest, nxt)
            return
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return
        budget[0] -= len(names)
        if budget[0] < 0:
            raise _TooManyEntries
        for name in names:
            if fnmatch.fnmatchcase(name, seg) and not (name.startswith(".") and not seg.startswith(".")):
                yield from walk(rest, os.path.join(base, name))

    yield from walk([s for s in os.path.normpath(pattern).split("/") if s], "/")


def readable(path: str) -> ProbeOutcome:
    """Every match is readable by you (and there is at least one)."""
    name = "readable"
    if not _confined(path):
        return _unknown(name, "refusing to probe paths outside /dev, /sys and /proc")
    matches = []
    try:
        for m in _bounded_glob(path, [MAX_GLOB_ENTRIES]):
            if not os.path.realpath(m).startswith(_PROBE_ROOTS):
                continue  # a link that leads elsewhere is never looked at
            matches.append(m)
            if len(matches) > MAX_READABLE_MATCHES:
                return _unknown(name, f"{path} matches implausibly many files")
    except _TooManyEntries:
        return _unknown(name, f"{path} would take too long to look through")
    matches.sort()
    if not matches:
        return _unknown(name, f"nothing matches {path}")
    ok = [m for m in matches if os.access(m, os.R_OK)]
    if len(ok) == len(matches):
        return _ok(name, f"all {len(matches)} of {path} are readable", count=len(matches))
    sample = matches[0]
    try:
        st = os.stat(sample)
        owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name} {oct(st.st_mode & 0o777)}"
    except (OSError, KeyError):
        owner = "?"
    return _fail(name, f"{len(matches) - len(ok)} of {len(matches)} {path} are not readable by you "
                       f"(e.g. {sample}: {owner})", readable=len(ok), total=len(matches))


# -- systemd ---------------------------------------------------------------------------------------
def systemd_unit(unit: str, scope: str = "system", expect: list[str] | None = None) -> ProbeOutcome:
    name = "systemd_unit"
    expect = expect or ["enabled", "active"]
    argv = ["systemctl"] + (["--user"] if scope == "user" else []) + [
        "show", "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "UnitFileState", "--", unit]
    res = proc.run(argv, timeout=15)
    if not res.ok:
        return _unknown(name, f"systemctl failed: {res.stderr.strip()[:200]}")
    props = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
    facts = {"unit": unit, **props}
    if props.get("LoadState") == "not-found":
        return _fail(name, f"{unit} is not installed", installed=False, **facts)
    problems = []
    # "static" units have no [Install] section and "alias" is only another name: neither is enabled.
    if "enabled" in expect and props.get("UnitFileState") not in ("enabled", "enabled-runtime"):
        problems.append(f"not enabled ({props.get('UnitFileState')})")
    if "active" in expect and props.get("ActiveState") != "active":
        problems.append(f"not running ({props.get('ActiveState')}/{props.get('SubState')})")
    if problems:
        return _fail(name, f"{unit}: " + ", ".join(problems), installed=True, **facts)
    return _ok(name, f"{unit} is {props.get('UnitFileState')} and {props.get('ActiveState')}", installed=True, **facts)


def journal_recent_match(unit: str, contains: list[str], within_minutes: int = 30) -> ProbeOutcome:
    """Plain substring matching only (manifests never supply regular expressions)."""
    name = "journal_recent_match"
    res = proc.run(["journalctl", "--since", f"-{within_minutes}min", "-o", "cat", "--no-pager", "-u", unit],
                   timeout=30, max_output=8 * 1024 * 1024)
    text = res.stderr.lower()
    if "insufficient permissions" in text or "no journal files were opened" in text or "not seeing messages" in text:
        return _unknown(name, "no permission to read the system journal")
    if res.returncode != 0:
        return _unknown(name, f"cannot read the journal: {res.stderr.strip()[:200]}")
    hits = [line for line in res.stdout.splitlines() if any(c in line for c in contains)]
    if hits:
        return _ok(name, f"recent log: {hits[-1][:160]}", matches=len(hits))
    return _fail(name, f"no recent log lines from {unit} in the last {within_minutes} minutes mention "
                       + " or ".join(repr(c) for c in contains))


# -- network sockets (from /proc/net, no privileges) -------------------------------------------------
_TCP_STATES = {"01": "ESTABLISHED", "0A": "LISTEN"}


def _parse_addr(hexaddr: str) -> tuple[str, int]:
    ip_hex, port_hex = hexaddr.split(":")
    port = int(port_hex, 16)
    raw = bytes.fromhex(ip_hex)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1]), port
    # IPv6: four little-endian 32-bit words
    words = struct.unpack("<4I", raw)
    return socket.inet_ntop(socket.AF_INET6, struct.pack(">4I", *words)), port


def tcp_sockets(proc_root: str = "/proc") -> list[dict[str, Any]]:
    out = []
    for fname in ("net/tcp", "net/tcp6"):
        try:
            lines = Path(proc_root, fname).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            state = _TCP_STATES.get(parts[3])
            if state is None:
                continue
            (laddr, lport), (raddr, rport) = _parse_addr(parts[1]), _parse_addr(parts[2])
            out.append({"state": state, "laddr": laddr, "lport": lport, "raddr": raddr, "rport": rport,
                        "inode": int(parts[9])})
    return out


def _socket_owners(inodes: set[int]) -> dict[int, dict[str, Any]]:
    """Map socket inodes to the owning process — only processes you own are visible."""
    found: dict[int, dict[str, Any]] = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                ino = int(target[8:-1])
                if ino in inodes and ino not in found:
                    try:
                        exe = os.readlink(f"/proc/{pid}/exe")
                    except OSError:
                        exe = None
                    found[ino] = {"pid": int(pid), "exe": exe}
    return found


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address; an IPv4 address that /proc/net/tcp6 shows as ::ffff:a.b.c.d is just IPv4."""
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    return addr.ipv4_mapped if addr.version == 6 and addr.ipv4_mapped else addr


def _addr_matches(sock_addr: str, wanted: str) -> bool:
    """The same concrete address (used for established connections)."""
    have, want = _address(sock_addr), _address(wanted)
    return have is not None and have == want


def _listener_serves(bound: str, wanted: str) -> bool:
    """Would a connection to `wanted` reach a socket bound to `bound`?

    0.0.0.0 serves every IPv4 address, :: serves every address (Linux sockets are dual-stack by
    default), and a loopback-only socket serves nothing else. As a *requested* address, 0.0.0.0 and ::
    mean "reachable from all interfaces", so a loopback-only listener does not satisfy them.
    """
    have, want = _address(bound), _address(wanted)
    if have is None or want is None:
        return False
    if have.is_unspecified:
        return have.version == 6 or want.version == 4
    return have == want


def tcp_listener(port: int, addr: str = "127.0.0.1", owner_exe_contains: str | None = None,
                 sockets: list[dict[str, Any]] | None = None) -> ProbeOutcome:
    name = "tcp_listener"
    sockets = sockets if sockets is not None else tcp_sockets()
    listening = [s for s in sockets if s["state"] == "LISTEN" and s["lport"] == port
                 and _listener_serves(s["laddr"], addr)]
    if not listening:
        return _fail(name, f"nothing is listening on {addr}:{port}", port=port)
    if owner_exe_contains:
        owners = _socket_owners({s["inode"] for s in listening})
        exes = [o["exe"] or "" for o in owners.values()]
        if not exes:
            return _unknown(name, f"something listens on {addr}:{port}, owned by another user", port=port)
        if not any(owner_exe_contains in e for e in exes):
            return _fail(name, f"{addr}:{port} is held by an unexpected program: {', '.join(exes)}", port=port,
                         owners=exes)
        return _ok(name, f"{addr}:{port} is held by {exes[0]}", port=port, owners=exes)
    return _ok(name, f"something is listening on {addr}:{port}", port=port)


def tcp_peer(port: int, addr: str = "127.0.0.1", sockets: list[dict[str, Any]] | None = None) -> ProbeOutcome:
    name = "tcp_peer"
    sockets = sockets if sockets is not None else tcp_sockets()
    conns = [s for s in sockets if s["state"] == "ESTABLISHED" and (
        (s["lport"] == port and _addr_matches(s["laddr"], addr)) or (s["rport"] == port and _addr_matches(s["raddr"], addr)))]
    if conns:
        return _ok(name, f"{len(conns)} connection(s) on {addr}:{port}", connections=len(conns))
    return _fail(name, f"no connection on {addr}:{port}", port=port)


# -- packages, kernel, processes ---------------------------------------------------------------------
def pacman_installed(name_: str) -> ProbeOutcome:
    """Exact package name only: `pacman -Q` would also accept a package that merely *provides* it."""
    name = "pacman_installed"
    res = proc.run(["pacman", "-Q", "--", name_], timeout=15)
    parts = res.stdout.split()
    if res.ok and len(parts) == 2 and parts[0] == name_:
        return _ok(name, f"{name_} {parts[1]} is installed", version=parts[1])
    if res.ok and parts:
        return _fail(name, f"{name_} is not installed ({parts[0]} provides it)", provider=parts[0])
    return _fail(name, f"the package {name_} is not installed")


def sysctl(key: str, expect: str | None = None, minimum: int | None = None, absent_ok: bool = False) -> ProbeOutcome:
    name = "sysctl"
    if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", key):
        return _unknown(name, "invalid sysctl key")
    path = Path("/proc/sys", *key.split("."))
    try:
        value = path.read_text().strip()
    except OSError:
        if absent_ok:  # a setting only some kernels have: a kernel without it restricts nothing
            return _ok(name, f"{key} does not exist on this kernel, so nothing restricts it")
        return _unknown(name, f"{key} does not exist on this kernel")
    if minimum is not None:
        try:
            ok = int(value) >= minimum
        except ValueError:
            return _unknown(name, f"{key} = {value} is not a number")
        return (_ok if ok else _fail)(name, f"{key} = {value}" + ("" if ok else f" (needs at least {minimum})"),
                                      value=value)
    if value == expect:
        return _ok(name, f"{key} = {value}")
    return _fail(name, f"{key} = {value} (needs {expect})", value=value)


def process_running(exe_contains: str) -> ProbeOutcome:
    name = "process_running"
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if exe_contains in exe:
            return _ok(name, f"running as PID {pid} ({exe})", pid=int(pid), exe=exe)
    return _fail(name, f"no running program path contains {exe_contains!r}")


# -- browsers ---------------------------------------------------------------------------------------
CHROMIUM_DIRS = [
    "google-chrome", "google-chrome-beta", "chromium", "BraveSoftware/Brave-Browser", "microsoft-edge", "vivaldi",
    "opera", "net.imput.helium", "thorium", "ungoogled-chromium",
]
CHROMIUM_FLATPAKS = {
    "com.google.Chrome": "google-chrome", "org.chromium.Chromium": "chromium", "com.brave.Browser":
    "BraveSoftware/Brave-Browser", "com.microsoft.Edge": "microsoft-edge", "com.vivaldi.Vivaldi": "vivaldi",
}


def _chromium_profiles(home: Path) -> list[tuple[str, Path]]:
    roots = [(d, home / ".config" / d) for d in CHROMIUM_DIRS]
    roots += [(f"{app} (Flatpak)", home / ".var/app" / app / "config" / d) for app, d in CHROMIUM_FLATPAKS.items()]
    out = []
    for label, root in roots:
        if not root.is_dir():
            continue
        for prof in root.iterdir():
            if (prof / "Preferences").is_file():
                out.append((f"{label}/{prof.name}", prof))
    return out


def _firefox_profiles(home: Path) -> list[tuple[str, Path]]:
    out = []
    flatpak_ff = home / ".var/app/org.mozilla.firefox"
    for label, base in (("firefox", home / ".config/mozilla/firefox"),  # XDG layout used by current Firefox releases
                        ("firefox", home / ".mozilla/firefox"),  # legacy layout
                        ("firefox (Flatpak)", flatpak_ff / "config/mozilla/firefox"),
                        ("firefox (Flatpak)", flatpak_ff / ".mozilla/firefox"),
                        ("librewolf", home / ".librewolf"), ("zen", home / ".zen")):
        if base.is_dir():
            out += [(f"{label}/{p.name}", p) for p in base.iterdir() if (p / "prefs.js").is_file()]
    return out


def browser_extension_present(family: str, ext_id: str, home: Path | None = None) -> ProbeOutcome:
    name = "browser_extension_present"
    home = home or Path.home()
    shape = r"[a-p]{32}" if family == "chromium" else r"([A-Za-z0-9._+-]+@[A-Za-z0-9.-]+|\{[0-9a-fA-F-]{36}\})"
    if not re.fullmatch(shape, ext_id):
        return _unknown(name, "invalid extension id")
    unreadable: list[str] = []
    if family == "chromium":
        profiles = _chromium_profiles(home)
        hits = [label for label, p in profiles
                if (p / "Extensions" / ext_id).is_dir() and _chromium_enabled(p, ext_id)]
    else:
        profiles = _firefox_profiles(home)
        hits = []
        for label, p in profiles:
            try:
                with open(p / "extensions.json", "rb") as fh:
                    data = json.loads(fh.read(8_000_001))   # a longer file is cut off and fails to parse
            except FileNotFoundError:
                continue  # a profile without add-ons has no such file
            except (OSError, ValueError):
                unreadable.append(label)  # cannot tell: this must not become a definite "not installed"
                continue
            addons = data.get("addons") if isinstance(data, dict) else None
            if not isinstance(addons, list):
                unreadable.append(label)
                continue
            if any(isinstance(a, dict) and a.get("id") == ext_id and a.get("active", True) for a in addons):
                hits.append(label)
    if hits:
        return _ok(name, f"installed in {', '.join(hits)}", profiles=hits)
    if not profiles:
        return _unknown(name, f"no {family}-based browser profiles found")
    if unreadable:
        return _unknown(name, f"cannot read the add-on list of {', '.join(unreadable)}")
    return _fail(name, f"not installed (or switched off) in any of {len(profiles)} {family} profile(s)",
                 profiles=[label for label, _ in profiles])


def _chromium_enabled(profile: Path, ext_id: str) -> bool:
    """An installed extension the user switched off is not present for the application's purposes. When the
    profile's settings cannot be read, it counts as present (its files are there)."""
    try:
        with open(profile / "Preferences", "rb") as fh:
            prefs = json.loads(fh.read(8_000_001))
        entry = prefs["extensions"]["settings"][ext_id]
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return not (isinstance(entry, dict) and entry.get("state") == 0)


REGISTRY: dict[str, Callable[..., ProbeOutcome]] = {
    "group_membership": group_membership,
    "readable": readable,
    "systemd_unit": systemd_unit,
    "journal_recent_match": journal_recent_match,
    "tcp_listener": tcp_listener,
    "tcp_peer": tcp_peer,
    "pacman_installed": lambda name: pacman_installed(name),  # noqa: E731
    "browser_extension_present": lambda family, id: browser_extension_present(family, id),
    "sysctl": sysctl,
    "process_running": process_running,
}


def run_probe(spec: dict[str, Any]) -> ProbeOutcome:
    params = dict(spec)
    kind = params.pop("probe")
    fn = REGISTRY.get(kind)
    if fn is None:
        return _unknown(kind, f"unknown probe type {kind!r}")
    try:
        return fn(**params)
    except Exception as exc:  # noqa: BLE001 - a broken probe must not break the health report
        return _unknown(kind, f"probe failed: {exc}")
