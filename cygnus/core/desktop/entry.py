"""Reader/writer for freedesktop Desktop Entry files (Desktop Entry Specification 1.5).

Preserves group and key order so that files Cygnus rewrites stay recognisable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_GROUP = re.compile(r"^\[(?P<name>[^\[\]]+)\]\s*$")
_KEY = re.compile(r"^(?P<key>[A-Za-z0-9-]+)(?:\[(?P<locale>[^\]]+)\])?\s*=\s?(?P<value>.*)$")
MAIN_GROUP = "Desktop Entry"


@dataclass(slots=True)
class DesktopEntry:
    # group -> ordered list of (raw key incl. locale, value)
    groups: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def get(self, key: str, group: str = MAIN_GROUP, default: str | None = None) -> str | None:
        for k, v in self.groups.get(group, []):
            if k == key:
                return unescape(v)
        return default

    def get_list(self, key: str, group: str = MAIN_GROUP) -> list[str]:
        value = self.get(key, group)
        return split_list(value) if value else []

    def set(self, key: str, value: str, group: str = MAIN_GROUP) -> None:
        """Set an unescaped value; it is escaped so it can never inject extra lines or keys."""
        self.set_raw(key, escape(value), group)

    def set_raw(self, key: str, value: str, group: str = MAIN_GROUP) -> None:
        """Set an already-escaped value (control characters are still refused)."""
        if any(c in value for c in "\n\r\0") or not _KEY.match(f"{key}=x"):
            raise ValueError("desktop entry keys/values must not contain line breaks")
        items = self.groups.setdefault(group, [])
        for i, (k, _) in enumerate(items):
            if k == key:
                items[i] = (key, value)
                return
        items.append((key, value))

    def remove(self, key: str, group: str = MAIN_GROUP) -> None:
        if group in self.groups:
            self.groups[group] = [(k, v) for k, v in self.groups[group] if k != key]

    @property
    def actions(self) -> list[str]:
        return self.get_list("Actions")

    def serialize(self) -> str:
        out: list[str] = []
        for group, items in self.groups.items():
            if out:
                out.append("")
            out.append(f"[{group}]")
            out.extend(f"{k}={v}" for k, v in items)
        return "\n".join(out) + "\n"


def parse(text: str) -> DesktopEntry:
    entry = DesktopEntry()
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _GROUP.match(line)
        if m:
            current = m.group("name")
            entry.groups.setdefault(current, [])
            continue
        if current is None:
            continue
        m = _KEY.match(line)
        if not m:
            continue
        key = m.group("key") + (f"[{m.group('locale')}]" if m.group("locale") else "")
        entry.groups[current].append((key, m.group("value")))
    return entry


def escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
            .replace("\r", "\\r"))


def unescape(value: str) -> str:
    out, i = [], 0
    mapping = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] in mapping:
            out.append(mapping[value[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_list(value: str) -> list[str]:
    """Split a ';'-separated list honouring '\\;' escapes."""
    items, cur, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] == ";":
            cur.append(";")
            i += 2
            continue
        if ch == ";":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        items.append("".join(cur))
    return [x for x in items if x]


# -- Exec lines (Desktop Entry Specification, "The Exec key") ------------------------------------------
_FIELD_CODE = re.compile(r"%[fFuUdDnNickvm%]")
_SAFE_ARG = re.compile(r"[A-Za-z0-9%_.,=:/@+-]+")


def split_exec(value: str) -> list[str]:
    """Split an (unescaped) Exec value into arguments: double quotes with backslash escapes, as the
    specification says, and single quotes leniently, as KDE and GLib accept them."""
    args, cur, i, in_arg = [], [], 0, False
    while i < len(value):
        ch = value[i]
        if ch in " \t\n":
            if in_arg:
                args.append("".join(cur))
                cur, in_arg = [], False
            i += 1
            continue
        in_arg = True
        if ch == '"':
            i += 1
            while i < len(value) and value[i] != '"':
                if value[i] == "\\" and i + 1 < len(value):
                    i += 1
                cur.append(value[i])
                i += 1
            if i >= len(value):
                raise ValueError("unterminated double quote")
            i += 1
        elif ch == "'":
            end = value.find("'", i + 1)
            if end < 0:
                raise ValueError("unterminated single quote")
            cur.append(value[i + 1:end])
            i = end + 1
        elif ch == "\\" and i + 1 < len(value):
            cur.append(value[i + 1])
            i += 2
        else:
            cur.append(ch)
            i += 1
    if in_arg:
        args.append("".join(cur))
    return args


def quote_exec_arg(arg: str) -> str:
    """Quote one argument for an Exec value (field codes and plain words stay as they are)."""
    if _FIELD_CODE.fullmatch(arg) or _SAFE_ARG.fullmatch(arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def exec_program(path: str) -> str:
    """A program path as the first word of an Exec value: "%" is literal, so it is doubled."""
    return quote_exec_arg(path.replace("%", "%%"))

