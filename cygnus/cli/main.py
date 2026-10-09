"""`cygnus` command-line interface (Phase 1: storage, detection, environment)."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cygnus import APP_ID, __version__
from cygnus.core import preferences
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.registry import open_registry
from cygnus.core.storage import locations
from cygnus.core.storage.discovery import scan
from cygnus.core.storage.probe import probe
from cygnus.core.util import proc


def _gib(n: int | None) -> str:
    return "?" if n is None else f"{n / 2**30:.1f} GiB"


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def _emit(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=_jsonable, sort_keys=True))


# -- storage --------------------------------------------------------------------------------------
def cmd_storage_scan(args: argparse.Namespace) -> int:
    cands = scan()
    if args.json:
        _emit([asdict(c) for c in cands])
        return 0
    for c in cands:
        status = "eligible" if c.eligible else f"not eligible: {c.ineligible_reason}"
        print(f"{c.display_name}")
        print(f"  {c.canonical_mount}  ·  {c.fs_type}  ·  UUID {c.fs_uuid}  ·  {_gib(c.free_bytes)} free of "
              f"{_gib(c.size_bytes)}  ·  class {c.location_class}  ·  {status}")
        for note in c.notes:
            print(f"  note: {note}")
    return 0


def cmd_storage_probe(args: argparse.Namespace) -> int:
    path = locations.safe_realpath(args.path)  # never steps into an absent automount
    result = probe(Path(path), ostree=not args.no_ostree)
    if args.json:
        _emit(result.to_dict())
        return 0 if result.writable and result.cleaned_up else 1  # same rule as the text output
    if not result.writable:
        print(f"cannot write to {result.path}: {result.errors.get('mkdir', 'unknown error')}")
        return 1
    print(f"{result.path} ({result.fs_type}; options {','.join(result.mount_options)})")
    for cap, ok in result.caps.items():
        mark = {True: "yes", False: "NO", None: "?"}[ok]
        err = f"  ({result.errors[cap]})" if cap in result.errors else ""
        print(f"  {cap:<24} {mark}{err}")
    print(f"  AppImages: {'supported' if result.supports_appimages else 'not supported'}")
    print(f"  Flatpak:   {'supported' if result.supports_flatpak else 'not supported'}")
    print(f"  probe directory removed: {'yes' if result.cleaned_up else 'NO — please inspect'}")
    return 0 if result.cleaned_up else 1


def cmd_storage_add(args: argparse.Namespace) -> int:
    reg = open_registry(args.registry)
    loc, result = locations.register(reg, args.path, args.label, default=args.default,
                                     apps_dir_name=args.apps_dir, run_probe=not args.no_probe)
    print(f"registered '{loc.label}' ({loc.location_class}) as {loc.id}")
    if result is not None and not result.cleaned_up:
        print("warning: the probe directory could not be removed", file=sys.stderr)
    return 0


def cmd_storage_list(args: argparse.Namespace) -> int:
    reg = open_registry(args.registry)
    cands = scan()
    rows = []
    for loc in reg.list_locations():
        rv = locations.resolve(loc, cands)
        try:
            apps = locations.apps_dir(loc, rv) if rv.online else None
        except CygnusError as exc:  # one bad entry must not hide the others
            apps, rv = None, locations.ResolvedLocation(loc, False, rv.path, str(exc))
        rows.append((loc, rv, apps))
    if args.json:
        _emit([{"id": l.id, "label": l.label, "class": l.location_class, "default": l.is_default,
                "online": r.online, "path": r.path, "reason": r.reason,
                "apps_dir": str(a) if a else None} for l, r, a in rows])
        return 0
    if not rows:
        print("no storage locations registered (use: cygnus storage add PATH --label NAME)")
    for loc, rv, apps in rows:
        state = f"online at {rv.path}" if rv.online else f"unavailable: {rv.reason}"
        default = "  [default]" if loc.is_default else ""
        print(f"{loc.label}{default}  ({loc.location_class}, {loc.fs_type})  {state}")
        if apps:
            print(f"  applications folder: {apps}")
        print(f"  id {loc.id}")
    return 0


def cmd_storage_default(args: argparse.Namespace) -> int:
    reg = open_registry(args.registry)
    loc = reg.get_location(args.id)
    if loc is None:
        raise CygnusError(f"no location with id {args.id}")
    loc.is_default = True
    reg.update_location(loc)
    print(f"'{loc.label}' is now the default location")
    return 0


def cmd_storage_remove(args: argparse.Namespace) -> int:
    reg = open_registry(args.registry)
    reg.remove_location(args.id)
    print("location removed from the registry (no files were touched)")
    return 0


# -- detect ---------------------------------------------------------------------------------------
def cmd_detect(args: argparse.Namespace) -> int:
    rc = 0
    out = []
    for f in args.files:
        try:
            cand = detect_file(f)
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the batch
            rc = 1
            out.append({"source": f, "error": str(exc)})
            if not args.json:
                print(f"{f}: {exc}")
            continue
        if args.json:
            out.append(cand.to_dict())
            continue
        print(f"{Path(f).name}")
        print(f"  format:  {cand.format}")
        print(f"  name:    {cand.name}   version: {cand.version}   arch: {cand.arch}")
        if cand.summary:
            print(f"  summary: {cand.summary}")
        for k, v in cand.identity.items():
            print(f"  {k}: {v}")
        if cand.depends:
            print(f"  depends: {', '.join(cand.depends[:12])}{' …' if len(cand.depends) > 12 else ''}")
        for finding in cand.findings:
            print(f"  [{finding.severity}] {finding.message}")
    if args.json:
        _emit(out)
    return rc


# -- health check ---------------------------------------------------------------------------------
def _find_manifest(query: str):
    from cygnus.core.manifest import catalog

    cat = catalog.bundled_manifests()
    q = query.lower()
    for app_id, loaded in cat.items():
        app = loaded.manifest.application
        if q in (app_id.lower(), app.name.lower(), *(n.lower() for n in app.package_names)):
            return loaded
    raise CygnusError(f"no manifest known for {query!r} (known: {', '.join(sorted(cat))})")


def _app_dirs(registry_path, extra: list[str]) -> list[Path]:
    dirs = [Path(d).expanduser() for d in extra]
    try:
        reg = open_registry(registry_path)
        cands = scan()
        for loc in reg.list_locations():
            rv = locations.resolve(loc, cands)
            if rv.online:
                try:
                    dirs.append(locations.apps_dir(loc, rv))
                except CygnusError:
                    pass
    except CygnusError:
        pass
    return dirs


def cmd_check(args: argparse.Namespace) -> int:
    from cygnus.core import inventory
    from cygnus.core.backends import flatpak as fb
    from cygnus.core.health.engine import InstallContext, evaluate, render_text

    loaded = _find_manifest(args.app)
    manifest = loaded.manifest
    try:
        env = fb.FlatpakEnv()
        fp_apps = {a["ref"]: a for a in fb.installed_apps_with_runtime(env)}
    except CygnusError:
        env, fp_apps = None, {}
    installs = inventory.find_installs(manifest, app_dirs=_app_dirs(args.registry, args.scan_dir), flatpak_env=env)
    try:
        installs += inventory.offline_installs(manifest, open_registry(args.registry))
    except CygnusError:
        pass
    if not installs:
        if args.json:
            _emit({"app": manifest.application.id, "trust": loaded.trust_level, "installs": [], "duplicate": None})
            return 1
        print(f"{manifest.application.name} is not installed (checked Flatpak, pacman and AppImage folders).")
        return 1
    reports = []
    for inst in installs:
        if inst.format == "appimage":
            ctx = InstallContext(source_id=inst.source_id, format="appimage", payload_path=inst.path,
                                 location_online=not inst.facts.get("offline"))
        elif inst.format == "flatpak":
            info = fp_apps.get(inst.flatpak_ref, {})
            ctx = InstallContext(source_id=inst.source_id, format="flatpak", flatpak_ref=inst.flatpak_ref,
                                 runtime_eol=info.get("runtime_eol"), runtime_installed=info.get("runtime_installed"))
        else:
            ctx = InstallContext(source_id=inst.source_id, format="pacman", package=inst.package)
        reports.append((inst, evaluate(manifest, ctx, trusted=loaded.can_drive_actions,
                                       dismissed=preferences.dismissed(manifest.application.id))))
    dup = inventory.duplicate_issue(manifest, installs)
    if args.json:
        _emit({"app": manifest.application.id, "trust": loaded.trust_level,
               "installs": [{"install": asdict(i), "overall": h.overall.value,
                             "features": [{"id": f.id, "name": f.name, "status": f.status.value,
                                           "explanation": f.explanation} for f in h.features],
                             "issues": [x.to_dict() for x in h.issues]} for i, h in reports],
               "duplicate": dup.to_dict() if dup else None})
        return 0
    for inst, health in reports:
        where = inst.path or inst.flatpak_ref or inst.package
        extra = " (running)" if inst.running else ""
        print(f"[{inst.format}] {where}{extra}" + (f"  version {inst.version}" if inst.version else ""))
        if inst.format == "flatpak" and fp_apps.get(inst.flatpak_ref, {}).get("runtime_eol"):
            print("  ⚠ runtime no longer receives security updates: " + fp_apps[inst.flatpak_ref]["runtime"])
        for ref in inst.referenced_by:
            print(f"  started from: {ref}")
        print("  " + render_text(health).replace("\n", "\n  "))
        print()
    if dup:
        print(f"[{dup.code}] {dup.title}: {dup.explanation}")
    return 0


# -- manifests ------------------------------------------------------------------------------------
def cmd_manifest_schema(args: argparse.Namespace) -> int:
    from cygnus.core.manifest.catalog import dumps_schema

    print(dumps_schema())
    return 0


def cmd_manifest_validate(args: argparse.Namespace) -> int:
    from cygnus.core.manifest.catalog import load_file

    rc = 0
    for f in args.files:
        try:
            lm = load_file(Path(f))
            print(f"{f}: valid — {lm.manifest.application.name}, serial {lm.manifest.serial}, "
                  f"trust {lm.trust_level}" + (f" ({'; '.join(lm.warnings)})" if lm.warnings else ""))
        except Exception as exc:  # noqa: BLE001
            rc = 1
            print(f"{f}: INVALID — {exc}")
    return rc


# -- application operations --------------------------------------------------------------------------
def _location_for(reg, path: str):
    """The registered location that holds `path`, if any."""
    cands = scan()
    best = None
    for loc in reg.list_locations():
        rv = locations.resolve(loc, cands)
        if rv.online and rv.path and (path == rv.path or path.startswith(rv.path.rstrip("/") + "/")):
            if best is None or len(rv.path) > len(best[1]):
                best = (loc, rv.path)
    return best[0] if best else None


def _answer_yes(prompt: str) -> bool:
    """Only an explicit yes counts; end of input (Ctrl-D) is a no."""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        print()
        return False


def _confirm(message: str, assume_yes: bool) -> bool:
    print(f"\n  {message}\n")
    return assume_yes or _answer_yes("Proceed? [y/N] ")


def _run_steps(kind: str, steps, reg, assume_yes: bool, description: str) -> int:
    from cygnus.core.ops import executor_for

    print(description)
    for s in steps:
        print(f"  • {s.description or s.kind}")
    if not _confirm("Apply these changes?", assume_yes):
        print("cancelled")
        return 1
    ex = executor_for(reg)
    report = ex.run(kind, steps, progress=lambda i, n, st: print(f"[{i + 1}/{n}] {st.description or st.kind}"))
    if report.state == "succeeded":
        for path in report.kept:
            print(f"  left in place ({report.kept_reasons.get(path, 'kept')}): {path}")
        print("done")
        return 0
    print(f"{report.state}: {report.error}")
    for e in report.compensation_errors:
        print(f"  could not undo: {e}")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    from cygnus.core.registry import db as regdb

    reg = open_registry(args.registry)
    rows = regdb.list_installations(reg)
    if args.json:
        _emit(rows)
        return 0
    if not rows:
        print("no applications are managed by Cygnus yet")
    for r in rows:
        where = r["source"].get("path") or r["source"].get("ref") or ""
        print(f"{r['name']}  ({r['format']}, {r['origin']}, version {r['version'] or '?'})  {where}")
    return 0


def cmd_feed(args: argparse.Namespace) -> int:
    from cygnus.core.backends import vendor_feeds as vf

    if args.sub == "list":
        mine = vf.user_feeds()
        for name in sorted({*vf.FEEDS, *mine}):
            feed = mine.get(name) or vf.FEEDS[name]
            print(f"{name}  {feed.index}  ({'added by you' if name in mine else 'built in'})")
        return 0
    if args.sub == "remove":
        if not vf.remove_user_feed(args.package):
            print(f"{args.package} is not one of the sources you added", file=sys.stderr)
            return 1
        print(f"removed the source you added for {args.package}")
        return 0
    feed = vf.Feed(args.package, args.index, args.base if args.base.endswith("/") else args.base + "/", args.rpm)
    release = vf.newest(feed, "deb")  # it must be readable, and must name the package, before it is kept
    vf.add_user_feed(feed)
    print(f"added: {args.package} is published as {release.version} at {feed.index}")
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    from cygnus.core import updates

    reg = open_registry(args.registry)
    if args.check:
        updates.check_all(reg, lambda line: print(line, file=sys.stderr))
    if args.check:
        try:
            updates.check_system_updates(lambda line: print(line, file=sys.stderr))
        except CygnusError as exc:
            print(f"system updates could not be checked: {exc}", file=sys.stderr)
    rows = updates.last_known(reg)
    system = updates.last_system_updates()
    available = any(u.status == updates.AVAILABLE for u in rows)
    if args.json:
        _emit({"applications": [u.as_dict() for u in rows], "system": system})
        return 1 if available else 0
    if system:
        if system.get("error"):
            print(f"System packages: could not check ({system['error']})")
        else:
            n = len(system["packages"])
            print(f"System packages: {n} update{'s' if n != 1 else ''} available" if n else "System packages: up to date",
                  "(includes a new kernel)" if system.get("kernel") else "", f"— checked {system['checked_at']}")
    if not rows:
        print("no applications are managed by Cygnus yet")
    for u in rows:
        extra = f" → {u.available}" if u.status == updates.AVAILABLE and u.available else ""
        print(f"{u.name} {u.current or ''}: {u.status}{extra}  ({u.detail})")
    return 1 if available else 0


def _say(line: str, indent: str = "") -> None:
    """A progress line on the terminal. A line that only moves a progress bar (libflatpak reports every quarter of a
    second) is not printed."""
    if getattr(line, "log", True):
        print(indent + line)


def _helper_commit(client, plan, assume_yes: bool) -> bool:
    from cygnus.core.privilege import HelperTimeout

    print(plan.message)
    if not _confirm("Continue (your password will be asked)?", assume_yes):
        print("cancelled")
        return False
    try:
        ok, detail = client.commit(plan, on_progress=lambda line: _say(line, "  "))
    except HelperTimeout as timeout:
        # No answer came back. The helper's ledger knows how the operation ended, and nothing is recorded as done
        # (or forgotten) unless it says succeeded.
        try:
            state = client.operation_state(timeout.op_id, timeout_ms=10_000)
        except Exception:  # noqa: BLE001 - not knowing is not an answer
            state = None
        if state == "succeeded":
            return True
        if state == "failed":
            print("not completed: the administrator helper reported that the change failed")
            return False
        raise CygnusError("Cygnus lost contact with the administrator helper while it was working"
                          + (" and it stopped before it finished" if state == "interrupted" else "")
                          + ". Cygnus's own list of installed software was NOT updated for this change, so check what "
                            "was really changed (`pacman -Q`, `cygnus doctor`) before trying again.") from timeout
    if not ok:
        print(f"not completed: {detail.splitlines()[-1] if detail else 'failed'}")
    return ok


def _print_review(r: dict) -> None:
    print(f"{r['name']} {r['version']} — {r['description']}")
    print(f"  maintainer {r['maintainer']}, {r['votes']} votes" + (", FLAGGED OUT OF DATE" if r["out_of_date"] else ""))
    print(f"  build files at commit {r['commit']}")
    if r["diff"]:
        print(f"\n--- changes since the version you approved ({r['previously_approved'][:12]}) ---\n{r['diff']}")
    else:
        for name, text in r["files"].items():
            print(f"\n===== {name} =====\n{text}")
    for h in r["hints"]:
        print(f"  ! {h['file']}:{h['line']}: {h['why']}: {h['text']}")
    for problem in r["problems"]:
        print(f"  ✗ cannot be reviewed in full: {problem}")
    if r["repo_dependencies"]:
        print("  needs from your repositories: " + ", ".join(r["repo_dependencies"]))
    if r["aur_dependencies"]:
        print("  needs other AUR packages (install them first): " + ", ".join(r["aur_dependencies"]))


def cmd_aur(args: argparse.Namespace) -> int:
    from cygnus.core.privilege import HelperClient
    from cygnus.gui import service

    r = service.aur_review(args.name)
    for dep in r["dependencies"]:  # dependencies first: they are built and installed first
        print(f"\n######## {dep['pkgbase']} (needed by {args.name})")
        _print_review({**dep, "name": dep["pkgbase"], "description": "AUR dependency", "maintainer": "?", "votes": "?",
                       "out_of_date": False, "repo_dependencies": [], "aur_dependencies": []})
    print(f"\n######## {r['pkgbase']}")
    _print_review(r)
    for b in r["blockers"]:
        print(f"  ✗ {b}")
    commits = {d["pkgbase"]: d["commit"] for d in r["dependencies"]} | {r["pkgbase"]: r["commit"]}
    if args.sub == "review":
        flags = " ".join(f"--reviewed {base}={c}" if base != r["pkgbase"] else f"--reviewed {c}"
                         for base, c in commits.items())
        print(f"\nTo build these exact versions: cygnus aur install {args.name} {flags}")
        return 0
    if not r["buildable"]:
        return 1
    if args.reviewed:
        given = {}
        for item in args.reviewed:
            base, _, commit = item.rpartition("=")
            given[base or r["pkgbase"]] = commit
        if given != commits:
            raise CygnusError("the build files changed since the versions you reviewed (or some were not named); "
                              "review them again")
    elif not sys.stdin.isatty() or not _answer_yes(f"\nHave you read the build files above ({len(commits)} package"
                                                   f"{'s' if len(commits) > 1 else ''}), and should Cygnus build them "
                                                   "as you? [y/N] "):
        print("not built")
        return 1
    from cygnus.core.detect import detect_file as _detect

    client = HelperClient()
    if r["repo_dependencies"] and not _helper_commit(
            client, client.plan_packages(install_repo=r["repo_dependencies"], asdeps=True), args.yes):
        return 1
    for dep in r["dependencies"]:
        built = service.aur_build(dep["pkgbase"], dep["commit"], _say, names=dep["names"])["packages"]
        plan = client.plan_packages(local_files=[(p, service.file_sha256(p)) for p in built], asdeps=True)
        if not _helper_commit(client, plan, args.yes):
            return 1
        service.record_aur_install(dep["pkgbase"], dep["names"][0], dep["commit"], dep.get("version", ""),
                                   packages=[_detect(p).name for p in built], dependency_of=args.name,
                                   registry_path=args.registry)
    built = service.aur_build(r["pkgbase"], r["commit"], _say, name=args.name)["packages"]
    plan = client.plan_packages(local_files=[(p, service.file_sha256(p)) for p in built])
    if not _helper_commit(client, plan, args.yes):
        return 1
    service.record_aur_install(r["pkgbase"], args.name, r["commit"], r["version"],
                               packages=[_detect(p).name for p in built], registry_path=args.registry)
    print("done")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    from cygnus.gui import service

    if args.pacman_lock:
        from cygnus.core.privilege import HelperClient

        if service.stale_pacman_lock() is None:
            print("pacman is not locked, or a package manager is running (then the lock is in use)")
            return 0
        client = HelperClient()
        ok = _helper_commit(client, client.plan_clear_stale_lock(), args.yes)
        print("done" if ok else "not removed")
        return 0 if ok else 1

    ops = service.interrupted_operations(args.registry)
    if not (args.finish or args.undo):
        if not ops:
            print("no interrupted operations")
        for op in ops:
            print(f"{op['id']}  {op['title']} — {op['progress']} ({op['state']}, started {op['started']})")
        return 1 if ops else 0
    op_id = args.finish or args.undo
    op = next((o for o in ops if o["id"] == op_id), None)
    if op is None:
        raise CygnusError(f"there is no interrupted operation {op_id!r} (run 'cygnus recover' to list them)")
    finish = bool(args.finish)
    affected = [s for s in op["steps"] if (s["state"] != "done") == finish]
    print(f"{op['title']}: {op['progress']}")
    print(("Steps still to run:" if finish else "Completed steps to undo:"))
    for s in affected:
        print(f"  - {s['description']}")
    if not _confirm(("Finish" if finish else "Undo") + " this operation?", args.yes):
        print("cancelled")
        return 1
    result = service.recover(op_id, "resume" if finish else "rollback", _say, args.registry)
    print("done" if result["ok"] else f"not completed: {result['error']}")
    return 0 if result["ok"] else 1


def cmd_watch(args: argparse.Namespace) -> int:
    """Check for updates and problems and notify about anything new (what the background timer runs)."""
    from cygnus.core import watch
    from cygnus.gui import service

    if args.enable or args.disable:
        watch.set_timer(bool(args.enable))
    if args.enable or args.disable or args.status:
        print("background checks: " + ("on" if watch.timer_enabled() else "off"))
        return 0
    shown = watch.run(gather=lambda: service.watch_gather(lambda line: print(line, file=sys.stderr),
                                                          registry_path=args.registry))
    for n in shown:
        print(f"notified: {n.title} — {n.body}")
    if not shown:
        print("nothing new")
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    from cygnus.gui import service

    service.dismiss_component(args.app, args.component, not args.undo)
    print(f"{args.component}: " + ("checked and suggested again" if args.undo else
                                    "no longer checked or suggested (cygnus dismiss --undo to change your mind)"))
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Update the whole system through the helper (fresh databases; never a partial upgrade)."""
    from cygnus.core.privilege import HelperClient

    client = HelperClient()
    print("Downloading the current package databases…")
    plan = client.plan_packages(sysupgrade=True)
    s = plan.summary
    if not (s.get("upgrade") or s.get("install") or s.get("remove")):
        print("The system is up to date.")
        return 0
    for p in s.get("upgrade", []):
        print(f"  {p['name']:<36} {p.get('from') or '':<22} → {p['version']}")
    for p in s.get("install", []):
        print(f"  {p['name']:<36} {'(new)':<22} → {p['version']}" + (f"  ({p['reason']})" if p.get("reason") else ""))
    for p in s.get("remove", []):
        print(f"  {p['name']:<36} removed ({p.get('reason', '')})")
    if s.get("download_bytes"):
        print(f"Download: {s['download_bytes'] / 2**20:.0f} MiB")
    return 0 if _helper_commit(client, plan, args.yes) else 1


def cmd_adopt(args: argparse.Namespace) -> int:
    from cygnus.core.manifest import catalog
    from cygnus.core.models import PackageFormat
    from cygnus.core.ops import appimage_ops

    cand = detect_file(args.file)
    if cand.format is not PackageFormat.APPIMAGE:
        raise CygnusError("only AppImages can be adopted in place for now")
    reg = open_registry(args.registry)
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    if appimage_ops.require_pinned_signature(Path(cand.source), manifest):
        print(f"Signature verified with {manifest.manifest.application.vendor.name}'s pinned key")
    loc = _location_for(reg, str(Path(cand.source)))
    key = appimage_ops.app_key_for(cand, manifest.manifest.application.id if manifest else None)
    from cygnus.core.desktop import integrate
    from cygnus.core.inventory import entry_references

    refs = entry_references([integrate.xdg_config_home() / "autostart"]).get(cand.source, [])
    steps = appimage_ops.plan_adopt(cand, app_key=key, location=loc, manifest=manifest,
                                    autostart=[Path(r) for r in refs])
    return _run_steps("adopt", steps, reg, args.yes,
                      f"Adopt {cand.name} where it is ({cand.source})"
                      + (f" on {loc.label}" if loc else "") + " and add it to the application menu:")


def cmd_install(args: argparse.Namespace) -> int:
    from cygnus.core.manifest import catalog
    from cygnus.core.models import PackageFormat
    from cygnus.core.ops import appimage_ops

    if not Path(args.file).exists() and not args.file.startswith(("/", "./", "~")):
        return _install_flatpak_ref(args)
    cand = detect_file(args.file)
    if cand.format is PackageFormat.FLATPAK_BUNDLE:
        return _install_flatpak_bundle(args)
    if cand.format in (PackageFormat.DEB, PackageFormat.RPM):
        return _install_foreign(args)
    if cand.format is not PackageFormat.APPIMAGE:
        raise CygnusError("`cygnus install` handles AppImages, Flatpak bundles and Flatpak app ids "
                          "(e.g. flathub:org.example.App); open other files in the Cygnus app")
    if not args.to:
        raise CygnusError("choose where to store the AppImage with --to (see `cygnus storage list`)")
    reg = open_registry(args.registry)
    loc = next((l for l in reg.list_locations() if l.label.lower() == args.to.lower()), None)
    if loc is None:
        raise CygnusError(f"no storage location named {args.to!r} (see `cygnus storage list`)")
    rv = locations.resolve(loc)
    if not rv.online:
        raise CygnusError(f"{loc.label} is not available: {rv.reason}")
    manifest = catalog.find_for({**cand.identity, "name": cand.name or ""}, catalog.bundled_manifests())
    key = appimage_ops.app_key_for(cand, manifest.manifest.application.id if manifest else None)
    steps = appimage_ops.plan_install(cand, app_key=key, location=loc, apps_dir=locations.apps_dir(loc, rv),
                                      manifest=manifest)
    return _run_steps("install", steps, reg, args.yes, f"Install {cand.name} on {loc.label}:")


def _print_plan(plan: dict) -> bool:
    print(f"{plan['name']} ({plan.get('ref') or plan['format']}) → {plan['target']}")
    for pl in plan["placements"]:
        size = f"  {pl['bytes'] / 2**20:.0f} MiB" if pl.get("bytes") else ""
        print(f"  {pl['name']:<22} {pl['location']}{size}  ({pl['reason']})")
    for i in plan["issues"]:
        print(f"  [{i['severity']}] {i['title']}" + (f": {i['explanation']}" if i.get("explanation") else ""))
    if not plan["installable"]:
        print("Cygnus will not install this (see above).")
    return plan["installable"]


def _install_flatpak_ref(args: argparse.Namespace) -> int:
    from cygnus.gui import service

    plan = service.analyse_flatpak_ref(args.file, args.to, args.registry)
    if not _print_plan(plan):
        return 1
    if not _confirm("Install?", args.yes):
        print("cancelled")
        return 1
    result = service.install_flatpak_ref(args.file, args.to, _say, args.registry)
    print("done" if result["ok"] else f"not installed: {result['error']}")
    return 0 if result["ok"] else 1


def _install_flatpak_bundle(args: argparse.Namespace) -> int:
    from cygnus.gui import service

    plan = service.analyse(args.file, args.to, args.registry)
    if not _print_plan(plan):
        return 1
    if not _confirm("Install?", args.yes):
        print("cancelled")
        return 1
    result = service.install_flatpak_bundle(args.file, args.to, _say, args.registry)
    print("done" if result["ok"] else f"not installed: {result['error']}")
    return 0 if result["ok"] else 1


def _install_foreign(args: argparse.Namespace) -> int:
    from cygnus.core.privilege import HelperClient
    from cygnus.gui import service

    plan = service.analyse(args.file, None, args.registry, progress=_say)
    print(f"{plan['name']} {plan['version']}: {plan['verdict']}")
    for i in plan["issues"]:
        print(f"  [{i['severity']}] {i['title']}" + (f": {i['explanation']}" if i.get("explanation") else ""))
    if plan.get("depends"):
        print("  also installed from your repositories, because it needs them: " + ", ".join(plan["depends"]))
    optional = [o["package"] for o in plan.get("optional") or []]
    for o in plan.get("optional") or []:
        print(f"  optional: {o['package']} ({', '.join(o['files'])} use it)"
              + ("; will be installed (--with-optional)" if args.with_optional else "; add --with-optional to install it"))
    accept = False
    if plan["strategy"] != "convert":
        if not plan.get("installable_after_review"):
            return 1
        scripts = plan["scripts"]
        print("\nCygnus could not fully read this package's install scripts. Converting never runs them, so nothing they "
              "would do happens. They mention:")
        for m in scripts["mentions"]:
            print(f"  {m['command']}: {m['what']} ({', '.join(m['scripts'])})")
        for group in scripts.get("effects") or []:
            print(f"  {group['title']}:")
            for item in group["items"]:
                print(f"    {item}")
        if not args.accept_scripts:
            print("\nRead the scripts (the whole text is in the package: `ar x file.deb`), then run this again with "
                  "--accept-scripts if you want to convert it anyway.")
            return 1
        accept = True
    if not _confirm("Convert it into a pacman package and install that?", args.yes):
        print("cancelled")
        return 1
    converted = service.convert_foreign(args.file, _say, plan.get("sha256"),
                                        optional if args.with_optional else (), accept)
    print("converted: " + "; ".join(converted["notes"] or ["no changes needed"]))
    client = HelperClient()
    if not _helper_commit(client, client.plan_packages(
            local_files=[(converted["package"], service.file_sha256(converted["package"]))]), args.yes):
        return 1
    service.record_package_install(converted["name"], converted["version"], origin="converted",
                                   source={"file": args.file, "vendor_package": converted["vendor_package"],
                                           "vendor_format": converted["vendor_format"]}, registry_path=args.registry)
    print("done")
    return 0


def _find_installation(reg, query: str) -> dict:
    from cygnus.core.registry import db as regdb

    q = query.lower()
    matches = [r for r in regdb.list_installations(reg) if q in (r["name"].lower(), r["app_id"].lower())]
    if not matches:
        raise CygnusError(f"{query!r} is not managed by Cygnus")
    if len(matches) > 1:
        raise CygnusError(f"{query!r} matches several installations: "
                          + ", ".join(f"{m['name']} ({m['format']})" for m in matches))
    return matches[0]


def cmd_uninstall(args: argparse.Namespace) -> int:
    from cygnus.core.ops import plan_uninstall
    from cygnus.core.registry import db as regdb

    reg = open_registry(args.registry)
    inst = _find_installation(reg, args.app)
    if inst["format"] in ("aur", "pacman"):
        from cygnus.core.privilege import HelperClient

        client = HelperClient()
        names = [a["locator"] for a in regdb.artifacts_of(reg, inst["id"]) if a["kind"] == "package"]
        if not _helper_commit(client, client.plan_packages(remove=names), args.yes):
            return 1
        with reg.transaction() as c:
            c.execute("DELETE FROM artifact WHERE installation_id=?", (inst["id"],))
        regdb.remove_installation(reg, inst["id"])
        print("done")
        return 0
    adopted = [a["locator"] for a in regdb.artifacts_of(reg, inst["id"]) if a["ownership"] == "adopted"]
    steps = plan_uninstall(reg, inst["id"], remove_payload=args.delete_file,
                           remove_adopted=adopted if args.remove_autostart else [])
    return _run_steps("uninstall", steps, reg, args.yes, f"Remove {inst['name']}:")


def cmd_move(args: argparse.Namespace) -> int:
    from cygnus.core import planner
    from cygnus.core.ops import appimage_ops

    reg = open_registry(args.registry)
    inst = _find_installation(reg, args.app)
    if inst["format"] == "flatpak":
        from cygnus.gui import service

        if not _confirm(f"Move {inst['name']} to {args.to}?", args.yes):
            print("cancelled")
            return 1
        result = service.move_flatpak(inst["id"], args.to, _say, args.registry)
        print("done" if result["ok"] else f"{result['state']}: {result['error']}")
        return 0 if result["ok"] else 1
    if inst["format"] != "appimage":
        raise CygnusError("system packages always live on the system drive")
    loc = next((l for l in reg.list_locations() if args.to in (l.id, l.label)), None)
    if loc is None:
        raise CygnusError(f"no storage location {args.to!r}")
    ok, reason = planner.location_supports(loc, "appimage")
    if not ok:
        raise CygnusError(reason)
    rv = locations.resolve(loc)
    if not rv.online:
        raise CygnusError(f"{loc.label} is not available: {rv.reason}")
    steps = appimage_ops.plan_move(reg, inst["id"], detect_file(inst["source"]["path"]), location=loc,
                                   apps_dir=locations.apps_dir(loc, rv))
    return _run_steps("move", steps, reg, args.yes, f"Move {inst['name']} to {loc.label}:")


def cmd_repair(args: argparse.Namespace) -> int:
    from cygnus.core.manifest import catalog
    from cygnus.core.ops import appimage_ops

    reg = open_registry(args.registry)
    inst = _find_installation(reg, args.app)
    if inst["format"] == "flatpak":
        from cygnus.core.ops import flatpak_ops
        from cygnus.gui import service

        for p in flatpak_ops.diagnose(inst["source"]):
            print(f"{p['what']} {p['state']}: {p['path']}")
        if not _confirm(f"Repair {inst['name']} (and check its Flatpak installation)?", args.yes):
            print("cancelled")
            return 1
        result = service.repair_app(inst["id"], _say)
        print("done" if result["ok"] else f"{result['state']}: {result['error']}")
        return 0 if result["ok"] else 1
    if inst["format"] != "appimage":
        raise CygnusError("system packages are repaired with pacman (they are verified when installed)")
    problems = appimage_ops.diagnose(reg, inst["id"])
    if not problems:
        print(f"nothing to repair: everything Cygnus set up for {inst['name']} is in place")
        return 0
    for p in problems:
        print(f"{p['what']} {p['state']}: {p['path']}")
    payload = next((p for p in problems if p["what"] == "payload"), None)
    if payload and payload["state"] != "changed":
        raise CygnusError(f"the application file is {payload['state']}; reinstall or reconnect its drive")
    loc = next((l for l in reg.list_locations() if l.id == inst["location_id"]), None)
    manifest = catalog.bundled_manifests().get(inst["app_id"])
    steps = appimage_ops.plan_repair(reg, inst["id"], detect_file(inst["source"]["path"]), location=loc,
                                     manifest=manifest)
    return _run_steps("repair", steps, reg, args.yes, f"Repair {inst['name']}:")


def cmd_fix(args: argparse.Namespace) -> int:
    """Install a missing component of an application (e.g. WhatPulse input access)."""
    from cygnus.core.health.engine import _component_health
    from cygnus.core.health.probes import run_probe
    from cygnus.core.ops.components import Declined, apply_actions
    from cygnus.core.privilege import HelperClient

    loaded = _find_manifest(args.app)
    comp = loaded.manifest.component(args.component)
    if comp is None:
        known = ", ".join(c.id for c in loaded.manifest.components)
        raise CygnusError(f"unknown component {args.component!r} (known: {known})")
    if not loaded.can_drive_actions:
        raise CygnusError("this manifest is not trusted enough to install components")
    health = _component_health(comp, run_probe)
    if health.status.value == "ok" and not args.force:
        print(f"{comp.name} is already working.")
        return 0
    from cygnus.core.health.engine import component_issue

    issue = component_issue(health, loaded.manifest)
    res = issue.preferred() if issue else None
    if res is None or not res.actions:
        if res is None:  # failing, and Cygnus has nothing it can do: say so instead of "nothing to do"
            why = issue.explanation if issue and issue.explanation else "its checks did not pass"
            print(f"{comp.name} is not working ({why}). Cygnus has no automatic fix for this.")
            return 1
        print(f"{comp.name}: {res.title} — {res.explanation}")
        return 0
    print(f"{comp.name}: {res.explanation}")
    for line in comp.discouraged_vendor_instructions:
        print(f"  (not doing the vendor's instruction: {line})")
    needs_helper = any(a.kind != "browser.open_store" for a in res.actions)
    if needs_helper:
        from cygnus.gui.fixes import identity_caution

        from cygnus.core.health.engine import privileges_note

        for note in (comp.security_note, privileges_note(comp), identity_caution(loaded, open_registry(args.registry))):
            if note:
                print(f"  NOTE: {note}")
    client = HelperClient() if needs_helper else None
    try:
        log = apply_actions(res.actions, client=client, confirm=lambda m: _confirm(m, args.yes),
                            browsers={k: v.model_dump() for k, v in comp.browsers.items()})
    except Declined:
        print("cancelled")
        return 1
    for line in log:
        print(f"  {line}")
    if comp.requires_relogin:
        print("Log out and back in for this change to take effect.")
    return 0


# -- doctor ---------------------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"Cygnus {__version__} ({APP_ID})")
    print(f"Python {sys.version.split()[0]}")
    checks = {
        "pacman": "pacman", "pacman-conf": "pacman", "flatpak": "flatpak", "ostree": "ostree",
        "unsquashfs": "squashfs-tools", "zsync": "zsync", "bsdtar": "libarchive",
        "desktop-file-validate": "desktop-file-utils", "kbuildsycoca6": "kservice",
        "update-desktop-database": "desktop-file-utils",
    }
    for tool, package in checks.items():
        path = proc.which(tool)
        print(f"  {tool:<24} {path or f'MISSING (package {package})'}")
    for mod in ("pyalpm", "gi", "cryptography", "systemd.journal", "PySide6"):
        try:
            __import__(mod)
            print(f"  python: {mod:<16} ok")
        except Exception as exc:  # noqa: BLE001
            print(f"  python: {mod:<16} MISSING ({type(exc).__name__})")
    from cygnus.core.ops import flatpak_ops

    state = flatpak_ops.relocation_state()
    if state.relocated_to is None:
        print("  flatpak user storage     on the system drive")
    else:
        try:
            missing = flatpak_ops.ensure_relocation_links(dry_run=True)  # doctor only looks
            online = state.relocated_to.is_dir()
            print(f"  flatpak user storage     {state.relocated_to}" + ("" if online else " (drive not connected)")
                  + (f" — links missing: {', '.join(missing)} (Cygnus restores them before its next Flatpak "
                     "operation)" if missing else ""))
        except CygnusError as exc:
            print(f"  flatpak user storage     PROBLEM: {exc}")
            return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from cygnus.core import report

    print(report.build(args.registry))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cygnus", description="Application installer and manager for CachyOS")
    p.add_argument("--version", action="version", version=f"cygnus {__version__}")
    p.add_argument("--registry", default=None, help="registry database path (default: XDG data dir)")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("storage", help="application storage locations").add_subparsers(dest="sub", required=True)
    s = st.add_parser("scan", help="list filesystems that could hold applications")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_storage_scan)
    s = st.add_parser("probe", help="test what a directory's filesystem supports (creates and removes a temp dir)")
    s.add_argument("path")
    s.add_argument("--no-ostree", action="store_true", help="skip the OSTree (Flatpak) compatibility test")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_storage_probe)
    s = st.add_parser("add", help="register a directory as a storage location")
    s.add_argument("path")
    s.add_argument("--label", required=True)
    s.add_argument("--default", action="store_true")
    s.add_argument("--apps-dir", default="Applications", help="applications folder relative to PATH")
    s.add_argument("--no-probe", action="store_true")
    s.set_defaults(func=cmd_storage_add)
    s = st.add_parser("list", help="show registered locations and whether they are available")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_storage_list)
    s = st.add_parser("set-default", help="make a location the default")
    s.add_argument("id")
    s.set_defaults(func=cmd_storage_default)
    s = st.add_parser("remove", help="forget a location (files are not touched)")
    s.add_argument("id")
    s.set_defaults(func=cmd_storage_remove)

    s = sub.add_parser("detect", help="identify package files and show their metadata")
    s.add_argument("files", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("check", help="check an application's health (all installed copies)")
    s.add_argument("app", help="application id or name, e.g. WhatPulse")
    s.add_argument("--scan-dir", action="append", default=[], help="extra folder with AppImages to look in")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_check)

    mf = sub.add_parser("manifest", help="vendor manifests").add_subparsers(dest="sub", required=True)
    s = mf.add_parser("schema", help="print the CAM v1 JSON Schema")
    s.set_defaults(func=cmd_manifest_schema)
    s = mf.add_parser("validate", help="validate manifest files (and .minisig signatures next to them)")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cmd_manifest_validate)

    s = sub.add_parser("list", help="applications managed by Cygnus")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)
    s = sub.add_parser("watch", help="check for updates and problems and notify (run by a background timer)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true", help="run it in the background every few hours")
    g.add_argument("--disable", action="store_true", help="stop the background checks")
    g.add_argument("--status", action="store_true", help="show whether background checks are on")
    s.set_defaults(func=cmd_watch)
    s = sub.add_parser("dismiss", help="stop checking and suggesting an optional feature you do not want")
    s.add_argument("app")
    s.add_argument("component")
    s.add_argument("--undo", action="store_true", help="check and suggest it again")
    s.set_defaults(func=cmd_dismiss)
    s = sub.add_parser("upgrade", help="update the whole system through the helper (never a partial upgrade)")
    s.add_argument("--yes", action="store_true", help="do not ask before the password prompt")
    s.set_defaults(func=cmd_upgrade)
    fd = sub.add_parser("feed", help="where newer versions of programs converted from a .deb/.rpm are published").add_subparsers(
        dest="sub", required=True)
    s = fd.add_parser("list", help="the built-in sources and the ones you added")
    s.set_defaults(func=cmd_feed)
    s = fd.add_parser("add", help="add the package list (an apt Packages.gz) a program's vendor publishes")
    s.add_argument("package", help="the vendor's package name, as in the .deb's Package: line")
    s.add_argument("--index", required=True, help="https address of the Packages.gz")
    s.add_argument("--base", required=True, help="https folder the Filename: lines are relative to")
    s.add_argument("--rpm", help="https address of the newest .rpm, for programs converted from the vendor's rpm")
    s.set_defaults(func=cmd_feed)
    s = fd.add_parser("remove", help="forget a source you added")
    s.add_argument("package")
    s.set_defaults(func=cmd_feed)
    s = sub.add_parser("updates", help="show update status (exit 1 if updates are available)")
    s.add_argument("--check", action="store_true", help="check online now (otherwise show the last check)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_updates)
    au = sub.add_parser("aur", help="community packages from the AUR (always reviewed before building)")
    au_sub = au.add_subparsers(dest="sub", required=True)
    s = au_sub.add_parser("review", help="show a package's build files (nothing is run)")
    s.add_argument("name")
    s.set_defaults(func=cmd_aur)
    s = au_sub.add_parser("install", help="review, build as you, then install through the helper")
    s.add_argument("name")
    s.add_argument("--reviewed", metavar="[BASE=]COMMIT", action="append",
                   help="the build-file commit you reviewed, for each package (non-interactive)")
    s.add_argument("--yes", action="store_true", help="do not ask before the administrator steps")
    s.set_defaults(func=cmd_aur)
    s = sub.add_parser("move", help="move an AppImage or Flatpak to another storage location")
    s.add_argument("app")
    s.add_argument("--to", required=True, help="location id or label")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_move)
    s = sub.add_parser("repair", help="restore what Cygnus set up for an application (menu entry, launcher, icon)")
    s.add_argument("app")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_repair)
    s = sub.add_parser("recover", help="list operations that did not finish; finish or undo one")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--finish", metavar="OP", help="run the remaining steps")
    g.add_argument("--undo", metavar="OP", help="undo the completed steps")
    g.add_argument("--pacman-lock", action="store_true",
                   help="remove pacman's lock file left behind by a crashed package manager (via the helper)")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_recover)
    s = sub.add_parser("adopt", help="manage an existing AppImage where it is and add it to the menu")
    s.add_argument("file")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_adopt)
    s = sub.add_parser("install", help="install an AppImage, a Flatpak bundle or a Flatpak app id "
                                       "(e.g. flathub:org.example.App) onto a storage location")
    s.add_argument("file", help="file, or [remote:]app.id[//branch]")
    s.add_argument("--to", default=None,
                   help="storage location label, e.g. HDD (needed for AppImages; Flatpaks default to the "
                        "default location; system packages always go to the system drive)")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("--with-optional", action="store_true",
                   help=".deb/.rpm: also install the repository packages that only some parts of the program use")
    s.add_argument("--accept-scripts", action="store_true",
                   help=".deb/.rpm: convert even though Cygnus could not read all of the install scripts (they are never "
                        "run, so what they would have done does not happen); read them first")
    s.set_defaults(func=cmd_install)
    s = sub.add_parser("uninstall", help="remove an application managed by Cygnus")
    s.add_argument("app")
    s.add_argument("--delete-file", action="store_true", help="also delete the application file itself")
    s.add_argument("--remove-autostart", action="store_true", help="also remove autostart entries it had")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_uninstall)
    s = sub.add_parser("fix", help="install a missing component of an application")
    s.add_argument("app")
    s.add_argument("component", help="component id from the manifest, e.g. input-access")
    s.add_argument("--force", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("report", help="print a report to paste into a bug report (no private details; changes nothing)")
    s.set_defaults(func=cmd_report)
    s = sub.add_parser("doctor", help="check the environment Cygnus depends on")
    s.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CygnusError, OSError, sqlite3.Error) as exc:
        print(f"cygnus: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - a bug: say so plainly; CYGNUS_DEBUG=1 shows the traceback
        if os.environ.get("CYGNUS_DEBUG") == "1":
            raise
        print(f"cygnus: unexpected error ({type(exc).__name__}: {exc}); run again with CYGNUS_DEBUG=1 for "
              "details", file=sys.stderr)
        return 2
