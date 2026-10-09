"""Background check: updates and problems → desktop notifications (run by a systemd user timer).

You are told about each thing once: an update to a version, an application that stopped working, an
operation that did not finish. A problem that goes away and comes back is reported again; system
updates are mentioned at most once a day. Nothing is installed or changed here.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from cygnus import APP_ID, APP_NAME
from cygnus.core import paths
from cygnus.core.util import proc
from cygnus.core.util.fs import atomic_write

SYSTEM_REPEAT_SECONDS = 24 * 3600
PROBLEMS = ("broken", "missing_component")


@dataclass(slots=True, frozen=True)
class Notice:
    key: str
    title: str
    body: str
    icon: str = APP_ID
    urgency: str = "normal"
    covers: tuple[str, ...] = ()  # the keys this notice stands for (a summary covers several); default: its own


def _state_path():
    return paths.state_dir() / "notified.json"


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_state_path(), json.dumps(state, indent=1, sort_keys=True).encode())


def collect(*, app_updates: list[dict[str, Any]], health: list[dict[str, Any]],
            interrupted: list[dict[str, Any]]) -> list[Notice]:
    """Notices for the current situation (pure; deduplication happens in `select`)."""
    notices = []
    for u in app_updates:
        if u["status"] == "update-available":
            facts = u.get("facts") or {}
            # What identifies this update across checks (never the time of the check, or it repeats every run).
            what = u.get("available") or facts.get("expected_sha1") or facts.get("commit") or "pending"
            notices.append(Notice(key=f"update:{u['installation_id']}:{what}", title="Update available",
                                  body=f"{u['name']} {u.get('available') or ''}".strip() + " — open Cygnus to update."))
    for app in health:
        for inst in app.get("installs", []):
            if inst["overall"] in PROBLEMS:
                failing = sorted(f["name"] for f in inst["features"] if f["status"] in PROBLEMS)
                notices.append(Notice(
                    key=f"health:{app['app_id']}:{inst['where']}:{inst['overall']}:{','.join(failing)}",
                    title=f"{app['name']}: something stopped working",
                    body=(", ".join(failing) or "The application") + " — open Cygnus for details and a fix.",
                    icon="dialog-warning", urgency="normal"))
    for op in interrupted:
        notices.append(Notice(key=f"op:{op['id']}", title=f"{op['title']} did not finish",
                              body="Open Cygnus to finish it or undo it.", icon="dialog-warning"))
    return notices


def select(notices: list[Notice], system: dict[str, Any] | None, state: dict[str, Any],
           now: float | None = None) -> tuple[list[Notice], dict[str, Any]]:
    """What to show now, and the new state. Keys that no longer apply are forgotten, so a problem
    that comes back is reported again."""
    now = time.time() if now is None else now
    seen = set(state.get("keys", []))
    current = {n.key for n in notices}
    show = [n for n in notices if n.key not in seen]
    new_state: dict[str, Any] = {"keys": sorted(current), "system": state.get("system")}
    updates = [n for n in show if n.key.startswith("update:")]
    if len(updates) > 1:  # one notification for several application updates
        show = [n for n in show if not n.key.startswith("update:")] + [Notice(
            key="updates", title=f"{len(updates)} application updates available",
            body="; ".join(n.body.split(" — ")[0] for n in updates) + " — open Cygnus to update.",
            covers=tuple(n.key for n in updates))]
    if system and not system.get("error"):
        count, prev = len(system.get("packages", [])), state.get("system") or {}
        if count and count != prev.get("count") and now - prev.get("at", 0) >= SYSTEM_REPEAT_SECONDS:
            show.append(Notice(key="system", title=f"{count} system updates available",
                               body=("Including a new kernel. " if system.get("kernel") else "")
                                    + "Open Cygnus → Updates to update the whole system."))
            new_state["system"] = {"count": count, "at": now}
        elif not count:
            new_state["system"] = None
    return show, new_state


def _escape(text: str) -> str:
    """Notification bodies are markup on Plasma; names from application metadata must stay text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(notice: Notice) -> None:
    """Show it, or raise: a notice that could not be shown must not be remembered as shown."""
    notify = proc.which("notify-send")
    if notify is None:
        print(f"{notice.title}: {notice.body}")
        return
    res = proc.run([notify, f"--app-name={APP_NAME}", f"--icon={notice.icon}", f"--urgency={notice.urgency}",
                    f"--hint=string:desktop-entry:{APP_ID}", "--", _escape(notice.title), _escape(notice.body)],
                   timeout=30)
    if res is not None and res.returncode != 0:  # e.g. the notification service is not running yet
        raise RuntimeError(f"notify-send failed: {(res.stderr or '').strip()[:200] or res.returncode}")


def settle(show: list[Notice], shown: list[Notice], state: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """The state to keep: what select() computed, minus everything that was meant to be shown but was not,
    so it is tried again next time."""
    failed = [n for n in show if n not in shown]
    keys = set(state.get("keys", []))
    for n in failed:
        keys.difference_update(n.covers or (n.key,))
    settled = {**state, "keys": sorted(keys)}
    if any(n.key == "system" for n in failed):
        settled["system"] = before.get("system")  # not mentioned yet: mention it again
    return settled


def run(*, gather: Callable[[], dict[str, Any]], notify: Callable[[Notice], None] | None = None) -> list[Notice]:
    data = gather()
    notices = collect(app_updates=data.get("app_updates", []), health=data.get("health", []),
                      interrupted=data.get("interrupted", []))
    before = load_state()
    show, state = select(notices, data.get("system"), before)
    shown = []
    try:
        for n in show:
            try:
                (notify or send)(n)
                shown.append(n)
            except Exception as exc:  # noqa: BLE001 - one notification failing must not stop the others
                print(f"could not show '{n.title}': {exc}", file=sys.stderr)
    finally:
        # always: otherwise everything already shown would be shown again next time; what failed is not kept
        save_state(settle(show, shown, state, before))
    return shown


# -- the systemd user timer ---------------------------------------------------------------------------------
TIMER = "cygnus-watch.timer"


def timer_enabled() -> bool:
    res = proc.run(["systemctl", "--user", "is-enabled", TIMER], timeout=30)
    return res.returncode == 0 and res.stdout.strip() == "enabled"


def set_timer(enabled: bool) -> None:
    from cygnus.core.errors import CygnusError

    res = proc.run(["systemctl", "--user", "enable" if enabled else "disable", "--now", TIMER], timeout=60)
    if res.returncode != 0:
        raise CygnusError(f"could not {'enable' if enabled else 'disable'} background checks: "
                          + (res.stderr.strip().splitlines() or ["?"])[-1])
