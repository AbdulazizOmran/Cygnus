"""Qt bridge: exposes cygnus.gui.service to QML. Work runs on a thread pool; results come back as signals."""

from __future__ import annotations

import json
import traceback
import uuid
from typing import Any, Callable

from PySide6.QtCore import Property, QObject, QRunnable, QThreadPool, QUrl, Signal, Slot

from cygnus.core import progress
from cygnus.core.errors import CygnusError
from cygnus.gui import service


class _Relay(QObject):
    done = Signal(str, str)  # request id, JSON
    line = Signal(str, str)  # request id, progress line
    fraction = Signal(str, float, str)  # request id, how far (0..1, only when there is a real count), what it is doing


class _Job(QRunnable):
    def __init__(self, rid: str, fn: Callable[..., Any], relay: _Relay, wants_progress: bool):
        super().__init__()
        self.rid, self.fn, self.relay, self.wants_progress = rid, fn, relay, wants_progress

    def _report(self, text: str) -> None:
        if getattr(text, "log", True):
            self.relay.line.emit(self.rid, str(text))
        fraction = getattr(text, "fraction", None)
        if fraction is not None:
            self.relay.fraction.emit(self.rid, float(fraction), str(text))

    def run(self) -> None:
        try:
            if self.wants_progress:
                result = self.fn(self._report)
            else:
                result = self.fn()
            payload = {"ok": True, "result": result}
        except CygnusError as exc:
            payload = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors instead of crashing the UI
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "trace": traceback.format_exc(limit=5)}
        progress.clear()  # this pool thread will serve another request next
        self.relay.done.emit(self.rid, json.dumps(payload, default=str))


def _path(url_or_path: str) -> str:
    return QUrl(url_or_path).toLocalFile() if url_or_path.startswith("file:") else url_or_path


class Backend(QObject):
    finished = Signal(str, str)  # request id, JSON {"ok", "result"|"error"}
    progress = Signal(str, str)
    progressValue = Signal(str, float, str)  # request id, fraction 0..1, label: only where a real count exists
    appsChanged = Signal()
    storageChanged = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        # Two pools: quick requests (lists, checks) must never wait behind a commit, a build or a download that holds
        # its thread for minutes. Anything that reports progress, or is marked long=True, goes to the long pool.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._long_pool = QThreadPool(self)
        self._long_pool.setMaxThreadCount(3)
        self._relay = _Relay()
        self._relay.done.connect(self._on_done)
        self._relay.line.connect(self.progress)
        self._relay.fraction.connect(self.progressValue)
        self._apps = "[]"
        self._storage = '{"locations": [], "candidates": []}'
        self._internal: dict[str, Callable[[dict], None]] = {}

    def _submit(self, fn: Callable[..., Any], *, wants_progress: bool = False, long: bool = False,
                internal: Callable[[dict], None] | None = None) -> str:
        rid = uuid.uuid4().hex
        if internal is not None:
            self._internal[rid] = internal
        pool = self._long_pool if (wants_progress or long) else self._pool
        pool.start(_Job(rid, fn, self._relay, wants_progress))
        return rid

    def _on_done(self, rid: str, payload: str) -> None:
        handler = self._internal.pop(rid, None)
        if handler is not None:
            handler(json.loads(payload))
        self.finished.emit(rid, payload)

    # -- cached models -----------------------------------------------------------------------------------
    @Property(str, notify=appsChanged)
    def appsJson(self) -> str:
        return self._apps

    @Property(str, notify=storageChanged)
    def storageJson(self) -> str:
        return self._storage

    def _set_apps(self, payload: dict) -> None:
        if payload.get("ok"):
            self._apps = json.dumps(payload["result"])
            self.appsChanged.emit()

    def _set_storage(self, payload: dict) -> None:
        if payload.get("ok"):
            self._storage = json.dumps(payload["result"])
            self.storageChanged.emit()

    @Slot(result=str)
    def refreshApps(self) -> str:
        return self._submit(service.list_apps, internal=self._set_apps)

    @Slot(result=str)
    def refreshStorage(self) -> str:
        return self._submit(service.storage, internal=self._set_storage)

    # -- requests ------------------------------------------------------------------------------------------
    @Slot(str, result=str)
    def checkApp(self, query: str) -> str:
        return self._submit(lambda: service.check_app(query))

    @Slot(str, str, result=str)
    def analyseFile(self, url: str, location: str) -> str:
        return self._submit(lambda progress: service.analyse(_path(url), location or None, progress=progress),
                            wants_progress=True)

    @Slot(str, str, result=str)
    def installFile(self, url: str, location: str) -> str:
        path = _path(url)

        def run(progress):
            result = service.install_appimage(path, location, progress)
            return result

        return self._submit(run, wants_progress=True, internal=lambda p: self.refreshApps())

    @Slot(str, str, result=str)
    def installFlatpakBundle(self, url: str, location: str) -> str:
        path = _path(url)
        return self._submit(lambda progress: service.install_flatpak_bundle(path, location or None, progress),
                            wants_progress=True, internal=lambda p: self.refreshApps())

    @Slot(str, str, result=str)
    def analyseFlatpakRef(self, spec: str, location: str) -> str:
        return self._submit(lambda: service.analyse_flatpak_ref(spec, location or None), long=True)

    @Slot(str, str, result=str)
    def installFlatpakRef(self, spec: str, location: str) -> str:
        return self._submit(lambda progress: service.install_flatpak_ref(spec, location or None, progress),
                            wants_progress=True, internal=lambda p: self.refreshApps())

    @Slot(str, str, str, bool, result=str)
    def convertForeign(self, url: str, expected_sha256: str, optional_json: str, accept_unread_scripts: bool) -> str:
        path = _path(url)
        optional = [str(n) for n in json.loads(optional_json or "[]")]
        return self._submit(lambda progress: service.convert_foreign(path, progress, expected_sha256 or None, optional,
                                                                      accept_unread_scripts),
                            wants_progress=True)

    # `record_json` says what to register once pacman has installed the package(s). That is done by
    # the backend when the plan is committed, not by the page, so it also happens if you leave the page.
    @Slot(str, str, result=str)
    def planBuiltPackage(self, path: str, record_json: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan_built_packages([path], origin="converted",
                                                              record=json.loads(record_json)), long=True)

    @Slot(str, bool, str, result=str)
    def planBuiltPackages(self, paths_json: str, asdeps: bool, record_json: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan_built_packages(json.loads(paths_json), asdeps=asdeps,
                                                              record=json.loads(record_json)), long=True)

    @Slot(str, result=str)
    def planUninstallPackages(self, installation_id: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan_remove_packages(service.package_names(installation_id),
                                                               forget=installation_id), long=True)

    @Slot(result=str)
    def defaultHandler(self) -> str:
        from cygnus.core import handlers

        return self._submit(handlers.status)

    @Slot(bool, result=str)
    def setDefaultHandler(self, enabled: bool) -> str:
        from cygnus.core import handlers

        return self._submit(lambda: handlers.enable() if enabled else handlers.disable())

    @Slot(result=str)
    def interruptedAurChains(self) -> str:
        from cygnus.gui import fixes

        return self._submit(fixes.interrupted_aur_chains)

    @Slot(str, result=str)
    def dismissAurChain(self, target: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.dismiss_aur_chain(target))

    @Slot(str, result=str)
    def aurReview(self, name: str) -> str:
        return self._submit(lambda: service.aur_review(name), long=True)

    @Slot(str, str, str, result=str)
    def aurBuild(self, pkgbase: str, commit: str, names_json: str) -> str:
        names = json.loads(names_json)
        return self._submit(lambda progress: service.aur_build(pkgbase, commit, progress, names=names),
                            wants_progress=True)

    @Slot(str, result=str)
    def planRepoDependencies(self, names_json: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan_repo_dependencies(json.loads(names_json)), long=True)

    # -- the optional AI assistant (the key is kept in the wallet; it is never returned to the page) ---------------------------------
    @Slot(result=str)
    def aiStatus(self) -> str:
        from cygnus.gui import assistant

        return self._submit(assistant.status, long=True)  # the wallet may ask the person to unlock it: never hold a short-job thread

    @Slot(bool, str, str, str, result=str)
    def aiConfigure(self, enabled: bool, provider: str, model: str, base_url: str) -> str:
        from cygnus.gui import assistant

        return self._submit(lambda: assistant.configure(enabled, provider, model, base_url), long=True)

    @Slot(str, str, result=str)
    def aiSetKey(self, provider: str, key: str) -> str:
        from cygnus.gui import assistant

        return self._submit(lambda: assistant.set_key(provider, key), long=True)

    @Slot(str, result=str)
    def aiClearKey(self, provider: str) -> str:
        from cygnus.gui import assistant

        return self._submit(lambda: assistant.clear_key(provider), long=True)

    @Slot(result=str)
    def aiTest(self) -> str:
        from cygnus.gui import assistant

        return self._submit(lambda progress: assistant.test(progress), wants_progress=True)

    @Slot(str, str, result=str)
    def aiDiscover(self, installation_id: str, extra_url: str) -> str:
        """What else does this installed application need? (reads its documentation, asks the AI, checks the answer)"""
        from cygnus.gui import assistant

        return self._submit(lambda progress: assistant.discover(installation_id, extra_url, progress), wants_progress=True)

    @Slot(str, result=str)
    def planSuggestion(self, token: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan_suggestion(token), long=True)

    @Slot(result=str)
    def optionalParts(self) -> str:
        """Cygnus's optional tools and whether each is installed."""
        from cygnus.core import optional_parts

        return self._submit(optional_parts.status)

    @Slot(str, result=str)
    def planOptionalParts(self, packages_json: str) -> str:
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan_optional_parts(json.loads(packages_json)), long=True)

    @Slot(str, result=str)
    def planLocalPackage(self, url: str) -> str:
        from cygnus.gui import fixes

        path = _path(url)
        return self._submit(lambda: fixes.plan_local_package(path), long=True)

    @Slot(str, result=str)
    def adoptAppImage(self, path: str) -> str:
        return self._submit(lambda progress: service.adopt_appimage(path, progress), wants_progress=True,
                            internal=lambda p: self.refreshApps())

    @Slot(str, result=bool)
    def isManaged(self, path: str) -> bool:
        try:
            return service.is_managed(path)
        except Exception:  # noqa: BLE001
            return False

    @Slot(result=str)
    def updateOverview(self) -> str:
        return self._submit(service.update_overview)

    @Slot(result=str)
    def systemUpdateOverview(self) -> str:
        return self._submit(service.system_update_overview)

    @Slot(result=str)
    def planSystemUpgrade(self) -> str:
        from cygnus.gui import fixes

        return self._submit(fixes.plan_system_upgrade, long=True)

    @Slot(result=str)
    def checkUpdates(self) -> str:
        return self._submit(service.check_updates, wants_progress=True)

    @Slot(str, result=str)
    def applyUpdate(self, installation_id: str) -> str:
        return self._submit(lambda progress: service.apply_update(installation_id, progress), wants_progress=True,
                            internal=lambda p: self.refreshApps())

    @Slot(str, result=str)
    def fetchConvertedUpdate(self, installation_id: str) -> str:
        """Download the newer version of a converted program; the page then opens the file for review (nothing is installed)."""
        return self._submit(lambda progress: service.fetch_converted_update(installation_id, progress),
                            wants_progress=True)

    @Slot(result=str)
    def stalePacmanLock(self) -> str:
        return self._submit(service.stale_pacman_lock)

    @Slot(result=str)
    def planClearStaleLock(self) -> str:
        from cygnus.gui import fixes

        return self._submit(fixes.plan_clear_stale_lock)

    @Slot(result=str)
    def interruptedOperations(self) -> str:
        return self._submit(service.interrupted_operations)

    @Slot(str, str, result=str)
    def recover(self, op_id: str, action: str) -> str:
        return self._submit(lambda progress: service.recover(op_id, action, progress), wants_progress=True,
                            internal=lambda p: self.refreshApps())

    @Slot(str, str, result=str)
    def moveApp(self, installation_id: str, location: str) -> str:
        return self._submit(lambda progress: service.move_app(installation_id, location, progress),
                            wants_progress=True, internal=lambda p: self.refreshApps())

    @Slot(str, result=str)
    def diagnoseApp(self, installation_id: str) -> str:
        return self._submit(lambda: service.diagnose_app(installation_id))

    @Slot(str, result=str)
    def repairApp(self, installation_id: str) -> str:
        return self._submit(lambda progress: service.repair_app(installation_id, progress), wants_progress=True,
                            internal=lambda p: self.refreshApps())

    @Slot(str, bool, result=str)
    def uninstall(self, installation_id: str, delete_file: bool) -> str:
        return self._submit(lambda progress: service.uninstall(installation_id, delete_file, progress),
                            wants_progress=True, internal=lambda p: self.refreshApps())

    @Slot(str, str, bool, result=str)
    def addLocation(self, url: str, label: str, make_default: bool) -> str:
        return self._submit(lambda: service.add_location(_path(url), label, make_default),
                            internal=lambda p: self.refreshStorage())

    @Slot(result=str)
    def backgroundChecks(self) -> str:
        return self._submit(service.background_checks)

    @Slot(bool, result=str)
    def setBackgroundChecks(self, enabled: bool) -> str:
        return self._submit(lambda: service.set_background_checks(enabled))

    @Slot(str, str, bool, result=str)
    def dismissComponent(self, app: str, component: str, dismiss: bool) -> str:
        return self._submit(lambda: service.dismiss_component(app, component, dismiss))

    @Slot(str, str, result=str)
    def planFix(self, app: str, component: str) -> str:
        """Ask the helper to plan a component fix; the result carries the helper's own message."""
        from cygnus.gui import fixes

        return self._submit(lambda: fixes.plan(app, component), long=True)  # may download the component's package

    @Slot(str, result=str)
    def commitFix(self, token: str) -> str:
        from cygnus.gui import fixes

        # The registry may have changed (an install recorded, an installation forgotten): refresh the list
        # from here, so it happens even if the page that asked is gone.
        return self._submit(lambda progress: fixes.commit(token, progress), wants_progress=True,
                            internal=lambda p: self.refreshApps())
