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
from cygnus.core import paths, preferences
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


def _run_steps(kind: str, steps, reg, assume_yes: bool, description: str, *, before_apply=None, after=None) -> int:
    """Show the steps, ask once, run them. `before_apply()` runs after the question and before the first step (returning False cancels);
    `after(report)` runs after a success and returns an exit status to add (for a script that did not finish)."""
    from cygnus.core.ops import executor_for

    print(description)
    for s in steps:
        print(f"  • {s.description or s.kind}")
    if not _confirm("Apply these changes?", assume_yes):
        print("cancelled")
        return 1
    if before_apply is not None and not before_apply():
        print("cancelled")
        return 1
    ex = executor_for(reg)
    report = ex.run(kind, steps, progress=lambda i, n, st: print(f"[{i + 1}/{n}] {st.description or st.kind}"))
    if report.state == "succeeded":
        for path in report.kept:
            print(f"  left in place ({report.kept_reasons.get(path, 'kept')}): {path}")
        print("done")
        return after(report) if after is not None else 0
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


def _age_words(fetched: float) -> str:
    import time

    if not fetched:
        return "not downloaded yet"
    days = int((time.time() - fetched) // 86400)
    return "downloaded today" if days < 1 else "downloaded yesterday" if days == 1 else f"downloaded {days} days ago"


def cmd_discover_catalogues(args: argparse.Namespace) -> int:
    """The online catalogues (AppImageHub...): which are on, how many programs each holds, and downloading them. Only `update` uses the network."""
    from cygnus.core import online_catalogues as oc
    from cygnus.gui import service

    if args.sub == "catalogues":
        rows = oc.status()
        if args.json:
            _emit(rows)
            return 0
        for row in rows:
            print(f"{row['id']:<14} {'on ' if row['on'] else 'off'}  {row['count']:>6} programs  {_age_words(row['fetched'])}  ({row['size']})")
            print(f"{'':<14} {row['what']}")
        if not oc.decided():
            print("\nNothing is downloaded until you say so: `cygnus discover update` downloads the catalogues that are on, or open Discover in the Cygnus window.")
        return 0
    if args.sub == "catalogue":
        rows = service.catalogue_set_on(args.id, args.state == "on")["status"]
        print(f"{args.id} is now {args.state}" + ("" if args.state == "off" else "; `cygnus discover update` downloads it if it is missing or old"))
        return 0
    if not oc.decided():
        oc.decide(True)  # typing `cygnus discover update` is the answer to the question the window asks
    out = service.catalogue_refresh(bool(args.all), lambda line: print(line, file=sys.stderr))
    if out["busy"]:
        print("another update of the catalogues is still running; try again when it has finished", file=sys.stderr)
        return 1
    for cat_id in out["refreshed"]:
        row = next(r for r in out["status"] if r["id"] == cat_id)
        print(f"{cat_id}: {row['count']} programs")
    for message in out["errors"].values():
        print(message, file=sys.stderr)
    if not out["refreshed"] and not out["errors"]:
        if not any(r["on"] for r in out["status"]):
            print("no catalogue is switched on; `cygnus discover catalogue ID on` switches one on (`cygnus discover catalogues` lists them)")
        else:
            print("the catalogues that are on are recent; --all downloads them again")
    return 1 if out["errors"] else 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Browse what can be installed (reads the application lists; installs nothing, downloads nothing)."""
    from cygnus.core import discover
    from cygnus.gui import service

    if args.sub in ("catalogues", "catalogue", "update"):
        return cmd_discover_catalogues(args)
    if args.sub == "info":
        entry = service.discover_app(args.key)
        if args.json:
            _emit(entry)
            return 0
        print(f"{entry['name']}" + (f"  ({entry['developer']})" if entry["developer"] else ""))
        print(f"  {entry['summary']}")
        if entry["description"]:
            print("\n" + "\n".join(f"  {line}" for line in entry["description"].splitlines()))
        if entry["url"]:
            print(f"\n  website: {entry['url']}")
        print("\nWays to get it:")
        for src in entry["sources"]:
            mark = "  [installed]" if src["installed"] else ""
            if src["kind"] == "flatpak":
                print(f"  • Flatpak: cygnus install {src['remote']}:{src['id']}{mark}")
            elif src["kind"] == "repo":
                print(f"  • Your repositories: cygnus packages install {src['package']}{mark}")
            elif src["kind"] == "release":
                from cygnus.core.backends import release_feeds

                kinds = " / ".join(release_feeds.LABELS.get(f, f) for f in src["formats"])
                print(f"  • The maker's own download ({kinds}): Discover in the Cygnus window fetches and checks the newest file; or get it from "
                      f"{src['page']}, then cygnus install FILE{mark}")
            elif src["kind"] == "appimage":
                print(f"  • A community listing (AppImageHub), not checked by Cygnus: Discover in the Cygnus window fetches the newest AppImage; or get it from "
                      f"{src['page']}, then cygnus install FILE{mark}")
            elif src["kind"] == "debian":
                needs = f"; Debian also lists as needed: {', '.join(src['needs'])}" if src["needs"] else ""
                print(f"  • Debian's own package, converted by Cygnus (it may not work: it was built for Debian{needs}): Discover in the Cygnus window downloads "
                      f"and checks it; then cygnus install FILE{mark}")
            elif src["kind"] == "fedora":
                needs = f"; Fedora also lists as needed: {', '.join(src['needs'])}" if src["needs"] else ""
                print(f"  • Fedora's own package, converted by Cygnus (it may not work: it was built for Fedora{needs}): Discover in the Cygnus window downloads "
                      f"and checks it; then cygnus install FILE{mark}")
            else:
                print(f"  • The vendor's own package ({' / '.join(src['formats'])}): download it, then cygnus install FILE{mark}")
        return 0
    group = args.group or ""
    if group and group not in discover.GROUP_IDS:
        raise CygnusError("unknown kind of program; choose one of: " + ", ".join(discover.GROUP_IDS))
    page = max(1, args.page)
    result = service.discover_search(" ".join(args.words), group, args.source or "", (page - 1) * discover.PAGE)
    if args.json:
        _emit(result)
        return 0
    for entry in result["entries"]:
        ways = ", ".join(sorted({s["kind"] for s in entry["sources"]}))
        print(f"{entry['key']:<40} {entry['name']}  [{ways}]" + ("  installed" if entry["installed"] else ""))
    pages = -(-result["total"] // discover.PAGE)
    if not result["total"]:
        print("nothing matches")
    elif not result["entries"]:
        print(f"-- there are {result['total']} matches on {pages} {'page' if pages == 1 else 'pages'}; --page {pages} is the last")
    elif result["offset"] + len(result["entries"]) < result["total"]:
        print(f"-- {result['total']} in all; --page {page + 1} shows the next ones")
    return 0


def cmd_permissions(args: argparse.Namespace) -> int:
    """What an installed Flatpak application may do (reads only)."""
    from cygnus.core import flatpak_permissions as fp
    from cygnus.core.backends import flatpak as fb

    result = fp.read(args.app_id, fb.FlatpakEnv(), user_path=fb.user_dir())
    if args.json:
        _emit(result)
        return 0
    print(f"{result['name']} ({result['app']}), from {result['origin'] or 'an unknown source'}")
    summary = result["summary"]
    print(f"  {summary['escape']} that can leave the sandbox, {summary['broad']} broad, {summary['total']} in all"
          + ("; some changed by you" if result["changed_by_you"] else ""))
    marks = {"escape": "!! ", "broad": "!  ", "normal": "   "}
    for group in result["groups"]:
        print(f"\n{group['title']}")
        for item in result["items"]:
            if item["group"] == group["id"]:
                print(f"  {marks[item['level']]}{item['text']}" + ("  [changed by you]" if item["source"] == "you" else ""))
    for line in result["removed"]:
        print(f"\n  {line}")
    for note in result["notes"]:
        print(f"\n  {note}")
    return 0


def cmd_tray(args: argparse.Namespace) -> int:
    from cygnus.core import tray

    if args.sub == "status":
        state = tray.status()
        shown = tray.describe(tray.pending())
        print("update icon: " + ("on" if state["enabled"] else "off"))
        print(f"it would say: {shown['tooltip']}" + ("" if shown["visible"] else " (and not be shown)"))
        if state["why"]:
            print(state["why"])
        return 0
    state = tray.enable() if args.sub == "on" else tray.disable()
    print("done: the update icon is " + ("on" if state["enabled"] else "off") + ("" if args.sub == "off" else " (it starts at every login)"))
    return 0


def _find_browser(spec: str) -> str:
    """The menu entry a person means: its file name ("helium.desktop" or "helium") or the name it shows ("Helium")."""
    from cygnus.core import defaultbrowser as db

    spec = spec.strip()
    for guess in (spec, spec + ".desktop"):
        if db.find_entry(guess):
            return guess  # even one that is no browser: the reason is then given, instead of "not found"
    named = [b for b in db.candidates() if b["name"].casefold() == spec.casefold()]
    if len(named) > 1:
        raise CygnusError(f"more than one web browser is called {spec}: " + ", ".join(b["desktop_id"] for b in named))
    if not named:
        raise CygnusError(f"no web browser called {spec} was found; `cygnus default-browser list` shows the ones there are")
    return named[0]["desktop_id"]


def cmd_default_browser(args: argparse.Namespace) -> int:
    from cygnus.core import defaultbrowser as db

    if args.sub == "list":
        state = db.status()
        found = db.candidates()
        if args.json:
            _emit([{**b, "default": b["desktop_id"] == state["default"]} for b in found])
            return 0
        for b in found:
            tags = [t for t, on in (("default", b["desktop_id"] == state["default"]), ("set up by Cygnus", b["managed"])) if on]
            print(f"{b['name']}  ({b['desktop_id']})" + (f"  [{', '.join(tags)}]" if tags else ""))
        if not found:
            print("no web browser was found")
        return 0
    if args.sub == "status":
        state = db.status()
        if args.json:
            _emit(state)
            return 0 if state["available"] else 1
        if not state["available"]:
            print(state["why"], file=sys.stderr)
            return 1
        name = db.assess(state["default"])["name"] if state["default"] else "nothing"
        print(f"links open with: {name}" + (f" ({state['default']})" if state["default"] else ""))
        if not state["consistent"]:
            for mime, entry in state["current"].items():
                print(f"  {mime}: {entry or 'nothing'}")
        chosen = state["default"] and all(state["explicit"].get(m) == state["default"] for m in state["current"])
        print("chosen on purpose: " + ("yes" if chosen else "no" + (" (it only comes first because nothing else is chosen)" if state["default"] else "")))
        if state["kde"] is not None:
            print(f"KDE's own setting: {state['kde'] or 'none'}")
        print("Cygnus remembers what was set before: " + ("yes" if state["chosen_here"] else "no"))
        return 0
    if args.sub == "undo":
        if not _confirm("Put back the browser settings that were there before Cygnus chose a browser?", args.yes):
            print("cancelled")
            return 1
        state = db.restore()
        for kind in state.get("left_alone", []):
            print(f"  left as it is now (you or a program changed it since): {kind}")
        print("done: " + (f"links open with {state['default']}" if state["default"] else "nothing is chosen any more"))
        return 0
    entry = _find_browser(args.browser)
    verdict = db.assess(entry)
    if verdict["blockers"]:
        print(f"{verdict['name']} cannot be the default browser:", file=sys.stderr)
        for reason in verdict["blockers"]:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    for note in verdict["notes"]:
        print(f"  note: {note}")
    if not _confirm(f"Make {verdict['name']} ({entry}) the browser links open with? What is set now is remembered, "
                    "and `cygnus default-browser undo` puts it back.", args.yes):
        print("cancelled")
        return 1
    state = db.enable(entry)
    print(("done: " if state["changed"] else "already so: ") + f"links open with {verdict['name']}")
    return 0


def cmd_ai(args: argparse.Namespace) -> int:
    """The optional AI assistant's settings. The key is typed here, or piped in, and goes to the wallet: never to a file."""
    import getpass

    from cygnus.gui import assistant

    if args.sub == "status":
        st = assistant.status()
        print(f"assistant: {'on' if st['enabled'] else 'off'}")
        print(f"which: {st['provider']}" + (f" ({st['model']})" if st["model"] else ""))
        if st["provider"] == "hosted":
            print("hosted assistant: " + ("available" if st["hosted_available"] else "not available in this version"))
        else:
            print("key saved: " + ("yes" if st["has_key"] else "no") + ("" if st["wallet_ok"] else f"  (wallet: {st['wallet_why']})"))
        return 0
    if args.sub in ("on", "off"):
        st = assistant.status()
        assistant.configure(args.sub == "on", st["provider"], st["model"], st.get("base_url", ""))
        print(f"the assistant is now {args.sub}" + ("; it reads a program's public pages only when you ask" if args.sub == "on" else ""))
        return 0
    if args.sub == "use":
        st = assistant.status()
        model = args.model if args.model is not None else ("" if args.provider == "hosted" else
                                                          st["model"] if st["provider"] == args.provider else
                                                          st["default_models"][args.provider])
        assistant.configure(st["enabled"], args.provider, model, args.base_url or "")
        print(f"using {args.provider}")
        return 0
    if args.sub == "key":
        if args.action == "clear":
            assistant.clear_key(args.provider)
            print("the saved key was removed")
            return 0
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Key (not shown): ").strip()
        assistant.set_key(args.provider, key)
        print("the key was saved in the wallet")
        return 0
    result = assistant.test(lambda line: print(line, file=sys.stderr))
    print("it works" if result["ok"] else "it answered, but not as expected")
    return 0 if result["ok"] else 1


def cmd_needs(args: argparse.Namespace) -> int:
    """What else does an installed program need? Read-only: it lists checked suggestions, it installs nothing."""
    from cygnus.core.registry import db as regdb
    from cygnus.gui import assistant

    reg = open_registry(args.registry)
    rows = [r for r in regdb.list_installations(reg) if r["name"].lower() == args.app.lower() or r["app_id"] == args.app]
    if not rows:
        print(f"{args.app} is not installed through Cygnus", file=sys.stderr)
        return 1
    if not assistant.status()["enabled"]:
        print("the AI assistant is off: turn it on with `cygnus ai on` (nothing is sent until you do)", file=sys.stderr)
        return 1
    out = assistant.discover(rows[0]["id"], args.url or "", lambda line: print(line, file=sys.stderr), reg)
    if args.json:
        _emit(out)
        return 0
    print(f"{out['app']}: suggestions from {out['provider']}, checked against what its documentation says (the AI can be wrong)")
    if not out["suggestions"]:
        print("  nothing was suggested" + ("; give a documentation page with --url" if out["needs_address"] else ""))
    for s in out["suggestions"]:
        print(f"\n  {s['name']} ({s['relation']}, {s['kind']})" + (f"  [nothing to do: {s['satisfied']}]" if s["satisfied"] else ""))
        print(f"    {s['why']}")
        print(f"    the page says: \u201c{s['quote']}\u201d  ({s['citation_url']})")
        for note in s["notes"]:
            print(f"    note: {note}")
    for why in out["left_out"]:
        print(f"  left out: {why}")
    if out["suggestions"]:
        print("\nTo act on one, open the program in the Cygnus window and press \"What else does this need?\": every step is confirmed there.")
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    from cygnus.core import self_update, updates

    reg = open_registry(args.registry)
    if args.check:
        updates.check_all(reg, lambda line: print(line, file=sys.stderr))
    if args.check:
        try:
            updates.check_system_updates(lambda line: print(line, file=sys.stderr))
        except CygnusError as exc:
            print(f"system updates could not be checked: {exc}", file=sys.stderr)
        own = self_update.check()
        if not own["ok"]:
            print(f"a newer Cygnus could not be looked for: {own['error']}", file=sys.stderr)
    rows = updates.last_known(reg)
    system = updates.last_system_updates()
    cygnus = self_update.last_known()
    available = any(u.status == updates.AVAILABLE for u in rows) or bool(cygnus and cygnus["newer"])
    if args.json:
        _emit({"applications": [u.as_dict() for u in rows], "system": system, "cygnus": cygnus})
        return 1 if available else 0
    if cygnus and cygnus["newer"]:
        print(f"Cygnus {cygnus['latest']} is available (you have {cygnus['current']}): {cygnus['url']}")
        print("  Cygnus does not update itself: get the new version the way you got this one (a package file you downloaded: sudo pacman -U FILE; or through your AUR helper if you installed it from the AUR).")
    elif cygnus:
        print(f"Cygnus {cygnus['current']}: up to date — checked {cygnus['checked_at']}")
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
    _snapshot_note()
    if not _news_acknowledged(args):
        return 1
    return 0 if _helper_commit(client, plan, args.yes) else 1


NEWS_SECONDS = 20.0  # how long `cygnus upgrade` waits for the news before saying it could not read it


def _snapshot_note() -> None:
    from cygnus.core import snapshots
    from cygnus.core.backends import pacman as pm

    try:
        print("\nSnapshot: " + snapshots.situation(pm.read_config().hookdirs)["text"])
    except Exception as exc:  # noqa: BLE001 - a note; it never stops an update
        print(f"\nSnapshot: could not tell whether one is taken ({exc})")


def _news_acknowledged(args: argparse.Namespace) -> bool:
    """Show the Arch news since the last upgrade; when an item says it needs a manual step, go on only if the person says they read it.
    --yes (which answers the password question) never answers this one: --ack-news does, in advance. A feed that cannot be read is
    said so and the upgrade goes on to the usual question."""
    from cygnus.core import news

    report = news.check_within(NEWS_SECONDS)
    if not report["ok"]:
        print(f"\nArch news could not be checked ({report['error']}); read https://archlinux.org/news/ yourself if you can.")
        return True
    if not report["items"]:
        print("\nNo Arch news since your last upgrade.")
        return True
    print(f"\nArch news since your last upgrade ({report['total']}):")
    for item in report["items"]:
        print(f"\n  {'[MANUAL STEP] ' if item['manual'] else ''}{item['title']}")
        for line in item["summary"].splitlines():
            print(f"    {line}")
        if item["link"]:
            print(f"    {item['link']}")
    if report["total"] > len(report["items"]):
        print(f"\n  … and {report['total'] - len(report['items'])} more: https://archlinux.org/news/")
    if report["needs_ack"] and not (args.ack_news or _answer_yes("\nI have read the news marked [MANUAL STEP] and know what to do [y/N] ")):
        print("cancelled: read it first (and do what it says), then run again; --ack-news says you have")
        return False
    return True


def _ago(epoch: float | None) -> str:
    import time

    if not epoch:
        return "never"
    seconds = max(0, time.time() - epoch)
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def _date(epoch: int | None) -> str:
    import time

    return time.strftime("%Y-%m-%d", time.localtime(epoch)) if epoch else "-"


def _size(n: int | None) -> str:
    return "-" if not n else (f"{n / 2**20:.1f} MiB" if n >= 2**20 else f"{n / 1024:.0f} KiB")


def _lists_note(lists: dict) -> None:
    print(f"(package lists refreshed {_ago(lists.get('refreshed'))}; `cygnus upgrade` refreshes them)")


def cmd_packages(args: argparse.Namespace) -> int:
    """Repository packages by name: search, show, list what is installed, install, remove."""
    from cygnus.core import packages

    sub = args.sub
    if sub == "search":
        found = packages.search(" ".join(args.query))
        for p in found["results"]:
            tag = (f"  [installed {p['installed_version']}" + (", update available" if p["update"] else "") + "]") if p["installed_version"] else ""
            print(f"{p['repo']}/{p['name']} {p['version']}{tag}")
            if p["desc"]:
                print(f"    {p['desc']}")
        if not found["results"]:
            print("no package matches")
        elif found["truncated"]:
            print(f"{found['total']} match; the first {len(found['results'])} are shown: search for more words to narrow it down")
        _lists_note(found["lists"])
        return 0
    if sub == "info":
        d = packages.details(args.name, args.repo)
        rows = [("Name", d["name"]), ("Version", d["version"]), ("Repository", d["repo"] or "(none: not offered by a repository)"),
                ("Description", d["desc"]), ("URL", d["url"]), ("Licenses", ", ".join(d["licenses"])),
                ("Groups", ", ".join(d["groups"])), ("Packager", d["packager"]),
                ("Download size", _size(d["package_size"])), ("Installed size", _size(d["installed_size"])),
                ("Built", _date(d["build_date"])),
                ("Installed", f"{d['installed_version']} ({d['reason']}, {_date(d['install_date'])})" if d["installed"] else "no"),
                ("Provides", ", ".join(d["provides"])), ("Conflicts with", ", ".join(d["conflicts"])),
                ("Replaces", ", ".join(d["replaces"]))]
        for key, value in rows:
            if value:
                print(f"{key:<15}: {value}")
        mark = {"installed": "installed", "available": "will be installed", "missing": "NOT AVAILABLE"}
        print("Depends on     : " + (", ".join(f"{x['dep']} ({mark[x['state']]})" for x in d["depends"]) or "nothing")
              + (f" … and {d['depends_more']} more" if d["depends_more"] else ""))
        if d["optdepends"]:
            print("Optional       : " + ", ".join(f"{x['dep']}{' (installed)' if x['installed'] else ''}"
                                                  + (f" - {x['why']}" if x["why"] else "") for x in d["optdepends"]))
        if d["installed"]:
            print("Required by    : " + (", ".join(d["required_by"]) or "nothing")
                  + (f" … and {d['required_by_more']} more" if d["required_by_more"] else ""))
            if d["optional_for"]:
                print("Optional for   : " + ", ".join(d["optional_for"]))
            if d["has_install_script"]:
                print("Install script : yes (pacman runs it as administrator when the package is installed or updated)")
            if args.files:
                for f in d["files"]:
                    print("  " + f)
                if d["files_total"] > len(d["files"]):
                    print(f"  … {d['files_total'] - len(d['files'])} more of {d['files_total']}")
            else:
                print(f"Files          : {d['files_total']} (--files lists them)")
        for kind in ("install", "remove"):
            if d["blocked"][kind]:
                print(f"Cannot {kind} from here: {d['blocked'][kind]}")
        return 0
    if sub == "installed":
        found = packages.installed()
        chosen = [p for p in found["packages"]
                  if (not args.explicit or p["reason"] == "explicit") and (not args.deps or p["reason"] == "dependency")
                  and (not args.unneeded or p.get("unneeded")) and (not args.updates or p["update"])
                  and (not args.foreign or p["repo"] is None)]
        for p in chosen:
            print(f"{p['name']:<40} {p['version']:<24} {p['reason']:<11} {_size(p['size']):>10}"
                  + (f"  -> {p['update_to']}" if p["update"] else "") + ("  (no longer needed)" if p.get("unneeded") else "")
                  + ("  (no repository)" if p["repo"] is None else ""))
        print(f"{len(chosen)} of {len(found['packages'])} installed packages; {found['updates']} have updates")
        _lists_note(found["lists"])
        return 0
    if sub == "orphans":
        found = packages.installed()
        managed = packages.managed_packages()
        left = [p for p in found["packages"] if p.get("unneeded") and not p["protected"] and p["name"] not in managed and not p["odd_name"]]
        odd = [p["name"] for p in found["packages"] if p.get("unneeded") and p["odd_name"] and not p["protected"] and p["name"] not in managed]
        for p in left:
            print(f"{p['name']:<40} {p['version']:<24} {_size(p['size']):>10}")
        print(f"{len(left)} packages were installed for another package and nothing needs them now ({_size(sum(p['size'] for p in left))} in all)."
              + (" `cygnus packages remove --unneeded` removes them." if left else ""))
        if left:
            print("(Look first: a tool you built something with, or use by hand, can show up here. Removing some can leave others that only they needed.)")
        if odd:
            print(f"(Left out, because Cygnus's helper does not take names like these: {', '.join(odd[:10])}. pacman can remove them.)")
        return 0
    if sub in ("cache", "clean"):
        from cygnus.core import cacheclean
        from cygnus.core.backends import pacman as pm

        try:
            shown = cacheclean.preview(pm.read_config(), args.keep, args.uninstalled)
        except cacheclean.CacheRefused as exc:
            raise packages.PackagesError(str(exc)) from None
        for problem in shown["problems"]:
            print(f"left alone: {problem}")
        print(f"{shown['files']} cached files ({_size(shown['bytes'])}) of {shown['packages']} package versions would go ({shown['looked_at']} files looked at), "
              + ("keeping only the installed version of every package" if args.keep == 0 else
                f"keeping the installed version of every package and the {args.keep} newest older one{'' if args.keep == 1 else 's'}")
              + ("; packages that are not installed lose all their cached versions" if args.uninstalled else "") + ".")
        for example in shown["examples"]:
            print(f"  {example}")
        if shown["packages"] > len(shown["examples"]):
            print(f"  … and {shown['packages'] - len(shown['examples'])} more")
        if sub == "cache" or not shown["files"]:
            return 0
        from cygnus.core.privilege import HelperClient

        client = HelperClient()
        return 0 if _helper_commit(client, client.plan_cache_clean(args.keep, args.uninstalled), args.yes) else 1
    if sub == "versions":
        from cygnus.core import pkgcache

        d = packages.details(args.name)
        if not d["installed"]:
            raise packages.PackagesError(f"{args.name} is not installed")
        if d["protected"]:
            print(f"{args.name} is a protected system package: Cygnus never replaces it from a file")
            return 0
        found = pkgcache.older_versions(args.name, d["installed_version"])
        print(f"{args.name} {d['installed_version']} is installed")
        for v in found:
            print(f"  {v.version:<28} {_date(v.modified)}  {_size(v.size):>10}")
        print(f"go back with: cygnus packages downgrade {args.name} --to VERSION" if found else "no older version is in your package cache")
        return 0

    from cygnus.core.privilege import HelperClient

    if sub == "downgrade":
        from cygnus.core import pkgcache

        d = packages.details(args.name)
        if not d["installed"]:
            raise packages.PackagesError(f"{args.name} is not installed")
        version = args.to
        if version is None:
            found = pkgcache.older_versions(args.name, d["installed_version"]) if not d["protected"] else []
            if not found:
                raise pkgcache.DowngradeError("no older version of it is in your package cache (or it is a protected package)")
            version = found[0].version
        checked = pkgcache.check_downgrade(args.name, version)
        client = HelperClient()
        plan, installed = pkgcache.plan_downgrade(client, args.name, version, checked=checked)
        print(f"  {args.name:<36} {installed:<22} → {version}  (older)")
        for p in plan.summary.get("install", []):
            print(f"  {p['name']:<36} {'(new)':<22} → {p['version']}  ({p.get('reason') or 'needed'})")
        print("Other packages that need the newer version make this fail (pacman changes nothing then); the next system update\n"
              "installs the newer version again. The helper does not check a package file's signature: it shows the file's SHA-256, and\n"
              "this file is from pacman's own cache (root's folder, filled by pacman after it checked the signature).")
        return 0 if _helper_commit(client, plan, args.yes) else 1
    client = HelperClient()
    if sub == "install":
        wanted = packages.check_install(args.names)
        try:
            plan = client.plan_packages(install_repo=wanted)
        except CygnusError as exc:
            if packages.refusal(str(exc)) == "update_first":
                print(packages.REFUSAL_TEXT["update_first"] + "\n`cygnus upgrade` does that.")
                return 1
            raise
    else:
        if args.unneeded:
            if args.names:
                raise packages.PackagesError("name the packages to remove, or use --unneeded for everything no longer needed, not both")
            found = packages.installed()
            managed = packages.managed_packages()
            args.names = [p["name"] for p in found["packages"]
                          if p.get("unneeded") and not p["protected"] and p["name"] not in managed and not p["odd_name"]][:packages.MAX_BATCH]
            odd = [p["name"] for p in found["packages"] if p.get("unneeded") and p["odd_name"] and not p["protected"] and p["name"] not in managed]
            if odd:
                print(f"Left out, because Cygnus's helper does not take names like these (pacman can remove them): {', '.join(odd[:10])}")
            if not args.names:
                print("Nothing is left over: no installed package is waiting for something that needs it.")
                return 0
            print("Installed for another package, and nothing needs them now: " + ", ".join(args.names)
                  + "\n(Packages that only these needed show up next time; run it again to remove those too.)")
        elif not args.names:
            raise packages.PackagesError("name the packages to remove, or use --unneeded")
        wanted = packages.check_removal(args.names, limit=packages.MAX_BATCH)
        confirmed = bool(args.not_installed_by_cygnus)
        try:
            plan = client.plan_packages(remove=wanted, confirm_not_installed_by_cygnus=confirmed)
        except CygnusError as exc:
            if confirmed or packages.refusal(str(exc)) != "not_installed_by_cygnus":
                raise
            print(packages.REFUSAL_TEXT["not_installed_by_cygnus"])
            if not _answer_yes(f"Remove {', '.join(wanted)} anyway? [y/N] "):  # --yes never answers this one
                print("cancelled (to answer this in advance: --not-installed-by-cygnus)")
                return 1
            plan = client.plan_packages(remove=wanted, confirm_not_installed_by_cygnus=True)
    s = plan.summary
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
    if paths.is_in_cache(cand.source):
        raise CygnusError("That file is in Cygnus's download folder, which Cygnus clears out. Use `cygnus install` instead: it puts a copy in your applications folder.")
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

    _check_script_flags(args)
    if not Path(args.file).exists() and not args.file.startswith(("/", "./", "~")):
        if getattr(args, "run_scripts", False):
            raise CygnusError("--run-scripts is for programs in archives and for .deb and .rpm files: a Flatpak has no install scripts")
        return _install_flatpak_ref(args)
    cand = detect_file(args.file)
    if getattr(args, "run_scripts", False) and cand.format not in (PackageFormat.ARCHIVE, PackageFormat.DEB, PackageFormat.RPM):
        raise CygnusError("--run-scripts is for programs in archives and for .deb and .rpm files: this kind of file has no install scripts "
                          "Cygnus runs (an Arch package's own install script is always run by pacman)")
    if cand.format is PackageFormat.FLATPAK_BUNDLE:
        return _install_flatpak_bundle(args)
    if cand.format in (PackageFormat.DEB, PackageFormat.RPM):
        return _install_foreign(args)
    if cand.format is PackageFormat.ARCHIVE:
        return _install_archive(args)
    if cand.format is not PackageFormat.APPIMAGE:
        raise CygnusError("`cygnus install` handles AppImages, programs in archives (.tar.gz, .tar.xz ...), .deb and .rpm files, "
                          "Flatpak bundles and Flatpak app ids (e.g. flathub:org.example.App); open other files in the Cygnus app")
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


def _safe(text: object) -> str:
    """A name or message from an archive or a script, with nothing in it that a terminal would obey."""
    from cygnus.core.util import scriptrun

    return scriptrun.printable(str(text).encode("utf-8", "replace"))


def _show_scripts(items: list[dict], texts: dict[str, str], who: str) -> None:
    for i in items:
        print(f"\n--- {_safe(i['name'])} (it would run as {who}) ---")
        print(_safe(texts.get(i["name"], "")))
    print("---")


def _report_scripts(results: list[dict]) -> int:
    """Say how each script went; 2 if any did not finish (the change itself was made)."""
    bad = 0
    for r in results:
        if r.get("ok"):
            print(f"  ran {_safe(r['script'])}: ok")
        else:
            bad = 2
            print(f"  {_safe(r['script'])}: " + ("not run" if not r.get("ran") else "did not finish") + f": {_safe(r.get('error') or 'it did not finish')}")
        if r.get("output"):
            for line in _safe(r["output"]).rstrip().splitlines()[-12:]:
                print("    " + line)
    return bad


def _run_admin_scripts(installation_id: str, chosen: list[dict], registry_path: str | None, assume_yes: bool, *, phase: str = "install") -> bool:
    """Run scripts as administrator through the helper, one confirmation (and one password) for each. False when any of them was
    declined, did not run or did not finish: the helper's own words are printed above."""
    from cygnus.core.privilege import HelperClient
    from cygnus.gui import service

    client = HelperClient()
    done = True
    for c in chosen:
        plan = service.prepare_admin_script(installation_id, c["name"], c["sha256"], client, registry_path, phase=phase)
        done = _helper_commit(client, plan, assume_yes) and done
    return done


def _check_script_flags(args: argparse.Namespace) -> None:
    if getattr(args, "scripts_as_admin", False) and not getattr(args, "run_scripts", False):
        raise CygnusError("--scripts-as-admin only means something together with --run-scripts")


def _install_archive(args: argparse.Namespace) -> int:
    """A program in a plain archive: shown (what is in it, where it goes, what its install scripts would have done), then unpacked."""
    from cygnus.gui import service

    plan = service.analyse(args.file, args.to, args.registry, progress=_say)
    ok = _print_plan(plan)
    for note in plan.get("notes", []):
        print(f"  note: {note}")
    scripts = plan.get("scripts")
    if scripts:
        for m in scripts["mentions"][:8]:
            print(f"  the install scripts mention {m['command']}: {m['what']}" + ("" if args.run_scripts else " (not run unless you pass --run-scripts)"))
    if not ok:
        return 1
    info = plan["archive"]
    main = args.program or info["main"]
    if not main:
        print("Cygnus cannot tell which program to start. Choose one with --program:")
        for choice in info["choices"][:15]:
            print(f"    {choice}")
        return 1
    if main not in info["choices"]:
        raise CygnusError(f"{main!r} is not one of the programs in the archive (choose from: {', '.join(info['choices'][:8])})")
    if not _confirm(f"Unpack {plan['name']} onto {plan['target']} and start {main} from there?", args.yes):
        print("cancelled")
        return 1
    run: list[dict[str, str]] = []
    if args.run_scripts:
        items = [i for i in (scripts or {}).get("items", []) if i["phase"] == "install"]
        who = "administrator" if args.scripts_as_admin else "you"
        if not items:
            print("the archive has no install scripts to run")
        else:
            _show_scripts(items, scripts["texts"], who)
            if not _confirm(f"Run {', '.join(i['name'] for i in items)} as {who} after installing?", args.yes):
                print("cancelled")
                return 1
            run = [{"name": i["name"], "sha256": i["sha256"], "as": "admin" if args.scripts_as_admin else "user"} for i in items]
    result = service.install_archive(args.file, args.to or plan["target"], plan["sha256"], main, _say, args.registry, run_scripts=run)
    if not result["ok"]:
        print(f"not installed: {result['error']}")
        return 1
    print("done")
    status = _report_scripts(result["scripts"])
    if result["admin_scripts"] and not _run_admin_scripts(result["installation_id"], result["admin_scripts"], args.registry, args.yes):
        print("the program is installed; the script you asked to run as administrator did not run or did not finish")
        status = 2
    try:
        libs = service.archive_libraries(result["installation_id"], _say, args.registry)
    except CygnusError as exc:
        print(f"the libraries could not be checked: {exc}")
        return status
    if libs["glibc"]:
        print(f"  warning: built for a newer C library ({libs['glibc']['needs']}) than this computer has ({libs['glibc']['have']})")
    if libs["packages"]:
        print("  it needs libraries that are not installed; your repositories have them in: " + ", ".join(libs["packages"]))
        print("  install them with:  sudo pacman -S --needed --asdeps " + " ".join(libs["packages"]))
    if libs["unresolved"]:
        print("  no repository provides: " + ", ".join(libs["unresolved"][:8]))
    return status


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
    vendor = plan.get("vendor_scripts")
    run_vendor = False
    if args.run_scripts:
        if not vendor:
            print("the package has no install scripts that Cygnus can run")
        else:
            from cygnus.core.util import scriptrun

            print("\nThe vendor's install scripts. If you go on they are put into the converted package, and pacman runs them as "
                  "administrator when it installs and removes it. " + vendor["warning"])
            for name in vendor["names"]:
                print(f"\n--- {name} ---\n" + scriptrun.printable(str(vendor["texts"].get(name, "")).encode("utf-8", "replace")).rstrip())
            for note in vendor["left_out"]:
                print(f"  {note}")
            print("---")
            if not _confirm("Run these scripts as administrator when the package is installed?", args.yes):
                print("cancelled")
                return 1
            run_vendor = True
    if plan["strategy"] != "convert" and not run_vendor:
        if not plan.get("installable_after_review"):
            return 1
        scripts = plan["scripts"]
        print("\nCygnus could not fully read this package's install scripts. Converting does not run them (unless you pass "
              "--run-scripts), so nothing they would do happens. They mention:")
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
                                        optional if args.with_optional else (), accept, run_vendor_scripts=run_vendor)
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

    _check_script_flags(args)
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
    chosen: list[dict[str, str]] = []  # removal scripts to run as you, as steps of the removal
    admin_chosen: list[dict[str, str]] = []  # removal scripts to run as administrator, once the removal has been confirmed
    if args.run_scripts:
        if inst["format"] != "archive":
            raise CygnusError("only programs installed from an archive have removal scripts of their own to run")
        from cygnus.gui import service

        listing = service.archive_scripts(inst["id"], args.registry)
        items = [i for i in listing["scripts"] if i["phase"] == "remove" and i["state"] == "ok"]
        who = "administrator" if args.scripts_as_admin else "you"
        if not items:
            print("this program has no removal scripts to run (or they are no longer as they were)")
        else:
            _show_scripts(items, {i["name"]: i["text"] for i in items}, who)
            if not _confirm(f"Run {', '.join(_safe(i['name']) for i in items)} as {who} when the program is removed?", args.yes):
                print("cancelled")
                return 1
            picked = [{"name": i["name"], "sha256": i["sha256"]} for i in items]
            if args.scripts_as_admin:
                admin_chosen = picked
            else:
                chosen = picked
    steps = plan_uninstall(reg, inst["id"], remove_payload=args.delete_file,
                           remove_adopted=adopted if args.remove_autostart else [],
                           run_scripts=[{**c, "as": "user"} for c in chosen] or None)
    state = {"script_failed": False}

    def before_apply() -> bool:
        if not admin_chosen:
            return True
        if _run_admin_scripts(inst["id"], admin_chosen, args.registry, args.yes, phase="remove"):
            return True
        state["script_failed"] = True  # one policy for every removal script: a script that does not finish does not decide for the person
        return _confirm("A removal script did not run or did not finish. Remove the program anyway?", args.yes)

    def after(report) -> int:
        results = [report.results[i] for i, st in enumerate(steps) if st.kind == "archive.run_script" and i in report.results]
        return max(_report_scripts(results), 2 if state["script_failed"] else 0)

    first = [f"first, as administrator, after you confirm: {_safe(c['name'])}" for c in admin_chosen]
    return _run_steps("uninstall", steps, reg, args.yes, f"Remove {inst['name']}:" + "".join(f"\n  • {line}" for line in first),
                      before_apply=before_apply, after=after)


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
    if inst["format"] == "archive":
        raise CygnusError("moving a program that was unpacked from an archive is not supported yet: remove it and install the archive "
                          "onto the other drive")
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
    if inst["format"] == "archive":
        raise CygnusError("repairing a program that was unpacked from an archive is not supported yet: remove it and install the archive again")
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
    s.add_argument("--ack-news", action="store_true",
                   help="say in advance that you read the Arch news that needs a manual step (--yes never says that)")
    s.set_defaults(func=cmd_upgrade)
    pk = sub.add_parser("packages", help="packages from your repositories, by name: search, show, install, remove").add_subparsers(
        dest="sub", required=True)
    s = pk.add_parser("search", help="find repository packages (every word must appear in the name, description, provides or group)")
    s.add_argument("query", nargs="+")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("info", help="show a package: what it needs, what needs it, its files (changes nothing)")
    s.add_argument("name")
    s.add_argument("--repo", help="the repository, when more than one offers the name")
    s.add_argument("--files", action="store_true", help="list the files of an installed package")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("installed", help="list installed packages")
    s.add_argument("--explicit", action="store_true", help="only those you chose to install")
    s.add_argument("--deps", action="store_true", help="only those installed as another package's dependency")
    s.add_argument("--unneeded", action="store_true", help="only dependencies no installed package needs any more")
    s.add_argument("--updates", action="store_true", help="only those with a newer version in your repositories")
    s.add_argument("--foreign", action="store_true", help="only those no repository offers (from a file or the AUR)")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("versions", help="older versions of an installed package that pacman's cache still holds (changes nothing)")
    s.add_argument("name")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("downgrade", help="go back to an older version from pacman's cache (the newest older one unless --to says)")
    s.add_argument("name")
    s.add_argument("--to", metavar="VERSION", help="the version to go back to, as `versions` lists it")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask before the password prompt")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("install", help="install packages from your repositories (only when the system is up to date)")
    s.add_argument("names", nargs="+")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask before the password prompt")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("orphans", help="packages installed for another package that nothing needs now (changes nothing)")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("cache", help="what cleaning pacman's package cache would remove (changes nothing)")
    s.add_argument("--keep", type=int, default=2, help="older versions to keep besides the installed one (0 to 10, default 2)")
    s.add_argument("--uninstalled", action="store_true", help="packages that are not installed lose all their cached versions")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("clean", help="remove old files from pacman's package cache (asks first; the installed version always stays)")
    s.add_argument("--keep", type=int, default=2, help="older versions to keep besides the installed one (0 to 10, default 2)")
    s.add_argument("--uninstalled", action="store_true", help="packages that are not installed lose all their cached versions")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask before the password prompt")
    s.set_defaults(func=cmd_packages)
    s = pk.add_parser("remove", help="remove packages (never a protected one, never one another package needs)")
    s.add_argument("names", nargs="*")
    s.add_argument("--unneeded", action="store_true", help="remove everything installed for another package that nothing needs now")
    s.add_argument("--not-installed-by-cygnus", action="store_true",
                   help="confirm, in advance, that you want to remove packages Cygnus did not install (-y does not answer that)")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask before the password prompt")
    s.set_defaults(func=cmd_packages)
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
    s = sub.add_parser("permissions", help="what an installed Flatpak application may do: its permissions and your overrides (changes nothing)")
    s.add_argument("app_id", help="the Flatpak application id, for example org.mozilla.firefox")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_permissions)
    tr = sub.add_parser("tray", help="the optional update icon in the system tray").add_subparsers(dest="sub", required=True)
    for name, text in (("status", "is it on, and what would it say (changes nothing)"), ("on", "switch it on and start it"), ("off", "switch it off and remove its start-up entry")):
        tr.add_parser(name, help=text).set_defaults(func=cmd_tray)
    dc = sub.add_parser("discover", help="browse the programs you can get (Flathub, your repositories, vendors' own packages, makers' own downloads)").add_subparsers(
        dest="sub", required=True)
    s = dc.add_parser("search", help="find programs; every word must appear in the name, what it does, its keywords or its maker (changes nothing)")
    s.add_argument("words", nargs="*", help="what to look for (nothing lists everything)")
    s.add_argument("--group", help="only one kind of program (browsers, chat, development, games, graphics, media, office, learn, system, internet, utility, other)")
    s.add_argument("--source", choices=["flatpak", "repo", "vendor", "release", "appimage", "debian", "fedora"], help="only programs that can be got this way")
    s.add_argument("--page", type=int, default=1, help="which page of results (40 to a page)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_discover)
    s = dc.add_parser("info", help="one program and every way to get it (changes nothing)")
    s.add_argument("key", help="the id `cygnus discover search` printed")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_discover)
    s = dc.add_parser("catalogues", help="the online catalogues (AppImageHub...): on or off, how many programs, how old (changes nothing)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_discover)
    s = dc.add_parser("catalogue", help="switch one online catalogue on or off (an off catalogue is not listed and not downloaded)")
    s.add_argument("id", help="the catalogue's id, as `cygnus discover catalogues` prints it")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_discover)
    s = dc.add_parser("update", help="download the online catalogues that are on and missing or more than a week old (uses the network)")
    s.add_argument("--all", action="store_true", help="download every catalogue that is on, even a recent one")
    s.set_defaults(func=cmd_discover)
    db_ = sub.add_parser("default-browser", help="choose which web browser links open with (and put the earlier choice back)").add_subparsers(
        dest="sub", required=True)
    s = db_.add_parser("status", help="what links open with now, and whether that was chosen or just came first (changes nothing)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_default_browser)
    s = db_.add_parser("list", help="the web browsers on this computer (changes nothing)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_default_browser)
    s = db_.add_parser("set", help="make a browser the one links open with; it is read back to check it took, and undone if it did not")
    s.add_argument("browser", help="its menu entry name (helium.desktop) or the name it shows (Helium)")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    s.set_defaults(func=cmd_default_browser)
    s = db_.add_parser("undo", help="put back what was set before Cygnus chose a browser")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    s.set_defaults(func=cmd_default_browser)
    ai = sub.add_parser("ai", help="the optional AI assistant that suggests what a program may still need (off by default)").add_subparsers(
        dest="sub", required=True)
    for name, text in (("status", "what is set"), ("on", "turn it on"), ("off", "turn it off"), ("test", "send one harmless test question")):
        ai.add_parser(name, help=text).set_defaults(func=cmd_ai)
    s = ai.add_parser("use", help="choose which assistant to use")
    s.add_argument("provider", choices=["hosted", "gemini", "compatible"])
    s.add_argument("--model")
    s.add_argument("--base-url", help="for `compatible`: https address, or http on this computer")
    s.set_defaults(func=cmd_ai)
    s = ai.add_parser("key", help="save or remove your key (kept in the wallet)")
    s.add_argument("action", choices=["set", "clear"])
    s.add_argument("provider", choices=["gemini", "compatible"])
    s.set_defaults(func=cmd_ai)
    s = sub.add_parser("needs", help="ask the AI assistant what else an installed program needs (it installs nothing)")
    s.add_argument("app")
    s.add_argument("--url", help="a documentation page (https) to read as well")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_needs)
    s = sub.add_parser("updates", help="show update status (exit 1 if updates are available, a newer Cygnus included)")
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
    s.add_argument("--program", metavar="PATH", default=None,
                   help="programs in archives: the program to start, as a path inside the archive's folder (only needed when Cygnus cannot tell)")
    s.add_argument("--with-optional", action="store_true",
                   help=".deb/.rpm: also install the repository packages that only some parts of the program use")
    s.add_argument("--accept-scripts", action="store_true",
                   help=".deb/.rpm: convert even though Cygnus could not read all of the install scripts (they are not "
                        "run unless you also pass --run-scripts, so what they would have done does not happen); read them first")
    s.add_argument("--run-scripts", action="store_true",
                   help="also run the install scripts that come with the file, after showing them in full (-y does not imply this). "
                        "Archives: the archive's scripts, as you (--scripts-as-admin for administrator). .deb/.rpm: the vendor's "
                        "scripts are put into the converted package and run as administrator by pacman, when it is installed and removed")
    s.add_argument("--scripts-as-admin", action="store_true",
                   help="archives, with --run-scripts: run the scripts as administrator (your password is asked) instead of as you")
    s.set_defaults(func=cmd_install)
    s = sub.add_parser("uninstall", help="remove an application managed by Cygnus")
    s.add_argument("app")
    s.add_argument("--delete-file", action="store_true", help="also delete the application file itself")
    s.add_argument("--remove-autostart", action="store_true", help="also remove autostart entries it had")
    s.add_argument("--run-scripts", action="store_true",
                   help="programs from an archive: first run the removal scripts it carries (prerm, postrm, uninstall), after showing them")
    s.add_argument("--scripts-as-admin", action="store_true", help="with --run-scripts: run them as administrator (your password is asked)")
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
