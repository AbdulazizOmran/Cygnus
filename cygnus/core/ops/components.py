"""Apply a missing component's actions (from a manifest) through the right mechanism.

Privileged actions go through cygnus-helper: the helper computes the plan, `confirm` is shown the
helper's own description, and only then is the plan committed (polkit asks for authorization).
"""

from __future__ import annotations

import subprocess
from typing import Callable

from cygnus.core import paths
from cygnus.core.errors import CygnusError
from cygnus.core.privilege import HelperClient, Plan
from cygnus.core.recovery.model import Action
from cygnus.core.util import http


class Declined(CygnusError):
    pass


def apply_actions(actions: list[Action], *, client: HelperClient | None, confirm: Callable[[str], bool],
                  progress: Callable[[str], None] = print, browsers: dict | None = None) -> list[str]:
    """Run actions in order; stops at the first failure. Returns a log of what happened."""
    log: list[str] = []
    for action in actions:
        k, p = action.kind, action.params
        if k == "browser.open_store":
            urls = [t.get("store_url") for t in (browsers or {}).values() if t.get("store_url")]
            for url in urls:
                log.append(f"open {url}")
                subprocess.Popen(["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            continue
        if client is None:
            raise CygnusError("this step needs the Cygnus helper, which is not available")
        if k == "group.add_user":
            plan = client.plan_group(p["group"], "add")
        elif k == "systemd.enable_now":
            plan = client.plan_unit(p["unit"], "enable_now")
        elif k == "pacman.install_repo":
            plan = client.plan_packages(install_repo=p["names"], asdeps=bool(p.get("asdeps")))
        elif k == "pacman.install_local":
            progress(f"Downloading {p['url']} …")
            path = http.download(p["url"], paths.cache_dir() / "downloads", expected_sha256=p["sha256"])
            log.append(f"downloaded and verified {path.name}")
            plan = client.plan_packages(local_files=[(str(path), p["sha256"])])
        else:
            raise CygnusError(f"action {k!r} is not supported yet")
        _commit(client, plan, confirm, progress, log)
    return log


def _commit(client: HelperClient, plan: Plan, confirm, progress, log: list[str]) -> None:
    if not confirm(plan.message):
        raise Declined("cancelled")
    ok, detail = client.commit(plan, on_progress=progress)
    log.append(("done: " if ok else "FAILED: ") + plan.message)
    if not ok:
        raise CygnusError(f"the operation failed: {detail[-500:]}")
