# Cygnus — Architecture & Implementation Proposal

*Project name: **Cygnus** (application manager for CachyOS / KDE Plasma). App ID: `io.github.omranabdulaziz.Cygnus`. Distribution: source on GitHub (later), finished builds sold via Gumroad ("buy it or build it yourself").*

Status: **APPROVED 2026-10-06 (rev. 2)** — implemented as a working application (releases up to pkgrel 24 are built as pacman packages); the phase descriptions below are the original plan, kept for the reasoning behind them. Decisions: Flatpak-on-HDD = partially relocated user installation (§7.2); optional Phase 6 features: none for now.
Date: 2026-10-06.
Rev. 2 incorporates an adversarial technical review: 12 claims were re-verified against source code and the live system, and 17 design flaws and spec gaps were fixed (see Appendix B).

---

## 0. Executive summary

Cygnus is a unified application layer **on top of** pacman, the AUR, Flatpak and AppImage. It does not replace them. It adds what they lack:

1. **One application model.** An *Application* consists of *components*: payload, runtime, system service, permission, browser extension, desktop integration and user data. *Features* (keyboard tracking, network stats, …) depend on requirements that can be verified.
2. **A storage abstraction.**
   - The user registers locations ("SSD", "HDD", "External") once.
   - Cygnus probes what each filesystem can *safely* hold.
   - Each component goes in the best location that is still **secure and correct for its package manager**.
   - Whatever must stay on the SSD is shown, with the reason.
3. **A deterministic pipeline: resolve → plan → execute → verify → recover.**
   - It is driven by a catalogue of diagnoses.
   - Every resolution is classified as *automatic*, *needs approval* or *not safely recoverable*.
   - Every change is journaled, so half-finished operations can be resumed or rolled back.
4. **Signed vendor manifests plus curated manifests.** They describe companion components and feature requirements. An optional AI layer may only *draft* manifests; it can never execute anything.
5. **A native KDE UI.**
   - Built with Python, PySide6 and Kirigami, plus KNotifications.
   - Privileged work happens in a small D-Bus helper. The helper computes and executes plans itself, and each kind of operation has its own polkit action.

### Findings on this machine that shape the design

| Finding | Consequence |
|---|---|
| The HDD (`sda1`, 832 GB) is **NTFS** (ntfs3 driver). There is no Linux-native filesystem on it. The live mount options (`uid=1000,gid=1000,acl`, no `nosuid`/`nodev`) are *not* the fstab options: `/mnt/data` and the udisks mount `/run/media/<user>/DATA` share one superblock. | The HDD is treated as a **user-owned** location. It holds user-run payloads: AppImages, portable apps, and the user's Flatpak app/runtime storage. **NTFS is never trusted for root-owned or root-executed content.** Its on-disk Linux ownership can be forged by anyone who edits the disk outside Linux (Windows, or another machine). |
| **pacman breaks symlink relocation.** A sandboxed test with the system pacman (§2.2) showed: a fresh install over `/opt/foo → HDD` fails; an **upgrade silently replaces the symlink with a real directory**, so the app moves back to the SSD and the HDD copy is orphaned; `pacman -Qk` reports a type mismatch. | Cygnus will **not** relocate pacman-owned files using symlinks. Native packages stay on system storage. To put a big app on the HDD, Cygnus's **format advisor** offers a vendor-supported relocatable format (AppImage, Flatpak) and explains the trade-off. |
| **WhatPulse is already broken in ways Cygnus must detect.** The Flatpak 6.3.2 sits on the **EOL** `org.freedesktop.Platform//23.08` runtime and came from a bundle with **no update source**. The AppImage is what actually runs (autostarted from the HDD). The pcap service comes from an unsigned local package and is running. **Your account is not in the `input` group, and `/dev/input/event*` is `root:input 0640` with no ACL.** WhatPulse's udev rule file is present, but keyboard/mouse counting most likely does not work right now. | A ready-made, real acceptance test for diagnosis, health checks and recovery (§21). |
| **Helium is distributed by the vendor as:** a **signed AppImage** (OpenPGP key `BE677C19…378E`; verified against the copy on your HDD), a tarball, a deb, an rpm and a COPR repo. The vendor refuses Flatpak. `helium-browser-bin` installs to `/opt/helium-browser-bin`. | "Helium on HDD" means the signed official AppImage. "Helium as a system package" means it lives on the SSD. The plan screen explains this (§22). |
| `snapper` + `snap-pac` + `limine-snapper-sync` are installed. | Every pacman transaction already gets btrfs pre/post snapshots. These serve as an expert last-resort rollback; package-level rollback stays primary. |
| **Shelly 3.1.6** (an Arch front-end for pacman, the AUR and Flatpak) is installed. It is the current default handler for `.flatpak` files. | Cygnus must coexist with it: wait on the pacman lock, attribute external changes, and never take over file associations silently. |

---

## 1. System inspection results (read-only)

| Item | Value |
|---|---|
| Distribution | CachyOS (rolling, `ID_LIKE=arch`), kernel 7.2.9-1-cachyos, x86_64 (x86-64-v3 repos) |
| Desktop | KDE Plasma **6.7.5** on Wayland; KDE Frameworks **6.30**; Qt **6.11.2** |
| Package manager | pacman 7.x with repos `cachyos-v3`, `cachyos-extra-v3`, `cachyos-core-v3`, `cachyos`, `core`, `extra`, `multilib`. `SigLevel = Required DatabaseOptional`. **`LocalFileSigLevel = Optional`** (unsigned local packages are accepted). `DownloadUser = alpm` (sandboxed downloads). |
| Other front-ends | `paru` (`/etc/paru.conf`: PgpFetch, Devel, Provides). **Shelly 3.1.6**: pacman/AUR/Flatpak GUI with its own polkit policy; default handler for `application/vnd.flatpak`. No PackageKit or Discover. |
| Snapshots | snapper (`root` config) + snap-pac (pre/post on every pacman transaction) + limine-snapper-sync |
| Flatpak | 1.18.4. System installation `/var/lib/flatpak` (repo mode `bare-user-only`), remote `flathub`, hidden bundle remote `whatpulse-origin` (empty URL, `gpg-verify=false`). The user installation `~/.local/share/flatpak` has no apps, but it holds **`overrides/`** (7 browser overrides for Plasma Browser Integration), **`db/`** (portal permission store) and a repo config. |
| Polkit | polkit 127 with polkit-kde-agent. Flatpak's rules let `wheel` install apps and runtimes **without a password**. Bundle installs need `auth_admin_keep`. |
| Privilege tools | pkexec, run0, sudo (password required). You are in `wheel`. |
| SSD | `nvme0n1` 238 GB: btrfs (`@`, `@home`, `@root`, `@srv`, `@cache`, `@log`, `@tmp`), 190 GB free; `/boot` is vfat. |
| HDD | `sda` 1 TB Seagate SATA, rotational. `sda1` NTFS "DATA" 832 GB (example filesystem UUID `<uuid>`, 414 GB free). ntfs3 mounts it at `/mnt/data` and at `/run/media/<user>/DATA`; both share one superblock. Live options are `uid=1000,gid=1000,acl,iocharset=utf8,prealloc`. The fstab line (`nofail,x-systemd.automount,gid=955,umask=022`) is **not** what is in effect; the automount unit is inactive. A second NTFS partition is mounted at another path. |
| GPU | NVIDIA RTX 3050 Ti (open module 615.71.09) + AMD iGPU. The Flatpak extension `GL.nvidia-615-71-09` is installed. |
| Python | 3.14.7. Installed: pyalpm 0.12, PyGObject with the `Flatpak-1.0` typelib, pydantic 2, requests, psutil. The KF6 Python bindings for **KNotifications** and **KCoreAddons** ship in the KF packages but need PySide6, which is in the repos and not installed. |
| Toolchain | cmake, gcc, clang, qmake6; Kirigami 6.30, kirigami-addons 1.15, qqc2-desktop-style |
| Tools present | bsdtar, zsync, ostree, desktop-file-validate, appstreamcli, fakeroot, kbuildsycoca6, update-desktop-database, xdg-mime, pacman-conf |
| Not present | dpkg, rpm, debtap, squashfs-tools, AppImageLauncher, Gear Lever |
| Your current apps | In `/mnt/data/apps/CachyOS/`: `helium-0.18.3.1-x86_64.AppImage` (running), `whatpulse-linux-latest_amd64.AppImage` (running; autostart entry `~/.config/autostart/whatpulse.desktop`), and `whatpulse-linux-latest_amd64.flatpak`. Also: Flatpak `org.whatpulse.WhatPulse` 6.3.2 (installed, not running) and pacman `whatpulse-pcap-service 1.5.1-1` (foreign, unsigned, enabled and active). |

### Integration points

- **Application menu:** KDE reads `.desktop` files from `$XDG_DATA_DIRS/applications` and `~/.local/share/applications`.
  - KSycoca rebuilds lazily: it checks directory mtimes at most every 1.5 s, on the next query.
  - Running `kbuildsycoca6` makes Plasma refresh immediately.
- **Flatpak exports:** `/etc/profile.d/flatpak.sh` and the systemd environment generators add every Flatpak installation's `exports/share` to `XDG_DATA_DIRS`.
  - This includes custom installations. It is verified in flatpak source, and the path is never touched (no stat).
  - New installations appear only **after re-login**.
- **Default applications:** stored in `~/.config/mimeapps.list` through `KApplicationTrader`. The default browser is `x-scheme-handler/http` and `x-scheme-handler/https`.
- **pacman hooks** live in `/usr/share/libalpm/hooks`.

---

## 2. Research findings that drive the design

### 2.1 Method

Every claim was checked in one of three ways:
- **Local inspection** of this machine.
- **Upstream source:** pacman/libalpm (CachyOS tree), flatpak 1.18, ostree, Linux `fs/ntfs3`, the AppImage runtime and tools, AppImageLauncher, go-appimage, Gear Lever, KService/KSycoca, and KDE Discover's Flatpak backend.
- **Official documentation:** freedesktop specs, the Arch wiki, the WhatPulse help centre, downloads and GitHub, and the Helium README and releases.

A second, independent pass re-verified the risky claims (Appendix B).

### 2.2 pacman and relocated directories

Tested empirically in a sandbox: the system pacman, a throw-away `--root`, run under `fakeroot`.

| Scenario | Result |
|---|---|
| Fresh install of a package with `opt/foo/…` while `/opt/foo` is a symlink to `mnt/hdd/foo` | **Fails:** `conflicting files: /opt/foo exists in filesystem` |
| Same, forced past the conflict | The symlink is **replaced by a real directory** |
| **Upgrade** after `/opt/foo` was turned into a symlink | Succeeds, but the **symlink is replaced by a real directory**: the new version lands on the SSD and the old copy is orphaned. **No error.** |
| `pacman -Qk` | `warning: foo: /opt/foo/ (File type mismatch)` |

The cause is in `lib/libalpm/add.c`: a directory entry meeting a symlink is treated as "overwriting file with dir", and extraction uses `ARCHIVE_EXTRACT_UNLINK`.
**Conclusion:** only a *mount* at the payload path is invisible to pacman. Symlinks are not.

### 2.3 libalpm access from Python (verified, with limits)

**What works:**
- pyalpm can run a transaction **dry-run** without root. It uses a temporary `dbpath` whose `local/` and `sync/` are symlinks to `/var/lib/pacman/{local,sync}` (the `checkupdates` technique).
  - Example: `ncdu` resolved to `ncdu 2.9.2-1.1` from `cachyos-extra-v3`, arch `x86_64_v3`.
- Unsatisfied-dependency errors come back structured, e.g. `[('c','notthere>=2',None)]`.

**What doesn't (pyalpm 0.12 with libalpm 16):**
- The question callback is never invoked.
- The event callback reports "not set".
- **`prepare()` segfaults** when converting a package-conflict error.
- pyalpm does not apply `DownloadUser` (the pacman 7 download sandbox).

**Design consequences:**
- pyalpm is used **only for read-only analysis**, always in a **crash-isolated child process**. A crash is classified, and the analysis falls back to `pacman -Sp` text output (`LC_ALL=C`).
- **Commits use the real `pacman` CLI**, which keeps hooks, the download sandbox and all `pacman.conf` semantics. A strict, allow-listed argv builder generates the command line (§10.1).
- Configuration is read with `pacman-conf`, never re-parsed by hand.

### 2.4 Flatpak (source + live system)

- **Custom installations** (`/etc/flatpak/installations.d/*.conf`) are **system** installations.
  - All writes to them go through the root `flatpak-system-helper`; `flatpak_dir_use_system_helper()` is TRUE for every non-user directory.
  - A custom installation is therefore only safe on storage whose *entire path chain* is root-owned and not user-writable.
- **Runtime lookup at run time** (`flatpak_find_deploy_for_ref`) searches the user installation plus *all* system installations. **At install time**, though:
  - a transaction sees only the dependency sources added explicitly;
  - the defaults are system installations only;
  - a system transaction never uses user runtimes;
  - the "unused refs" calculation looks only at its own installation plus the user one.
  - So a runtime shared across installations can be **pruned by `flatpak update`** (CLI, Shelly, Discover) while it is still needed.
  - **Design consequence:** runtimes are installed into the **same installation** as the app. Any deliberate reuse is `flatpak pin`-ned and recorded.
- **End-of-life status** is published per ref (`ostree.endoflife` / `endoflife-rebase`).
  - libflatpak exposes it through `RemoteRef.get_eol()`/`get_eol_rebase()`, `InstalledRef.get_eol()` and the transaction signals `end-of-lifed`/`end-of-lifed-with-rebase`.
  - All of these are verified present in 1.18.4.
  - Freedesktop runtimes get 2 years of support, with a new branch every August, so **23.08 is EOL**. Flatpak on this system already flags it.
- **Error classification** uses `Flatpak.Error` codes (verified): `RUNTIME_NOT_FOUND`, `NEED_NEW_FLATPAK`, `REMOTE_NOT_FOUND`, `ALREADY_INSTALLED`, `NOT_INSTALLED`, `REF_NOT_FOUND`, `OUT_OF_SPACE`, `NOT_AUTHORIZED`, `UNTRUSTED`, `DIFFERENT_REMOTE`, `RUNTIME_USED`, `PERMISSION_DENIED`, …
- **Bundles (`.flatpak`)** can be fully inspected without installing them: ref, runtime, permissions, appstream, `runtime-repo`, and whether an update origin exists.
- **A symlinked user base directory is supported:** flatpak `realpath`s it before binding.

### 2.5 NTFS (ntfs3) as a trust boundary — rejected

From the kernel source:
- ntfs3 stores per-file Linux ownership and mode in WSL-style `$LXUID/$LXGID/$LXMOD` extended attributes.
- Root-created files do get `$LXUID=0`.
- Setuid bits and `security.capability` are honoured when the mount lacks `nosuid` (as `/mnt/data` does today).

But:
1. **Offline forgery:** anyone who can edit the disk outside Linux (Windows dual-boot, another computer) can write those attributes and file contents, including setuid-root binaries.
2. **Possible in-place bypass:** ntfs3's `system.dos_attrib`/`system.ntfs_attrib` handlers have no owner check, which plausibly lets a non-owner flip write bits (not tested).
3. **Unstable options:** the effective mount options depend on which mounter ran first.

**Decision:** NTFS, exFAT and FAT locations are classified **user-owned**, whatever their probe results. They never hold anything root-owned or root-executed.

**Separate recommendation, your choice:** add `nosuid,nodev` to the `/mnt/data` fstab entry. As mounted today, a setuid-root file planted on that disk from outside Linux would be honoured — a local root escalation path that exists independently of Cygnus. AppImages only need exec, so they are unaffected.

### 2.6 AppImage

- **Type 2 structure:** magic `AI\x02` at offset 8; ELF sections `.upd_info`, `.sha256_sig` and `.sig_key`; a squashfs payload after the ELF.
- **Reading without executing:** `unsquashfs -o <offset>` (squashfs-tools ≥ 4.5.1, which fixes CVE-2021-40153 and CVE-2021-41072), extracting only the listed metadata files into an empty directory. Non-squashfs payloads (e.g. DwarFS) are reported as "metadata unavailable".
- **Update information formats:** `zsync|URL`, `gh-releases-zsync|user|repo|tag|glob`, `pling-v1-zsync`.
- **Helium's signature:** a detached OpenPGP signature over the hex sha256 of the file with both signature sections zero-filled.
  - It verified against key `BE677C1989D35EAB2C5F26C9351601AD01D6378E`.
  - That key must be **pinned from the vendor**, never trusted because it is embedded in the file.
- **WhatPulse** AppImages are unsigned: both signature sections are all zeros.

### 2.7 DEB / RPM

- **Vendor scripts:** the examples studied (Chrome, VS Code, Discord, WhatPulse pcap) show maintainer scripts adding apt/yum repos and keys, cron jobs, `update-alternatives` entries, `chmod 4755` sandboxes, and service enabling.
- **Payloads** assume Debian/Fedora library names and paths.
- **Dependency mapping:** `debtap` maps dependencies heuristically. The Arch wiki recommends native or AUR `-bin` packages instead.
- **Soname lookup:** only 531 of 1404 local packages declare soname provides; glibc, gcc-libs, qt6-base, nss, libx11 and mesa do not.
  - Mapping a missing `DT_NEEDED` library therefore uses soname provides first, then a **files database** downloaded into a private temp dbpath.
  - Any heuristic mapping is labelled as such.

### 2.8 Vendor test cases

WhatPulse is covered in §21 and Helium in §22. Their facts come from official sources, cited in Appendix A.

---

## 3. Architecture overview

```
┌──────────────────────────────── user session (unprivileged) ──────────────────────────────────┐
│   cygnus (GUI, Kirigami/QML)         cygnus (CLI)         cygnus-check.timer (user, optional) │
│          └──────────────┬────────────────┴────────────────────────┘                           │
│                         ▼                                                                     │
│  ┌─────────────────────────── cygnus.core (pure Python library) ────────────────────────────┐ │
│  │ Detection │ Registry │ Storage Mgr │ Resolver (deps / runtimes / companions / arch)       │ │
│  │ Planner │ Executor + Op Journal │ Recovery Engine │ Health Engine │ Update Mgr           │ │
│  │ Manifest System (schema, signatures, trust, catalog) │ Discovery (+ optional AI drafts)  │ │
│  │ Desktop Integration │ Service/Permission probes │ Privilege client                       │ │
│  │ Backends: Pacman │ AUR │ LocalPkg │ Flatpak │ AppImage │ Deb │ Rpm │ Portable             │ │
│  │ (pyalpm analysis runs in a crash-isolated child process)                                 │ │
│  └──────────────────────────────────────────────────────────────────────────────────────────┘ │
│        │ user-scope writes (~/.local, user Flatpak, HDD app dirs)        │ D-Bus (system bus) │
└────────┼─────────────────────────────────────────────────────────────────┼────────────────────┘
         ▼                                                                 ▼
   libflatpak ──► flatpak-system-helper (own polkit)        cygnus-helper (root, D-Bus activated,
   systemd --user, kbuildsycoca6, mimeapps                  exits when idle; polkit per verb)
                                                            ├─ computes plans itself → Commit(plan_id)
                                                            ├─ pacman CLI (allow-listed argv; hooks, snap-pac)
                                                            ├─ systemd units of managed packages
                                                            ├─ group membership (allow-listed, caller only)
                                                            ├─ privileged journal + ledger /var/lib/cygnus
                                                            └─ journald audit (structured)
```

### 3.1 Technology choice

**Recommendation: Python 3.14 for the core and the helper, with a PySide6/QML/Kirigami GUI.**

**Why:**
- The hard part of this project is engine logic: resolution, planning, recovery and health, all of which need extensive tests.
- The key native APIs are already available and working here:
  - **pyalpm** for analysis;
  - **libflatpak through GObject introspection**;
  - the `pacman` CLI for commits.
- KDE officially documents Python + Kirigami.
- KF6 ships Python bindings for **KNotifications** and **KCoreAddons**.
- Python 3.14 reads `.pkg.tar.zst` natively (verified: `tarfile` `r:zst`).

**Trade-off:**
- C++/KF6 would be more native and start faster.
- The core has no Qt dependency, so the GUI could be ported later without touching the engine.

**Implementation notes:**
- **D-Bus:** PyGObject `Gio.DBus` on both sides (installed; supports passing file descriptors). Polkit is called directly via `org.freedesktop.PolicyKit1`.
- **Isolated mode:** both processes run Python in **isolated mode** (`python -I`), so user `site-packages`, `.pth` files and `PYTHON*` variables cannot inject code into Cygnus.
- **New runtime dependencies (official repos):** `pyside6`, `python-cryptography` (Ed25519 manifest signatures), `python-systemd` (structured journald audit), `squashfs-tools` (read AppImages without executing them).
- **Dev dependencies:** `python-pytest`, `python-pytest-qt`.

### 3.2 Process boundaries

| Process | Privilege | Responsibilities |
|---|---|---|
| `cygnus` GUI / CLI | user | Analysis, planning previews, user-scope installs (AppImage, portable, user Flatpak, desktop integration), libflatpak operations, UI |
| `cygnus-helper` | root; D-Bus activated, idle exit | Computes and executes privileged plans. A small set of typed verbs (§16.3). Never runs shell strings. Journals privileged steps itself, so it finishes or rolls back even if the GUI dies. Keeps the ledger and the audit trail. |
| `flatpak-system-helper` | root (existing) | Writes to Flatpak system installations, governed by Flatpak's own polkit actions |
| systemd, logind, udisks2 | existing | Unit state; shutdown/sleep **block** inhibitors (held by the helper during commits); drive presence and hot-plug |

The GUI is **never** run as root. No credentials are stored.

---

## 4. Modules

Each module is a package under `cygnus/core/`. Backends implement a common abstract interface:

```
inspect() · resolve() · placement_options() · plan_install/update/remove/move/repair/reinstall()
execute(step) · verify() · diagnose(error) · list_installed() · reconcile()
```

| Module | Responsibility |
|---|---|
| `detect` | Identifies the input type: magic bytes, ELF + AppImage magic, `ar` (deb), RPM lead, zstd/xz tar with `.PKGINFO`, Flatpak bundle header, `.flatpakref`/`.flatpakrepo`, or a package name searched in repos, AUR and Flathub. Hostile inputs are handled safely: no `extractall`, size and member caps, path-traversal checks. |
| `backends/*` | `PacmanBackend`, `AurBackend`, `LocalPkgBackend`, `FlatpakBackend` (remote ref / bundle / flatpakref), `AppImageBackend`, `DebBackend`, `RpmBackend`, `PortableBackend` |
| `registry` | User registry (SQLite/WAL, versioned migrations); reconciliation with the sources of truth |
| `storage` | Location discovery (mountinfo, lsblk, udisks2), stable IDs, capability probes, placement policy, space accounting, offline handling |
| `resolver` | Dependencies (crash-isolated pyalpm dry-run, AUR graph, soname/files mapping), Flatpak runtimes/extensions, companions (manifests), arch/platform/session validation |
| `planner` | Install/update/remove/move/repair/reinstall plans: ordered typed steps, placements, sizes, privileges, remaining issues |
| `executor` | User-side journaled step runner with compensations; delegates privileged steps to the helper as a single plan |
| `recovery` | Diagnosis catalogue, error classifiers, resolution catalogue, safety classes, bounded recovery loop |
| `health` | Feature/requirement model, probe library, health computation |
| `manifest` | Schema, validation, Ed25519 (minisign-format) verification, trust store, catalogue, discovery |
| `discovery` | Discovery without manifests (package metadata, AppStream relations, companion taxonomy, §12.3); optional AI drafting |
| `desktop` | `.desktop` files, icons, MIME, autostart, launcher shims, KDE refresh, default apps, native messaging hosts |
| `services` | systemd system/user unit probes (read-only over D-Bus); changes go through the helper |
| `privilege` | Helper client: plan/commit protocol, fd passing, progress, polkit error mapping |
| `updates` | Per-source update providers |
| `ai` (optional) | Provider abstraction; produces *draft manifests* only |
| `gui/` | Kirigami QML and Python view-models; contains no package-manager logic |
| `helper/` | `cygnus-helper`, polkit policy, D-Bus configuration, ledger, privileged journal |

---

## 5. Data model

Cygnus keeps two stores, deliberately separate.

1. **User registry:** `~/.local/share/cygnus/registry.db`. SQLite in WAL mode, with migrations, and a backup taken before each migration. It holds everything about the user's applications.
2. **System ledger:** `/var/lib/cygnus/ledger.db`, root-owned, `0600` (it holds every user's operations, so it is read only through the helper's per-user filtered `GetLedger`), and written **only** by the helper.
   - **What it records:** packages Cygnus installed; units it enabled; groups it changed; rollback package copies; snapper snapshot numbers; and the **privileged operation journal**.
   - **Why it is separate:** a user-writable database must never be able to tell root what to delete. Privileged removals are authorized against the ledger and the pacman DB, never against the user registry.

UI preferences go in `~/.config/cygnusrc` (KConfig). **No critical state lives in GUI configuration.**

Core tables (abridged):

```
storage_location(id UUID, label, fs_uuid, partuuid, fs_type, subpath, canonical_mount, class{system,posix,user-owned,limited},
                 removable, capabilities JSON, probed_at, probe_boot_id, user_apps_dir, is_default, reserve_bytes, state)
application(id, display_name, vendor, appstream_id, icon, primary_installation_id, manifest_id NULL, trust_level, created_at)
installation(id, application_id, format{pacman,aur,localpkg,flatpak,appimage,deb-converted,rpm-converted,portable},
             source JSON, version, arch, location_id, origin{installed,adopted}, update_provider JSON,
             state{ok,degraded,broken,offline,partial,removed_externally,changed_externally})
component(id, installation_id, kind{payload,runtime,extension,dependency,system_service,user_service,permission,
          browser_extension,native_messaging_host,desktop_integration,codec,driver,config,user_data,companion_app,cli_helper,plugin},
          ref, relation{hard,recommended,optional,unverified}, required_for JSON, placement{location_id,reason},
          installed_by{cygnus,system,vendor,user}, ledger_ref NULL, pinned BOOL)
feature(id, application_id, key, name, required BOOL)
requirement(id, feature_id, component_id NULL, probe JSON, severity{hard,recommended,optional})
artifact(id, installation_id, kind{file,dir,desktop_entry,icon,mime_default,mime_association,autostart,launcher_shim,
         flatpak_override,native_messaging_manifest,pacman_pkg,flatpak_ref,unit_state,group_membership},
         locator, scope{user,system}, sha256 NULL, created_by_op, ownership{created,adopted}, on_uninstall{remove,keep,ask}, refcount)
operation(id, kind, app_id, plan JSON, plan_digest, helper_plan_id NULL, state{planned,running,succeeded,failed,
          rolled_back,needs_attention}, started, finished)
operation_step(op_id, seq, action JSON, state{pending,running,done,failed,compensated,skipped}, compensation JSON, result JSON)
health_result(installation_id, feature_id, status{ok,warn,missing,broken,unknown,likely_failing,offline}, evidence JSON, checked_at)
issue(id, op_id NULL, installation_id NULL, code, severity, facts JSON, resolutions JSON, status, created_at)
recovery_event(id, issue_id, resolution_id, approved_by_user BOOL, outcome, details JSON, at)
manifest(id, app_id, source_url, serial, expires, signer_key_id, trust_level, raw JSON, verified_at)
history(id, installation_id, kind{installed,updated,changed_externally,repaired,moved,...}, details JSON, at)
```

**Ownership rules (enforced in code):**
- Cygnus removes only artefacts it **created**, matched by exact locator and, for files, by sha256; otherwise it asks.
- **Adopted** artefacts (ones that existed before Cygnus) are never removed without explicit per-item confirmation.
- **Nothing is ever selected by name similarity.**
- User-data paths come only from manifests or format conventions (e.g. `~/.var/app/<id>`). They are shown with their sizes and require explicit selection.
- Shared components (runtimes, services, group memberships, native messaging hosts) are reference-counted across applications.

**Reconciliation:**
- The pacman DB, Flatpak installations and the filesystem are the sources of truth.
- Changes made by pacman, paru, Shelly, the flatpak CLI or vendor self-updaters are detected and recorded in the history as **external**. Cygnus never fights them.

---

## 6. Storage model

### 6.1 Locations

**Identity:**
- A location is identified by **filesystem UUID** plus subpath (and subvolume, for btrfs).
- Device names are never stored. PARTUUID and drive model/serial are kept for display only.

**Sources:**
- `/proc/self/mountinfo` is authoritative. Effective mount options are always read from here, never from fstab or from cache.
- `lsblk --json` supplies ROTA, TRAN, RM and HOTPLUG.
- UDisks2 over D-Bus provides removable status and mount/unmount signals.

**Duplicate mounts** of one superblock (`/mnt/data` and `/run/media/<user>/DATA`):
- They are merged into one location.
- The canonical path is the one with an fstab entry, so `/mnt/data` is preferred.

**Automount safety:** before touching any path on an automount or absent location, Cygnus checks that the device exists (`/dev/disk/by-uuid/<uuid>` or udisks) **without touching the path**. This avoids long blocks when the drive is missing.

**First-run setup:**
- Lists suitable candidates in plain language, with free space, SSD/HDD type and "what it can hold".
- Excludes: read-only filesystems, `/boot`, tmpfs, pseudo filesystems, and (unless enabled) network shares.
- You choose a default location.
- Each location gets a user-visible apps folder, defaulting to `<mount>/Applications`. You can point it at the existing `/mnt/data/apps/CachyOS` instead.

### 6.2 Capability probe

**The probe is unprivileged.** It runs in `<loc>/.cygnus-probe-<random>/`, which is removed afterwards. Results are stored with the boot ID and re-checked when mount options change.

| Capability | Test (no file is ever executed) |
|---|---|
| exec allowed | `noexec`/`nosuid`/`nodev` flags in mountinfo, plus `access(X_OK)` after `chmod +x` |
| symlinks, hardlinks | create the link, then `lstat` / `st_nlink` |
| atomic rename, `RENAME_NOREPLACE` | rename tests |
| permission bits persist | `chmod 0644`, then stat |
| case sensitivity | create `a`, test whether `A` exists |
| user xattrs | set/get `user.cygnus.test` |
| `O_TMPFILE`, fsync, flock, shared mmap | direct syscalls |
| filename charset | `:`, `?`, `\` and long names |
| **OSTree compatibility** (Flatpak) | `ostree init --mode=bare-user-only`; commit a tiny tree (symlink, exec bit); hardlink checkout; `ostree fsck` |

**Location classes:**

| Class | Filesystems |
|---|---|
| `system` | the root filesystem and `/home` |
| `posix` | btrfs/ext4/xfs/f2fs (local, Linux-native) |
| `user-owned` | NTFS/exFAT/FAT, or any filesystem whose ownership a user or outside party can forge |
| `limited` | no symlinks or no exec |

**No privileged probe is needed.** NTFS is user-owned by policy (§2.5). For `posix` locations, root-ownership checks are part of store creation (§6.4).

### 6.3 Payload classes and placement policy

| Payload class | Examples | Allowed on |
|---|---|---|
| **U** — user-run, user-owned | AppImage files, portable extracted apps, the user Flatpak installation's `repo/`, `app/`, `runtime/` and `.removed/` | any location with exec. Flatpak additionally needs symlinks, hardlinks and a passing OSTree probe. |
| **S** — system-owned or root-executed | pacman payloads; custom *system* Flatpak installations; anything root executes (scriptlets, system services, setuid helpers, udev `RUN`, polkit exec paths) | `system` storage, or a **verified root-owned store on a `posix` location** (§6.4). **Never `user-owned`.** |
| **I** — integration | `.desktop` files, icons, MIME, autostart, launcher shims, unit files, `/etc` configuration, native messaging manifests, Flatpak `db/`/`overrides/`/`exports/` | SSD (system/home); small |
| **D** — user data | `~/.config/<app>`, `~/.var/app/<id>`, `~/.local/share/<app>` | wherever the app expects it (SSD); never moved implicitly |

The planner places every component and gives a human-readable reason for every fallback. That produces the summary **"Main application: HDD · System integration: SSD · Configuration: SSD"**.

### 6.4 Root-owned stores (only on `posix` locations; not applicable to this HDD)

For S-class payloads on a Linux-native secondary drive, such as a future ext4/btrfs external disk:

1. **Create:** with approval, the helper creates `<mount>/.cygnus-store` (`root:root 0755`). It uses fd-relative operations only (`openat2` with `RESOLVE_NO_SYMLINKS`).
2. **Verify the path chain:** the **whole path chain**, from `/` to the store, must be root-owned and not writable by group or others. The helper refuses FUSE and user-mounted filesystems, and it resolves the location from the fs UUID itself, never trusting a path from the client.
3. **Mount:** the store is mounted at `/var/lib/cygnus/stores/<location-id>` (`nosuid`, `nodev` unless needed) by **one static mount unit per location**, not one per app.
4. **Pre-mount check, on every mount:**
   - open the source without following symlinks;
   - require uid 0 and mode 0755;
   - require `st_dev` to be the expected filesystem;
   - require a root-written marker containing the location UUID.
   On any mismatch, the store is not mounted.
5. **Guard the empty mount point:** it is `chattr +i`, so nothing lands on the SSD while the store is unmounted.

Stores host (a) a custom system Flatpak installation for that location and (b) the optional native-package payload mounts (§7.3).

### 6.5 Offline / removable locations

| Aspect | Behaviour |
|---|---|
| State | `online`, `offline`, or `degraded` (read-only, dirty NTFS, nearly full) |
| Apps on an offline location | shown as **"Unavailable: HDD (DATA) is not connected"**, never as broken |
| Health checks | skipped while offline |
| Updates, moves, uninstalls | queued or blocked, with the reason |
| Launcher shims (§15) | show a KDE notification instead of failing silently |
| Flatpak while the HDD is offline | the HDD part of the user installation is simply missing. No runtime pruning can hit SSD apps, because every installation keeps its own runtimes (§7.2). |

---

## 7. Per-format strategy

| Format | Detection | Install mechanism | Payload location | Update | Uninstall | Relocatable? |
|---|---|---|---|---|---|---|
| **Pacman repo** | name in sync DBs | helper: plan → `pacman` CLI (§10.1) | system | full system upgrade only (no partial upgrades) | `pacman -R` (+ deps Cygnus installed that are now orphaned) | **No** (optional §7.3 only on `posix` stores) |
| **AUR** | AUR RPC v5 | clone → **mandatory PKGBUILD/.install review (diffs on updates)** → `makepkg` as user → helper installs the built package | system | rebuild when the AUR version changes, with diff review | `pacman -R` | as pacman |
| **Local `.pkg.tar.zst`** | zstd tar with `.PKGINFO` | fd-passed to the helper → root-owned staging copy → hash + metadata re-check → `pacman -U` | system | vendor manifest URL, else "manual updates" | `pacman -R` | as pacman |
| **Flatpak (remote / .flatpakref)** | ref / keyfile | libflatpak `FlatpakTransaction` (dry-run via `ready` → plan) | the installation for the chosen location (§7.2) | `add_update` + EOL/rebase checks | `add_uninstall`; unused runtimes ref-counted | **Yes** |
| **Flatpak bundle (.flatpak)** | GVariant header | `add_install_bundle`, after runtime resolution (§9) | same | only if the bundle has an origin URL or a manifest lists newer bundles; else "manual updates" | same | **Yes** |
| **AppImage** | ELF + `AI\x02` | prerequisite checks → verify (pinned-key signature / hash) → copy or move to `<loc>/Applications/<App>/` → integrate | any U-capable location | zsync (`.upd_info`), GitHub releases API (asset digests); vendor self-update detection | remove the file + integration Cygnus created | **Yes** |
| **DEB / RPM** | `ar` / RPM lead | policy engine (§10.3): native alternative → converted local pacman package → portable extraction → refuse with reasons | system (converted) or any U location (portable) | vendor manifest, else none | `pacman -R` / remove the portable folder | converted: no; portable: yes |

### 7.1 AppImage specifics

**Prerequisite checks** (the `APPIMAGE_RUNTIME_UNAVAILABLE` diagnosis, sketched after this list):
- the static type-2 runtime needs only `fusermount3`, or `fusermount` for older runtimes;
- `/dev/fuse` must be accessible;
- the target location must not be mounted `noexec`.

If a check fails, Cygnus offers the vendor's **extract-and-run** mode (`APPIMAGE_EXTRACT_AND_RUN=1`) and explains it.

A minimal sketch of how those checks could map to that diagnosis (illustrative only; the paths are standard, the function itself is hypothetical):

```python
# Sketch: AppImage prerequisite checks behind APPIMAGE_RUNTIME_UNAVAILABLE.
import os, shutil

def _mount_options(path: str) -> set[str]:
    """Per-mount options (field 6) of the longest mountinfo mount point containing path."""
    path = os.path.realpath(path)
    best, opts = "", set()
    with open("/proc/self/mountinfo") as f:
        for line in f:
            fields = line.split()
            mnt = fields[4]
            inside = path == mnt or path.startswith(mnt.rstrip("/") + "/")
            if inside and len(mnt) >= len(best):
                best, opts = mnt, set(fields[5].split(","))
    return opts

def appimage_prereq_problems(appimage_path: str) -> list[str]:
    problems = []
    if not (shutil.which("fusermount3") or shutil.which("fusermount")):
        problems.append("no fusermount3/fusermount helper installed")
    if not os.access("/dev/fuse", os.R_OK | os.W_OK):
        problems.append("/dev/fuse is not accessible")
    if "noexec" in _mount_options(appimage_path):
        problems.append("the AppImage's location is mounted noexec")
    return problems  # non-empty → APPIMAGE_RUNTIME_UNAVAILABLE; offer extract-and-run
```

**Metadata is read without execution:** Cygnus computes the squashfs offset from the ELF header, then extracts only the `.desktop` file, `.DirIcon`, icons and AppStream metadata. It also reads `.upd_info`.

**Placement:**
- One folder per app, e.g. `<loc>/Applications/Helium/helium-0.18.3.1-x86_64.AppImage`.
- The previous version is kept for rollback (default: 1).

**Updates:**
- The new file is downloaded to a temporary file **on the same filesystem**, verified, then atomically `rename()`d into place.
- **Self-updating apps** (the WhatPulse 7 updater replaces its AppImage in place): a hash change is recorded as **"changed outside Cygnus (vendor self-update?)"** in the history. Signed apps are re-verified, and a signature mismatch is ✗.

**Trust levels:**
- *Vendor-signed* (pinned key)
- *Hash-verified* (published digest)
- *Unsigned* (TLS only)

### 7.2 Flatpak placement

| Location | Installation used | Notes |
|---|---|---|
| **SSD** (default) | the existing system installation `/var/lib/flatpak` | unchanged behaviour |
| **HDD on NTFS (this machine)** | **the user installation, partially relocated**: only `repo/`, `app/`, `runtime/` and `.removed/` (which must share one filesystem: checkouts hardlink into the repository, and uninstalling or updating renames a deployment into `.removed/`) live in `<HDD>/.cygnus-user/flatpak/`, symlinked from `~/.local/share/flatpak/` | User-owned: no root process ever touches it. `db/` (portal permissions), `overrides/` (e.g. the existing browser overrides) and `exports/` stay on the SSD. If the HDD is absent, Flatpak errors instead of writing to the SSD. Runtimes for HDD apps are installed **into the user installation** (no cross-installation pruning risk). **Trade-off:** `flatpak run` checks the user installation first, so launching any Flatpak may wake the HDD. |
| **Linux-native secondary drive** | custom **system** installation on a verified root-owned store (§6.4) | `installations.d/cygnus-<id>.conf`. Exports appear in menus after re-login. Until then Cygnus shows a "log out to see new menu entries" notice. |

**Moving a Flatpak app:**
1. Install the same ref into the target installation. Origin-less bundles are re-exported first with `flatpak build-bundle` from the local repo.
2. Verify the new install.
3. Uninstall from the source installation, removing runtimes the source no longer needs.

`~/.var/app/<id>` (user data) is untouched throughout.

### 7.3 Native packages and large payloads (optional phase; `posix` stores only)

pacman cannot follow relocated paths (§2.2), so the only pacman-transparent mechanism is a **mount** at the payload directory. An opt-in later phase adds **managed payload mounts**, under these conditions:

- **Eligibility:** a directory is eligible only if *exclusively* owned by one package (`pacman -Qo` over the tree), larger than a threshold, containing nothing root executes, and on a verified root-owned `posix` store.
- **Mechanism:** the payload is bind-mounted at its package path through **static** mount units, generated when you enable the feature (not at boot from a database).
- **Safety:** a pacman `PreTransaction` hook with `AbortOnFail` refuses any transaction that touches such a path while its mount is inactive. Its message names the drive and the override.
- **Caveat:** snapper rollbacks of `/` do not cover these payloads, and Cygnus warns about this.

**On this machine it does not apply**, because the HDD is NTFS.

The primary answer to "put this big app on the HDD" remains the **format advisor**.

---

## 8. Resolution pipeline

```
Request ─► Detect ─► Identify app (AppStream id, manifest) ─► Resolve dependencies ─► Resolve runtimes/extensions
        ─► Resolve companions (manifest + discovery) ─► Validate arch/platform/session ─► Validate storage/placement
        ─► Staleness checks (EOL runtime, outdated bundle/vendor package, out-of-date AUR, stale sync DB)
        ─► Plan (+ remaining issues with resolutions) ─► User confirmation
        ─► Execute (journaled) ─► Verify (backend verify + health probes) ─► SUCCESS
                                      └─► on failure: classify ─► diagnose ─► resolutions ─► present ─► apply approved ─► verify again
```

Each stage emits **facts** and **issues**. An `Issue` carries a stable code, severity, facts and evidence, plus one or more `Resolution`s. A resolution consists of typed actions (§13.4), the privilege it needs, and a **safety class**:

| Class | Meaning |
|---|---|
| `AUTO` | **Unprivileged, reversible, user-scope only, trusted source, no security downgrade.** Examples: regenerating a Cygnus-created desktop entry; re-downloading a hash-verified AppImage. Shown and logged; no prompt. |
| `APPROVAL` | Everything else that is safe. **Every helper verb is APPROVAL**, as is any EOL or unsigned component, any permission change and any removal. |
| `NONE` | Not safely recoverable. Cygnus gives a precise explanation and lists the legitimate options. |

**Bounds on recovery:**
- Selection is deterministic, by a fixed policy order: vendor-supported > non-EOL > signed > fewer privileges > smaller change.
- Each resolution is applied at most once per operation.
- There are at most two recovery rounds, then Cygnus stops and reports.

### 8.1 Diagnosis catalogue (initial)

**Native packages**

| Code | Detected by | Resolution (class) |
|---|---|---|
| `PKG_DEP_MISSING` | dry-run unsatisfied deps | add a repo provider (APPROVAL, within the plan) / AUR (APPROVAL + review) |
| `PKG_DEP_UNRESOLVABLE` | no provider | NONE; explain |
| `PKG_CONFLICT` | conflict detected (crash-isolated dry-run / `pacman -Sp`) | explicit replacement plan listing both packages (APPROVAL) / cancel |
| `PKG_SYNC_STALE` | package version not on mirrors (404) / sync DB older than the local packages | full upgrade + targets (APPROVAL); **never `-Sy` alone** |
| `PKG_PARTIAL_UPGRADE_RISK` | the plan would upgrade installed packages outside a full `-Syu` | full upgrade + targets (APPROVAL) |
| `PKG_REPO_DISABLED` / `PKG_REPO_MISSING` | package in a known but unconfigured repo (manifest knowledge) | explain; enabling a repo is APPROVAL and manifest-driven only |
| `PKG_SIG_INVALID` / `PKG_SIG_UNKNOWN_KEY` | signature errors | NONE / explain (no automatic key import) |
| `PKG_UNSIGNED_LOCAL` | local package without a signature | only with a vendor-verified hash (APPROVAL, trust label) |
| `PKG_DB_LOCKED` | `db.lck` present | if another front-end (pacman, paru, Shelly) is running: wait, showing which one. If stale (no libalpm user, lock predates boot): remove it (APPROVAL) |
| `PKG_INCOMPLETE` | `pacman -Qk` missing files / interrupted step | reinstall the same version (APPROVAL) |
| `VERSION_UNAVAILABLE` | the requested version or commit is not on the mirror / remote / vendor | offer the nearest available version (APPROVAL) / NONE |
| `VENDOR_PKG_STALE` | local or converted package older than the vendor's latest (manifest) | update from the vendor artefact (APPROVAL) |
| `AUR_OUT_OF_DATE` / `AUR_ORPHANED` / `AUR_BUILD_FAILED` | RPC fields / makepkg exit code + log classifier | warn; the classifier maps causes (missing makedeps, checksum mismatch, missing PGP key) to specific fixes |

**Flatpak**

| Code | Detected by | Resolution (class) |
|---|---|---|
| `FP_RUNTIME_MISSING` / `FP_RUNTIME_UNAVAILABLE` / `FP_RUNTIME_EOL` / `FP_RUNTIME_EOL_REBASE` | metadata + remote refs + EOL fields | the §9 algorithm |
| `FP_EXTENSION_MISSING` (incl. a GL driver mismatch) | host NVIDIA version vs installed `GL.nvidia-*` | install the matching extension (APPROVAL; reversible) |
| `FP_REMOTE_MISSING` / `FP_REMOTE_DISABLED` | bundle `runtime-repo`, `RuntimeRepo` | add the vendor-declared `.flatpakrepo`, showing its URL and key (APPROVAL) |
| `FP_BUNDLE_NO_UPDATE_SOURCE` | empty origin URL | inform: "manual updates"; check the manifest for newer builds |
| `FP_PERMISSION_MISSING` | the manifest requires a permission or portal grant that the app's metadata, overrides or portal store lack | `flatpak.override` (APPROVAL, shows the exact permission) / portal grant instructions |
| `FP_RUNTIME_PRUNE_RISK` | a runtime needed by an app in another installation is not pinned | `flatpak pin` (APPROVAL) |

**Platform, libraries and AppImage**

| Code | Detected by | Resolution (class) |
|---|---|---|
| `ARCH_INCOMPATIBLE` (all formats: `.PKGINFO` arch / CPU v3-v4 level; AppImage ELF machine; Flatpak arch; deb/rpm `Architecture`) | header comparison | a compatible build from the manifest or another source / NONE |
| `LIB_SONAME_MISSING` | ELF `DT_NEEDED` cannot be satisfied | map to a package via soname provides or the files DB (APPROVAL; labelled heuristic when it is one) / NONE |
| `GLIBC_TOO_OLD` | binary needs a newer `GLIBC_2.x` than the host has | NONE; explain |
| `APPIMAGE_RUNTIME_UNAVAILABLE` | FUSE/`fusermount`/`noexec` checks | fix the cause (APPROVAL) / extract-and-run mode (AUTO) |

**Components, permissions and integration**

| Code | Detected by | Resolution (class) |
|---|---|---|
| `SVC_MISSING` / `SVC_DISABLED` / `SVC_FAILED` | systemd D-Bus | install / enable+start / journal excerpt + restart (all APPROVAL) |
| `COMPANION_MISSING` | a manifest companion app/helper/plugin is absent | install via its declared source (APPROVAL) |
| `POSTINSTALL_FAILED` | a post-install step failed (unit enable, group, integration, trigger) | retry that step / roll back the operation (APPROVAL) |
| `PERM_GROUP_MISSING` / `PERM_RELOGIN_REQUIRED` | `getent` vs session groups (`/proc/<pid>/status`) | add to the group (APPROVAL, with a security note) → "log out required" |
| `BROWSER_EXT_MISSING` | profile scans | open the store page (user action) |
| `NMH_MISSING` / `NMH_BROKEN` | native messaging manifest absent, or its `path` missing (incl. Flatpak browser paths `~/.var/app/<browser>/…`) | reinstall the manifest (AUTO if Cygnus created it) / explain the Flatpak browser limitation |
| `CODEC_MISSING` / `DRIVER_MISSING` | manifest probes (GStreamer registry, `ffmpeg -decoders`, `/sys/module`) | suggest packages (APPROVAL) |
| `DESKTOP_ENTRY_BROKEN` | Exec target missing / invalid file | regenerate (AUTO for Cygnus-created) |
| `DESKTOP_ID_SHADOWED` | a user-level desktop ID hides a system one (or the reverse) | choose which one wins (APPROVAL) |
| `MIME_REGISTRATION_BROKEN` / `DEFAULT_APP_LOST` | `mimeapps.list` points to a missing or removed desktop ID; a handler is not registered | restore / offer alternatives (APPROVAL) |

**Storage, operations and coexistence**

| Code | Detected by | Resolution (class) |
|---|---|---|
| `STORAGE_OFFLINE` / `STORAGE_FULL` / `STORAGE_INCAPABLE` | storage manager | connect the drive / choose another location / fallback placement (explained) |
| `OP_INCOMPLETE` | journal step left `running` | resume / roll back (APPROVAL) |
| `DUPLICATE_INSTALL` / `PORT_CONFLICT` | the same app in two formats; both bind the same port | "keep one" (APPROVAL; never automatic) |
| `UNKNOWN_FAILURE` | unclassified errors, child crash | raw output preserved; no guessing |

**Error classifiers** map raw failures to these codes:
- pacman: exit code + `LC_ALL=C` messages
- libflatpak: `Flatpak.Error` codes
- makepkg: exit codes + log regexes
- HTTP: status codes

---

## 9. Flatpak runtime resolution (and stale runtimes)

For any Flatpak input, **before** installing:

1. **Read the declared metadata:** app ID, branch, arch, `runtime=`, `sdk=`, extensions, `[Context]` permissions, the bundle's `runtime-repo` and origin.
2. **Is the runtime installed** in the target installation, or in another installation with a pin? Check its EOL flag (`InstalledRef.get_eol()`).
3. **If not, query the configured remotes** for that exact ref: is it available? EOL? Does it have an `eol-rebase`?
4. **If the remote is missing**, use the declared `RuntimeRepo` / `runtime-repo`, but only after showing the user its URL and key (`FP_REMOTE_MISSING`, APPROVAL).
5. **Is the app bundle stale?** Ask the vendor manifest for newer builds and their runtimes. For apps from a remote, check the current commit's runtime.
6. **If a newer app build uses a supported runtime, prefer it.** It appears in the plan as the recommended option.
7. **If only the EOL runtime is available**, offer it with an explicit warning (APPROVAL): *"org.freedesktop.Platform 23.08 no longer receives security fixes."* An `eol-rebase` target is shown for information only.
8. **If the runtime is unavailable**, the outcome is `NONE`, with an explanation and the alternatives: other vendor formats from the manifest, or contacting the vendor.
9. **Never substitute a different runtime branch.** The runtime ABI is per branch, and Flatpak cannot override an app's runtime without a rebuild.
10. **Resolve extensions and GL drivers.** On this system, host NVIDIA 615.71.09 must match `org.freedesktop.Platform.GL.nvidia-615-71-09`. A health probe re-checks this after driver updates.

Library transactions call `add_default_dependency_sources()` explicitly, which the CLI does but the library does not by default.

**After install**, the health engine keeps watching:
- an app whose runtime becomes EOL turns ⚠ "Runtime no longer supported";
- every update check looks for a newer build.

---

## 10. Native package recovery

### 10.1 Pacman / repository packages

**1. Analysis (unprivileged, crash-isolated child process)**
- A pyalpm dry-run produces the add/remove sets, replacements and missing dependencies.
- If the child crashes (e.g. the pyalpm conflict bug), Cygnus falls back to `LC_ALL=C pacman -Sp --print-format …`, classifying from its text.

**2. Plan model and database snapshot binding**
- **Fast path:** the system sync DBs are current, every needed package is downloadable, and the plan upgrades nothing that is already installed. The plan is then just `-S targets`.
- **Otherwise:** the plan is **full upgrade + targets**, computed against freshly downloaded sync DBs in a private temp dbpath.
- The plan pins those DB files by sha256. The helper:
  1. verifies the signatures where the repos sign their DBs;
  2. installs exactly those DB files atomically;
  3. recomputes the plan.
- If the recomputed plan differs, the result is a **re-plan with the differences shown**, never a silent change.
- Cygnus never runs `-Sy` without `-u`.

**3. Execution (helper)**
- The helper **computes the plan itself**, returns a summary plus a single-use `plan_id`, and executes only `Commit(plan_id)`. Details in §16.
- Execution uses the `pacman` CLI with an allow-listed argv: `--noconfirm`, `--needed` for repository installs (never for `pacman -U`: a package file you chose is always installed, even when that version is already there), `--asdeps` where planned, and `--` before targets. This keeps the `DownloadUser` sandbox, hooks and snap-pac snapshots, whose numbers are recorded.
- Allowed flags are a fixed set. `--nodeps`, `--dbonly`, `--noscriptlet`, `--overwrite`, `--assume-installed` and `--ask` are **never** used.
- **Conflict replacement** happens only when the approved plan contains exactly that conflict set. The exact mechanism is validated in Phase 2 sandbox tests. If it cannot be done without broad flags, the conflict is presented as a separate, explicit removal step that you approve.
- A **protected set** can never be removed or replaced through Cygnus: `base`, `glibc`, `systemd`, `pacman`, kernels, bootloader and initramfs tooling, `sudo`/`polkit`.

**4. Verification**
- `pacman -Qi`/`-Qk` on each target
- a soname check of the installed binaries
- desktop entry and health probes

### 10.2 AUR

**Resolution:**
- RPC v5 info/multi-info builds the dependency graph: `Depends`/`MakeDepends`/`CheckDepends`, with split packages handled via `PackageBase`.
- Repo dependencies and makedepends fold into the same §10.1 plan, including the `-Syu` rule.
- Out-of-date and orphaned packages are flagged.

**Review (mandatory):**
- Cygnus shows the PKGBUILD, `.install` and source URLs.
- On updates it shows a **diff** against the last version you approved, stored in the registry.
- "Skip review" is off by default and labelled as a security trade-off.

**Build:**
- Runs as you, in `~/.cache/cygnus/aur/<pkgbase>`.
- `validpgpkeys` are fetched only with approval.
- Clean-chroot builds are out of scope for v1, because they need root. A narrowly scoped verb could be added later.

**Install:**
- Built packages go to the helper by fd → `auth_admin` prompt that says *"locally built, unsigned, runs code as root"*.
- Makedepends that Cygnus added are removed afterwards.

### 10.3 DEB / RPM policy engine

**1. Identify**
- `.deb`: control fields via `bsdtar`.
- `.rpm`: header tags, including soname requires such as `libc.so.6(GLIBC_2.34)(64bit)`, and scriptlets, via a header parser.

**2. Prefer native**
Check the repos/AUR (same name, `-bin`, AppStream ID), Flathub (API v2) and vendor manifests. Example: the WhatPulse pcap `.deb`/`.rpm` → the vendor's own `.pkg.tar.zst`.

**3. Analyse the payload**
- layout: self-contained `/opt/<vendor>` vs distro-integrated;
- ELF `DT_NEEDED` + RPATH/RUNPATH → bundled libs, host libs, soname provides, files DB;
- maximum `GLIBC_2.x` vs host;
- architecture.

**4. Classify maintainer scripts by known pattern:**

| Pattern | Treatment |
|---|---|
| `ldconfig`, cache updates | pacman hooks → drop |
| `chmod 4755 …/chrome-sandbox` | noted; dropped if unprivileged user namespaces are available, else NONE |
| `systemctl enable` | approved unit action |
| `useradd` / `adduser` | `sysusers.d` |
| `update-alternatives` | drop or package a symlink |
| apt/yum repos + keys, cron updaters | **drop and report** |
| anything unrecognised | **blocks** automatic conversion and is shown to the user |

**5. Decide**

| Outcome | When / how |
|---|---|
| **Convert** | Build a local pacman package: Arch dependency names, unsafe files dropped, provenance recorded. pacman then owns every file. Installed with `auth_admin`. |
| **Portable extraction** | To a U location — only for self-contained payloads that need nothing root-owned. |
| **Refuse** | With exact reasons: unresolvable sonames, glibc too new, kernel modules/DKMS, debconf-driven configuration, multiarch-only paths, conflicts with files owned by other packages. |

Cygnus never copies files into system directories outside a package.

---

## 11. Operations journal, rollback, recovery

**User-side journal** (in the registry). Every plan becomes an operation with ordered, **idempotent** steps. Each step has:
- an intent, recorded *before* it runs;
- a result, recorded *after*;
- a **compensation**, for example remove a created file or restore a backup.

**File changes:** written to a temp file on the same filesystem → `fsync` → `rename`. Backups are taken before any overwrite.

**Privileged journal** (in the ledger, owned by the helper):
- The helper journals its own steps and holds a logind **`block`** inhibitor for shutdown and sleep while committing. (A `delay` inhibitor is capped at about 5 s and is not enough.)
- It **ignores client disconnects**: a pacman commit always runs to completion or failure. The GUI only mirrors the helper's state.

**Rollback material:**
- Before upgrades, removals or replacements, the helper copies the current versions' package files into `/var/cache/cygnus/rollback/`, so `paccache` cannot remove them.
- Downgrading after a full `-Syu` is offered **only** as APPROVAL with warnings, because it is not generally safe on Arch. In that situation the snapper snapshot pair is the honest alternative.

**Failure mid-way:**
1. Stop.
2. Mark the operation `failed`.
3. Diagnose.
4. Offer one of three choices: **resolve and continue**; **roll back** (compensations in reverse order, each verified); or **leave as is**, in which case the state is recorded and an `OP_INCOMPLETE` issue stays visible.

**Crash or power loss:** on next start, operations still marked `running`, in either journal, produce **Resume / Roll back / Inspect**.

**pacman specifics:**
- An interrupted transaction is checked with `pacman -Dk`, `pacman -Qk <targets>` and the `db.lck` state.
- The snap-pac pre/post snapshot numbers are offered as an **expert last resort**, with a clear warning that `snapper undochange` reverts *every* file changed in that window.

**Flatpak specifics:** deploys are atomic per ref. Rollback is `flatpak update --commit=<previous>`, or uninstalling the newly added refs.

**Recording:** every recovery attempt is stored in `recovery_event` and shown in the app's History.

---

## 12. Features, requirements, health, repair

### 12.1 Model

| Concept | Definition |
|---|---|
| **Application** | Has **Features**. |
| **Feature** | Has **Requirements**. |
| **Requirement** | A probe plus a severity (`hard` / `recommended` / `optional`). |

Component relations: **HARD DEPENDENCY**, **RECOMMENDED COMPONENT**, **OPTIONAL INTEGRATION**, **THIRD-PARTY / UNVERIFIED**.

Package dependencies are a separate layer. A dependency is needed to install or run; a requirement is needed for one capability.

**Health states:**

| State | Meaning |
|---|---|
| ✓ Fully functional | everything checks out |
| ⚠ Some optional functionality unavailable | an optional requirement fails |
| ⚠ Missing required component | a feature's hard requirement fails |
| ✗ Broken installation | the core feature fails, or the payload is missing or corrupt |
| ? Likely failing / unknown | the evidence is indirect, e.g. the process lacks the `input` group; shown with that evidence |
| ⏏ Unavailable | storage offline (never counted as broken) |

### 12.2 Probe library

The probes are deterministic and almost all unprivileged. Each returns a status plus evidence.

| Area | Probes |
|---|---|
| Files & desktop | `file_exists`, `executable_present`, `desktop_entry_valid` (`desktop-file-validate` + Exec target), `desktop_id_unique` |
| pacman | `pacman_installed(name, version_range)`, `pacman_files_intact` |
| Flatpak | `flatpak_installed`, `flatpak_runtime_available`, `flatpak_runtime_eol`, `flatpak_runtime_pinned`, `flatpak_gl_driver_match`, `flatpak_permission(app, perm)` |
| systemd | `systemd_unit(name, scope, expect)`, `journal_recent_match(unit, regex, within)` |
| Permissions & IPC | `group_membership(group, effective: configured\|session)`, `tcp_listener(addr, port, owner_exe)`, `tcp_peer`, `dbus_name_owner` |
| Browser | `browser_extension_present(family, id)`, `native_messaging_host(browser, name)` |
| Kernel & system | `sysctl`, `kernel_module`, `file_capability` |
| Runtime environment | `soname_resolvable`, `process_running`, `autostart_entry_valid`, `mime_default(type, desktop_id)`, `appimage_prereqs`, `storage_online` |

Probe definitions live in **manifests**. The engine has no app-specific code.

### 12.3 Companion discovery taxonomy

Discovery uses the signals below. A manifest takes priority over all of them.

| Class | Detection signals (from package contents, metadata or a manifest) |
|---|---|
| System service | `usr/lib/systemd/system/*.service` in package or payload; manifest |
| User service | `usr/lib/systemd/user/*`; autostart entries; Background portal |
| udev rules / device permissions | `usr/lib/udev/rules.d/*`; vendor scripts (parsed, never run); `/dev` access patterns |
| Groups / users | `sysusers.d`; scriptlet `useradd`/`gpasswd`; docs (AI-drafted, unverified) |
| Polkit rules / actions | `usr/share/polkit-1/*` |
| D-Bus services | `usr/share/dbus-1/{system,services}/*` |
| Kernel modules / DKMS | `modules-load.d`, `dkms.conf`, `*.ko` |
| File capabilities | `setcap` in scriptlets / docs; `getcap` on payload |
| Browser extensions | manifest only (store IDs) |
| Native messaging hosts | `NativeMessagingHosts/*.json`, `native-messaging-hosts/*.json` in payload; manifest |
| CLI helpers / plugins / codecs / drivers | `optdepends`; AppStream `<requires>`/`<recommends>`; GL extension rules |
| Runtimes | Flatpak metadata; ELF interpreter / library needs |

### 12.4 Repair and Reinstall

**Repair** fixes what is broken while preserving user data and settings. It always shows a plan first.

| Format | Repair | Reinstall |
|---|---|---|
| pacman / AUR / local | verify (`-Qk`) → reinstall the **same** version (cache → mirror → rebuild for AUR) | same, unconditionally |
| Flatpak | `flatpak repair` (that installation) → reinstall the ref if still broken | uninstall + install the same ref/commit (data in `~/.var/app` kept) |
| AppImage / portable | re-verify the hash or signature → re-download the same version if it fails | re-download / re-extract the same version |
| All formats | re-create Cygnus-owned integration (desktop entry, icons, shim, MIME registrations, autostart, native messaging manifest), re-enable required services, re-check permissions, then the full health check | same |

---

## 13. Vendor manifest system

### 13.1 Standards surveyed and what is reused

| Standard | Reused | Gap it leaves |
|---|---|---|
| **AppStream MetaInfo** | component IDs, `developer id`, `<provides>`, `requires/recommends/supports` (control, hardware, internet, modalias, kernel), `<releases>`, `<custom>` | no system services, groups, capabilities, browser extensions, typed install actions or feature→requirement mapping |
| **Debian** Depends/Recommends/Suggests | hard / recommended / optional semantics | package level only |
| **winget** | dependency categories, external dependencies, elevation requirement | Windows-specific |
| **Homebrew Cask** | `uninstall` vs `zap` split | macOS-specific |
| **Flatpak** metadata | runtime / extension / permission vocabulary | per-app sandbox only |
| **Snap interfaces** | named, typed permission vocabulary | Snap only |
| **TUF** | serial, expiry, rollback and freeze protection, key rotation | heavyweight; only its ideas are used |
| **minisign / Ed25519** | simple, auditable signatures (`python-cryptography`) | needs domain binding |
| **RFC 8615** `/.well-known/` | discovery bound to the vendor's domain | — |

### 13.2 Format

**Cygnus Application Manifest (CAM) v1:**
- **JSON**, specified by a published **JSON Schema 2020-12** generated from pydantic models.
- `manifest_version`, a monotonic `serial` and an `expires` field.
- A detached **minisign-format Ed25519** signature (`.minisig`).
- **Key binding:** keys are bound to the vendor **domain** through `https://<domain>/.well-known/cygnus/keys.json`, which is signed and supports rotation. They are pinned on first use or shipped in Cygnus's curated trust store.

Sketch (abridged):

```json
{
  "manifest_version": 1, "serial": 2026100601, "expires": "2027-04-01T00:00:00Z",
  "application": {"id": "org.whatpulse.WhatPulse", "name": "WhatPulse",
    "vendor": {"name": "WhatPulse", "domain": "whatpulse.org"}, "supported_versions": ">=6.0",
    "platforms": [{"os": "linux", "arch": ["x86_64"], "sessions": ["x11", "wayland"]}]},
  "sources": [
    {"id": "appimage", "format": "appimage", "url": "https://releases.whatpulse.org/latest/linux/whatpulse-linux-latest_amd64.AppImage",
     "update": {"type": "zsync", "self_updating": true}, "verification": {"type": "tls-only"},
     "recommended_when": ["relocatable", "wayland-window-tracking"]},
    {"id": "flatpak-bundle", "format": "flatpak-bundle", "update": {"type": "manual"},
     "known_issues": ["runtime-eol:org.freedesktop.Platform//23.08"]}],
  "features": [
    {"id": "core", "name": "Main application", "required": true},
    {"id": "keys", "name": "Keyboard tracking", "requires": ["input-access"]},
    {"id": "mouse", "name": "Mouse tracking", "requires": ["input-access"]},
    {"id": "network", "name": "Network statistics", "requires": ["pcap-service"]},
    {"id": "web", "name": "Browser activity", "optional": true, "requires": ["web-insights"], "notes": "Requires WhatPulse Premium"}],
  "components": [
    {"id": "input-access", "type": "permission", "relation": "hard",
     "action": {"kind": "group.add_user", "group": "input"}, "requires_relogin": true,
     "security_note": "Members of 'input' can read all keyboard and mouse events.",
     "verify": [{"probe": "group_membership", "group": "input", "effective": "session"}]},
    {"id": "pcap-service", "type": "system-service", "relation": "hard", "required_for": ["network"],
     "platform_sources": {"arch": {"kind": "pacman.install_local",
        "url": "https://github.com/whatpulse/linux-external-pcap-service/releases/download/v1.5.1/whatpulse-pcap-service-1.5.1-1-x86_64.pkg.tar.zst",
        "sha256": "54fe5e87…160c"}},
     "post": [{"kind": "systemd.enable_now", "unit": "whatpulse-pcap-service.service"}],
     "privileges": ["root-service", "CAP_NET_RAW", "CAP_NET_ADMIN"],
     "verify": [{"probe": "systemd_unit", "unit": "whatpulse-pcap-service.service", "expect": ["enabled", "active"]},
                {"probe": "tcp_peer", "addr": "127.0.0.1", "port": 3499}]},
    {"id": "web-insights", "type": "browser-extension", "relation": "optional",
     "browsers": {"chromium-family": {"id": "fnfhoihlmikplapbgegdmpifhgmaigbf"}, "firefox": {"id": "webinsights@whatpulse.org"}},
     "action": {"kind": "browser.open_store"}}],
  "conflicts": [{"between": ["appimage", "flatpak-bundle"], "reason": "both listen on 127.0.0.1:3499"}],
  "user_data": {"paths": ["~/.local/share/whatpulse", "~/.var/app/org.whatpulse.WhatPulse"]}
}
```

### 13.3 Trust levels

| Level | Source | What it may do |
|---|---|---|
| 1. **Vendor-signed** | key bound to the vendor domain | propose components; every helper action stays APPROVAL |
| 2. **Curated** | signed by the Cygnus project key, shipped with Cygnus (WhatPulse and Helium start here) | same |
| 3. **Package metadata** | pacman `optdepends`, AppStream relations | informational / APPROVAL |
| 4. **Third-party / Unverified** | user-imported or AI-drafted | always shown as *unverified*, with citations; APPROVAL |

No trust level can turn a privileged action into AUTO. AUTO is limited to unprivileged, reversible, user-scope steps (§8).

### 13.4 Closed action vocabulary (no shell, ever)

| Area | Actions |
|---|---|
| Packages | `pacman.install_repo(names)`, `pacman.install_local(url, sha256 \| signature)`, `aur.build(pkgbase)` (review required) |
| Flatpak | `flatpak.install(ref, remote)`, `flatpak.install_bundle(url, sha256)`, `flatpak.add_remote(flatpakrepo_url)`, `flatpak.override(app, permissions)`, `flatpak.pin(runtime)` |
| AppImage | `appimage.install(url, verification)` |
| Services | `systemd.enable_now(unit)` / `systemd.user.enable_now(unit)` — only for units shipped by a managed package or component |
| Permissions | `group.add_user(group)` — allow-listed by manifest; the caller's own account only |
| Browser | `browser.open_store`; `browser.external_extension(id, update_url)` — the Chromium "External Extensions" mechanism, which prompts in the browser and never force-installs |
| Native messaging | `nmh.install(browser, manifest from a package/component)` |
| Information | `info.show(text)` |

Each action has a dedicated, validated code path. This vocabulary is the **only** way a manifest can cause change.

### 13.5 Discovery order

1. The curated catalogue shipped with Cygnus.
2. AppStream `<custom><value key="cygnus::manifest">URL</value></custom>`.
3. `https://<vendor-domain>/.well-known/cygnus/manifest/<app-id>.json`. The domain comes from AppStream `developer id`/homepage or a Flathub-verified domain.
4. Package metadata.
5. Optional AI drafting (§14).

---

## 14. Discovery fallback & AI layer

**Without a manifest**, Cygnus uses:
- package metadata: `optdepends` → OPTIONAL; AppStream relations → probes;
- Flatpak permissions, which are informational;
- the companion taxonomy signals in §12.3.

**AI layer** (optional, off by default; Cygnus is fully functional without it):

1. A deterministic fetcher retrieves vendor documentation from an **allow-list**: the vendor domain (taken from package or AppStream metadata) and known code forges.
2. The model receives that text **as data**. It must return **only** a draft manifest in the CAM schema, with a citation (URL + quote) for every claim.
3. The model has no tools that act. Its output is strictly parsed against the schema and the vocabulary.

**Every AI-derived item is handled the same way:**
- labelled *Third-party / Unverified*;
- deterministically verified where possible: URLs on the vendor domain, downloads hash- or signature-checked, packages validated by their package manager;
- shown with citations;
- approval-gated;
- executed only through the §13.4 actions.

**Shell commands found in documentation** (`curl … | sudo bash`, `sudo sh setup-input-permissions.sh`) remain *quoted text*.
- A few well-understood patterns are recognised deterministically and offered as typed, reviewable actions. Example: `gpasswd -a $USER input` → `group.add_user(input)`.
- Everything else is non-actionable.
- The WhatPulse script's `chmod 644 /dev/input/event*` is an example of what gets dropped and reported.

---

## 15. Desktop integration (KDE Plasma)

**Packages and Flatpaks** bring their own desktop files. Cygnus validates them and never edits files it does not own.

**AppImages and portable apps:**

| Item | How Cygnus handles it |
|---|---|
| Desktop file | Written to `~/.local/share/applications/`. Cygnus keeps the upstream desktop ID (e.g. `helium.desktop`) **only if no other `XDG_DATA_DIRS` entry has it**, so the Wayland app_id matches and the taskbar icon is right. Otherwise it uses `cygnus-<id>.desktop` and sets `StartupWMClass`. The collision check is repeated at every reconcile (`DESKTOP_ID_SHADOWED`). The file carries `X-Cygnus-Managed=true`. |
| Launch command | `Exec` points to a **launcher shim** on the SSD, `~/.local/libexec/cygnus/launch/<id>`. This POSIX sh script checks the device is present (`/dev/disk/by-uuid/…`) without touching the mount. If the drive is missing, it shows a KDE notification ("WhatPulse is stored on HDD (DATA), which is not connected"); otherwise it `exec`s the AppImage. Moving the app only rewrites the shim. |
| Icons | `~/.local/share/icons/hicolor/<size>/apps/` |
| AppStream data | optional, in `~/.local/share/metainfo/` |
| MIME types | `MimeType=` is preserved. Cygnus runs `update-desktop-database ~/.local/share/applications`, then `kbuildsycoca6` so Plasma refreshes immediately (failure is non-fatal; KSycoca also rebuilds lazily). |
| Autostart | Entries in `~/.config/autostart/` are tracked and rewritten on move. WhatPulse's current entry points straight at `/mnt/data/…`. |

**Default applications:**
- "Set as default browser" writes `x-scheme-handler/http` and `x-scheme-handler/https` to `~/.config/mimeapps.list`, under `[Default Applications]` and `[Added Associations]`, exactly as KDE's `KApplicationTrader` does. `text/html` is optional.
- Defaults are only changed when you ask.

**Native messaging hosts:**
- Manifests come from packages or manifest components. Locations: `~/.config/<chromium-family>/NativeMessagingHosts/`, `~/.mozilla/native-messaging-hosts/`, and `~/.var/app/<browser>/…` for Flatpak browsers.
- Their `path` targets are verified. Cygnus explains when a Flatpak browser cannot reach a host program on the host system.
- Existing Flatpak overrides, such as the Plasma Browser Integration ones in `~/.local/share/flatpak/overrides`, are adopted, never overwritten.

**"I downloaded a file and opened it":**
- Cygnus registers as an **additional, non-default** handler for: `application/vnd.appimage`, `application/vnd.flatpak`, `application/vnd.flatpak.ref`, `application/vnd.flatpak.repo`, `application/vnd.debian.binary-package` and `application/x-rpm`.
- It also adds a Cygnus MIME type for `*.pkg.tar.zst`/`*.pkg.tar.xz`, a sub-class of `application/x-zstd-compressed-tar` so Ark keeps working.
- Shelly is currently the default for `.flatpak`. Cygnus *offers* to become the default and never takes it silently.

**Notifications:** KNotifications (Python binding) with `cygnus.notifyrc`.

---

## 16. Security model

### 16.1 Principles

**No root GUI, ever.**

**The helper owns the plan.** The client sends an *intent*, e.g. "install `whatpulse-pcap-service` from fd #3 with expected sha256 X". The helper then:
1. resolves, validates and computes the plan itself;
2. stores it, and returns a **single-use `plan_id`**, a short expiry and a human-readable summary;
3. on `Commit(plan_id)`, triggers polkit with that summary substituted into the authentication message (name, version, sha256, source, "runs code as root");
4. executes exactly the stored plan.

A process running as you can still *request* plans, but it cannot change what gets executed after you have seen the summary. The authentication dialog, not the GUI, shows the exact action.

**Authorization tiers** (§16.2):
- `auth_admin_keep`, a 5-minute reuse, applies **only** to installs and full upgrades from **configured, signed repositories**.
- Everything that runs unsigned or locally built code as root, removes anything outside the ledger, or changes permissions or storage needs `auth_admin` **every time**, with details.

**Input handling — everything is treated as hostile:**
- strict regexes for names;
- user-supplied files arrive by **fd passing**, are copied into root-owned `/var/cache/cygnus/staging/`, then hashed and parsed;
- fd-relative path handling, no symlink following;
- no shell: argv lists only, sanitized environment, `LC_ALL=C`;
- allow-listed pacman flags only.

**Least scope:**
- Removals only cover packages in the ledger, or ones you explicitly confirm in the helper-generated summary.
- There is a **protected package set** (§10.1).
- Unit verbs only touch units shipped by managed packages.
- Group verbs only affect **the caller's own account**, for allow-listed groups.

**Process integrity:**
- Both processes run under `python -I`, so user `site-packages`, `.pth` files and `PYTHON*` environment variables are ignored.
- The helper is a systemd D-Bus service with hardening that is compatible with running pacman: private `/tmp`, no new privileges for children where possible, a restricted environment.

**Auditing:**
- Every privileged step is logged to journald (structured, with a `MESSAGE_ID`) and to the ledger.

**Destructive actions** (remove, delete user data, roll back) always get an explicit confirmation listing the items.

**Never:** Cygnus never formats, partitions, edits the bootloader, or deletes anything outside its owned artefacts.

### 16.2 Polkit actions (active local session)

| Action ID (`<app-id>.…`) | Default | Covers |
|---|---|---|
| `packages.install-repo` | `auth_admin_keep` | installs from configured signed repos (incl. makedepends) |
| `system.upgrade` | `auth_admin_keep` | full `-Syu` (+ targets) |
| `packages.install-local` | `auth_admin` | local, AUR-built and converted packages (unsigned / user-built code as root) |
| `packages.remove` | `auth_admin` | any removal or replacement |
| `services.manage` | `auth_admin` | enable / disable / restart units of managed packages |
| `permissions.manage` | `auth_admin` | group membership changes (`input` = keystrokes) |
| ~~`storage.manage`~~ | | not built: storage is configured without administrator rights, so the action was removed |
| `recovery.manage` | `auth_admin` | stale lock removal, rollback, expert snapshot rollback |

### 16.3 Helper API (D-Bus `…Helper1`)

All long-running calls emit progress signals.

**Plan methods** — each returns `plan_id` + summary:

| Method | Arguments |
|---|---|
| `PlanPackages` | `install_repo[]`, `install_local_fds[]` + `expected_sha256[]`, `remove[]`, `with_sysupgrade`, `syncdb_snapshot_digests` |
| `PlanUnit` | `unit`, `action` |
| `PlanGroup` | `group`, `add\|remove` |
| `PlanStore` | `fs_uuid` |
| `PlanFlatpakInstallation` | `fs_uuid`, `enable\|disable` |
| `PlanClearStaleLock` | — |

**Other methods:**

| Method | Purpose |
|---|---|
| `Commit(plan_id)` | Triggers polkit and executes. |
| `GetOperation(op_id)` | Returns state and journal. |
| `GetLedger()` | Read-only. |

### 16.4 Threat model (abridged)

| Threat | Mitigation |
|---|---|
| Malicious package or manifest | Signatures and hashes; trust levels; closed vocabulary; AUR review |
| Web/AI instructions and prompt injection | Never executed; output is schema-validated data (§14) |
| Local malware as the same user | Can request plans, but polkit shows helper-generated details. Risky tiers always re-authenticate. `python -I`. Protected set. |
| Confused deputy / time-of-check-time-of-use | Helper-owned plans; fd staging; DB snapshot binding |
| Path swaps on user-writable drives | Root never operates on user-owned locations. Stores exist only on root-owned path chains, with pre-mount verification (§6.4). |
| NTFS ownership forgery | NTFS is never trusted for root content (§2.5). `nosuid,nodev` is recommended for `/mnt/data`. |
| Manifest rollback or freeze | `serial` + `expires` |
| Unsigned local packages (`LocalFileSigLevel=Optional`) | Explicit trust label, a vendor hash requirement, and `auth_admin` with details |
| AUR PKGBUILD risk | Mandatory review with diffs; "user-built, runs as root" stated at authentication |

---

## 17. Update management

| Source | Provider | Notes |
|---|---|---|
| Repo packages | pyalpm against fresh sync DBs in a private temp dbpath (unprivileged, crash-isolated) | Applied **only** as a full upgrade via the helper, with DB snapshot binding. Coexists with pacman, paru and Shelly. |
| AUR | RPC version comparison; `-git` packages per `Devel` semantics | Rebuild with **diff review** |
| Flatpak (remote) | libflatpak update listing + transaction dry-run | EOL/rebase checks first; new runtimes are previewed |
| Flatpak (bundle without origin) | vendor manifest | Otherwise: "Manual updates only — download a newer bundle from the vendor" |
| AppImage | zsync `.upd_info` header (length / SHA-1 / mtime) or the GitHub releases API (asset sha256 digests) | Atomic replace; previous version kept; vendor self-updates recorded |
| Local / converted packages | vendor manifest URL (`VENDOR_PKG_STALE`) | Otherwise: "No automatic update mechanism exists" |

**How updates are applied:**
- Every update runs through the same pipeline (§8): dependency, runtime and companion checks all happen **before** any change.
- An optional `cygnus-check.timer` (user unit) sends notifications only. It never installs anything by itself.

---

## 18. Uninstall

The plan screen offers three explicit choices:

1. **Remove application.** Removes the payload, the integration Cygnus created, and dependencies Cygnus installed that nothing needs any more.
2. **Remove application + user data.** Also removes declared or conventional data paths. Each path is listed with its size and can be unticked.
3. **Remove optional components.** Removes services, extensions, group memberships and native messaging hosts that Cygnus added and nothing else uses. Each one is listed with the privilege it needs, for example: "Disable and remove the WhatPulse PCap Service (a root service that forwards packet headers to 127.0.0.1:3499)".

Adopted and unowned items are kept unless you explicitly select them. A summary lists what was kept and why.

---

## 19. Move

Before you confirm, the move plan shows:
- the current size;
- the movable size;
- what must stay on system storage, and why;
- free space on the target after the move;
- whether the app must be closed first (Cygnus checks running processes).

| Format | How the move works |
|---|---|
| **AppImage / portable** | Copy → verify the hash → update the shim, autostart entry and registry → remove the source only after verification. Rollback means keeping the source. |
| **Flatpak** | Install into the target installation → verify → uninstall from the source. Runtimes follow the app, checked across **all** installations. |
| **pacman packages** | Not movable: "managed by the system package manager". §7.3 applies only on `posix` stores. |

---

## 20. GUI / UX

**Navigation:** a Kirigami window with a global drawer: **Applications · Updates · Available Applications · Storage · Activity · Settings**.

**Applications:**
- A list with icon, name, version, format chip, **location chip** (SSD / HDD / ⏏) and **health badge**.
- Filters for "needs attention" and "updates".

**Application page:**
- Header: icon, name, vendor, version, source and trust badge.
- **Features & components** table (✓ ⚠ ✗ ? ○ ⏏), with inline actions such as **Install missing component** and **Repair**.
- Storage breakdown.
- Update status and history.
- Actions: **Update · Repair · Reinstall · Move · Uninstall**.
- A **Technical details** expander for the Linux specifics.

**Install flow:**
1. Drop or open a file, or search.
2. A live analysis checklist runs: detect → dependencies → runtimes → components → permissions → storage → staleness.
3. A **plan page** shows the mock-ups from the brief, including estimated HDD and SSD usage.
4. Confirm. The helper's authentication dialog shows the exact privileged summary.
5. Per-step progress.
6. A **verification report** with the final health.

**Problems** appear as `Kirigami.InlineMessage` cards with **Resolve Automatically / Show Details / Cancel** and a plain-language explanation.

**Storage page:**
- Locations with capacity bars.
- Plain-language capabilities, e.g. "Can store: apps you run · Cannot store: system packages (NTFS)".
- An **Add Location** wizard with detected candidates.
- Offline state, and the `nosuid,nodev` hardening suggestion for NTFS.

**Settings:** default location, previous versions to keep, AUR review policy, update-check schedule, AI discovery (off; provider), trust store.

**Platform integration:** native portal dialogs, KNotifications, and Breeze styling via `qqc2-desktop-style`.

---

## 21. Worked example: WhatPulse (on this machine)

### Vendor facts

- **Formats:** the official Linux builds are an **AppImage** and a **Flatpak bundle** only. The latest stable is 6.3.2 (2026-08-08); the latest beta is 7.0-beta4.
- **Distribution:** there is **no Flathub listing and no Flatpak repo**.
- **Runtime:** both the stable bundle and the newest beta bundle target the **EOL `org.freedesktop.Platform//23.08`** runtime.
- **AppImage:** updates through `zsync|https://releases.whatpulse.org/latest/…AppImage.zsync` and is **unsigned**.

### Feature → requirement matrix

| Feature | Requirement | Relation | Probe | Fix (privilege) |
|---|---|---|---|---|
| Main application | AppImage, or Flatpak plus runtime 23.08 (EOL) | hard | AppImage present / `flatpak info` | Install. Prefer the AppImage on Plasma Wayland. |
| Keyboard tracking / Mouse tracking | Read access to `/dev/input/event*` via the **`input` group** (systemd's default udev rules set `GROUP=input`) | hard | `group_membership(input, session)` + the WhatPulse process's group list | `group.add_user(input)` (`permissions.manage`), then log out and back in. **Not** the vendor script: it `chmod 644`s every input device and writes an obsolete udev rule. |
| Network statistics | **External PCap Service**: a root service with `CAP_NET_RAW`/`CAP_NET_ADMIN`, connected to the client on `127.0.0.1:3499` | hard (for this feature) | `systemd_unit` enabled + active; `journal_recent_match("PF_RING Stats")`; `tcp_listener(3499, owner=whatpulse)` + `tcp_peer` | `pacman.install_local` (vendor GitHub asset, sha256 pinned; `auth_admin`) + `systemd.enable_now`. Never the vendor's `curl \| sudo bash` installer. |
| Browser activity (Web Insights) | MV3 extension (CWS `fnfhoihlmikplapbgegdmpifhgmaigbf`, AMO `webinsights@whatpulse.org`) talking to `ws://127.0.0.1:3488`; requires a **Premium** account | optional | profile scan + `tcp_listener(3488)` | `browser.open_store` (user action); pairing is approved inside WhatPulse |
| App / window uptime on Wayland | Unsandboxed access to compositor information (the Flatpak lacks KWin bus access — inferred) | recommended | install type + session | Prefer the AppImage |

### What Cygnus reports today (adopting the existing installs)

```
WhatPulse 6.3.2 (AppImage on HDD · unsigned)                         ⚠ Missing required component
  Main application                 ✓  /mnt/data/apps/CachyOS/whatpulse-linux-latest_amd64.AppImage (running)
  Keyboard tracking                ?  Likely failing: your account is not in "input"; /dev/input is root:input 0640, no ACL
  Mouse tracking                   ?  (same cause)
  Network statistics               ✓  PCap Service enabled, active, connected to WhatPulse (127.0.0.1:3499)
  Browser activity                 ○  Optional: Web Insights extension (WhatPulse Premium)
  Also found: Flatpak copy of WhatPulse 6.3.2 (not running) on EOL runtime 23.08, no update source
  [Grant input access]  [Keep one copy…]  [Show details]
```

### New install, choosing HDD

```
Install WhatPulse
  Main application:     HDD   (AppImage, 98 MB — vendor-recommended; the Flatpak bundle needs an obsolete runtime)
  System components:    SSD   (PCap Service: 68 KB package + system service)
  Required components:  Input access (group "input")  ·  External PCap Service
  Optional components:  Web Insights (browser extension, Premium)
  Runtime:              none (AppImage)
  Status:               All required components available
  Notes:                Unsigned download (HTTPS only). Input access lets programs you run read keystrokes.
  [Cancel] [Install]
```

### If you insist on the Flatpak bundle

```
WhatPulse (Flatpak bundle)
  Required runtime: org.freedesktop.Platform/x86_64/23.08   Status: available on Flathub — END OF LIFE
  Checked: newer WhatPulse build? → 6.3.2 and 7.0-beta4 both use 23.08 · vendor Flatpak repo? → none · Flathub listing? → none
  Options: ● Use the official AppImage instead (no runtime needed)   ○ Install with obsolete runtime (not recommended)
  [Resolve Automatically] [Show Details] [Cancel]
```

None of this is hard-coded. It all comes from the curated WhatPulse manifest plus the generic probes.

---

## 22. Worked example: Helium

### Sources

- An official **signed AppImage**. Key `BE677C19…378E` is pinned in the curated manifest.
- A tarball (`.tar.xz`) with a detached `.asc` signature.
- A `.deb` and an apt repository.
- A COPR repository.
- `helium-browser-bin` (AUR, or the CachyOS repo when present), built from the signed tarball and installed into `/opt/helium-browser-bin`.
- The vendor refuses Flatpak.

### Installing to the HDD

1. Cygnus picks the **signed AppImage** and verifies it against the pinned key.
2. It stores the file in `<HDD>/Applications/Helium/`.
3. It integrates the desktop entry:
   - **Keeps `helium.desktop`** unless a system `helium.desktop` exists. The `helium-browser-bin` package ships one, so if that is also installed, Cygnus reports `DESKTOP_ID_SHADOWED` and asks which should win.
   - Registers the `%U` URL/file handlers and the `new-window` and `new-private-window` actions.
4. It offers "Set as default browser".

Health probe: unprivileged user namespaces must be enabled, because Helium ships no setuid sandbox.

Updates come from `gh-releases-zsync|imputnet|helium-linux|…` via the GitHub API (asset digests), plus the signature check.

### Installing to the SSD

The SSD option, or your preference for the system package, gives `helium-browser-bin`: from the repo if present, otherwise from the AUR with PKGBUILD review. The plan shows `/opt` on the SSD.

### Your existing Helium AppImage

It is adopted in place. Moving it into the managed folder is optional.

---

## 23. Testing strategy

### 23.1 Unit and component tests

These use pytest, make no system changes, and use fixtures generated by the tests themselves.

- **Detection.** Tiny generated samples of each format:
  - an AppImage (ELF stub + `mksquashfs`), a deb (`ar` + tar), an rpm (crafted header);
  - a `.pkg.tar.zst` (`tarfile`), Flatpak bundles from a local test OSTree repo, a `.flatpakref`;
  - architecture variants;
  - hostile archives: path traversal, symlinks, oversize members, zstd window bombs.
- **Storage.** mountinfo/lsblk fixtures, including this machine's shared-superblock NTFS mounts; the probe on tmpfs/btrfs temp dirs; placement-policy tables.
- **Resolver.** pyalpm against **fixture sync/local DBs** (missing deps, conflicts, replaces, arch mismatch, partial-upgrade risk), with a **crash-isolation test** for the known conflict segfault.
- **Runtime resolver.** Local OSTree repos with refs marked EOL (`flatpak build-commit-from --end-of-life`), missing runtimes, rebase targets.
- **Recovery.** Table-driven issue→resolution tests, plus classifiers checked against recorded pacman/flatpak/makepkg outputs.
- **Planner.** Golden-file plans for install, update, uninstall, move, repair and reinstall.
- **Manifests.** Schema checks, signatures with test keys, expiry/serial rollback, vocabulary enforcement.
- **Desktop integration.** Temporary `XDG_*` dirs, `desktop-file-validate`, shim generation, autostart rewrite, ID-collision detection, `mimeapps.list` default writing.
- **Services and health.** A fake systemd D-Bus, plus real `systemd --user` transient units.
- **Executor.** **Fault injection** at every step boundary: compensations, rollback, and crash-resume from both journals.
- **Helper.**
  - Request fuzzing.
  - Plan/commit protocol: `plan_id` single use and expiry; mismatch handling.
  - Flag allow-list; protected set.
  - Runs on a private D-Bus with a fake polkit authority.
- **GUI.** pytest-qt view-model smoke tests.

### 23.2 Isolated integration tests (no host changes)

- **Flatpak.** Uses **`FLATPAK_USER_DIR` only**, pointed at a temp dir, with a local test repo (app + runtime): install, missing runtime, EOL warning, bundle install, uninstall.
  - `FLATPAK_SYSTEM_DIR` is **not** used: system operations would still go through the real system helper into `/var/lib/flatpak`.
- **pacman.** Sandbox root plus `fakeroot` (the §2.2 technique): install, upgrade, remove, `-Qk`, and the relocation regression tests.
- **Offline drive.** A loop-free simulation: a temp "location" made unavailable mid-test, checking the shim notification, ⏏ state, queued operations, and that nothing is written to the fallback path.

### 23.3 Real tests on this machine

Each is explained beforehand and run only with your go-ahead. All are reversible.

| # | Test | What happens |
|---|---|---|
| 1 | **Pacman** | Install and remove a tiny repo package (e.g. `ncdu`) via the helper. |
| 2 | **AUR** | Review, build, install and remove a tiny AUR package with no dependencies. |
| 3 | **Flatpak** | Install a small Flathub app to the SSD, then to the HDD (partially relocated user installation, after the OSTree probe passes). Move it, then uninstall. |
| 4 | **Flatpak with missing runtime** | The real WhatPulse bundle in an **isolated `FLATPAK_USER_DIR`** with no runtime, exercising §9 against real Flathub metadata. On the host itself, only diagnosis of the existing install. |
| 5 | **AppImage** | Adopt Helium (signature verified) and WhatPulse. Integrate them, and optionally move one to the managed folder. |
| 6 | **DEB** | Analyse `whatpulse-pcap-service_1.5.1_amd64.deb` (expected verdict: "prefer the vendor's Arch package"). Also convert, install and remove a self-made test `.deb`. |
| 7 | **RPM** | Analyse the WhatPulse pcap `.rpm`, including scriptlet classification. Also convert a self-made test `.rpm`. |
| 8 | **Companion services** | WhatPulse health check plus the "input access" fix. This changes your groups, so it is a separate approval. |
| 9 | **Coexistence** | Start a Shelly or paru update while Cygnus plans: verify lock waiting and external-change attribution. |

---

## 24. Implementation plan

### Phase 0 — This proposal

- **Deliverables:** this document.
- **System changes:** none.

### Phase 1 — Foundations

**Deliverables:**
- repo layout and `pyproject`;
- core models; registry and migrations;
- storage discovery and the unprivileged probe;
- format detection for all formats;
- CLI skeleton;
- tests.

**System changes (each needs your approval):**
- Install from the official repos: `pyside6 python-cryptography python-systemd squashfs-tools python-pytest python-pytest-qt`.
- Run the **unprivileged** storage probe on `/mnt/data`. It creates and deletes `/mnt/data/.cygnus-probe-*`, including a tiny OSTree test repo.

### Phase 2 — Analysis engine

**Deliverables:**
- **Backend analysis:**
  - pacman: crash-isolated dry-run plus `pacman -Sp` fallback;
  - AUR RPC;
  - Flatpak, bundles and EOL;
  - AppImage: metadata, signatures, update info, prerequisites;
  - deb/rpm analysers.
- **Planning:** runtime resolver; diagnosis catalogue; planner, including repair and reinstall plans.
- **Manifests:** schema, signing, trust store, and curated WhatPulse and Helium manifests.
- **Health:** probe library and health engine.
- **Tests:** isolated Flatpak (`FLATPAK_USER_DIR`) and pacman sandbox tests.

**System changes:** none.

### Phase 3 — Execution

**Deliverables:**
- **Execution core:** executor, journals and compensations.
- **Executors:** AppImage/portable; Flatpak; desktop integration (shims, MIME, defaults, native messaging hosts).
- **`cygnus-helper`:** plan/commit protocol, polkit tiers, ledger, privileged journal, audit, block inhibitor, rollback cache.
- **Operations:** recovery engine, update manager, uninstall, move, repair, reinstall.

**System changes:** none. The helper is tested on a private bus.

### Phase 4 — GUI

**Deliverables:** the Kirigami app with all pages and flows; notifications; MIME handlers (offered, not forced); update-check timer.

**System changes:** none.

### Phase 5 — Packaging and real tests

**Deliverables:**
- a PKGBUILD;
- **installing Cygnus as a pacman package** — this places the helper, polkit policy, D-Bus config, systemd units and the desktop entry (whose `MimeType` line is how Cygnus is offered for software files) under `/usr`. (A pacman hook and a separate MIME XML file were planned and are not part of the package.)
- real-machine tests 1–9;
- adopting your existing apps;
- the full documentation set.

**System changes:** yes — the package install, plus each real test, announced before it runs.

### Phase 6 — Optional

**Deliverables:**
- an AI discovery provider;
- managed payload mounts on `posix` stores (§7.3);
- custom system Flatpak installations on `posix` stores.

**System changes:** per feature.

### Documentation set (`docs/`)

- architecture
- supported formats
- storage model
- security model
- dependency model
- runtime resolution
- recovery engine
- **vendor manifest specification** (normative, with JSON Schema and test vectors)
- companion components
- feature requirements
- AI discovery
- limitations
- installation
- development
- testing
- troubleshooting

---

## 25. Limitations (stated up front)

1. **Native packages stay on system storage.** pacman, AUR and converted packages cannot be relocated, because pacman cannot follow relocation (§2.2). The optional managed mounts work only on Linux-native secondary drives.
2. **This HDD (NTFS) holds user-level applications only.** That means AppImages, portable apps and the Flatpak app/runtime storage of the user installation. The same applies to any exFAT or FAT drive.
3. **NTFS has general caveats:**
   - A Windows "Fast Startup" or hibernated volume mounts read-only or dirty. Cygnus detects and reports this but cannot repair it; that needs `chkdsk` on Windows.
   - ntfs3 is less proven than ext4 or btrfs.
   - A Flatpak on the HDD makes the disk spin up when Flatpak apps launch.
4. **DEB and RPM support is analysis-first.** Conversion happens only when it is demonstrably safe. Many packages will instead be answered with "use the native/AUR/Flatpak/AppImage equivalent" or "cannot be installed safely".
5. **Flatpak runtimes cannot be swapped.** An app built for an EOL runtime needs that runtime or a vendor rebuild.
6. **Feature detection is only as good as its probes and manifests.** Without a manifest, Cygnus knows only package metadata and generic probes, and it says so.
7. **Some apps cannot be updated automatically.** Bundles without an origin and unknown local packages show "manual updates".
8. **Downgrades after a full upgrade are risky on Arch.** Rollback of system upgrades relies on snapper (expert option); package-level downgrades are offered only with warnings.
9. **pyalpm has known defects.** It is used only for analysis, in a crash-isolated child process; pacman itself performs every commit.
10. **Wayland specifics may be inferred.** Some behaviour, such as WhatPulse window tracking, is partly undocumented by the vendor, so claims about it are marked as inferred.
11. **Cygnus coexists with other package tools but cannot read intent.** It reconciles with pacman, paru, Shelly and the flatpak CLI, but cannot know *why* those changes were made.

---

## 26. Decisions needed

1. **Approve the architecture and stack:** Python + PySide6/Kirigami; a helper-owned plan/commit D-Bus helper with tiered polkit; the SQLite registry plus root ledger.
2. **Approve the Phase 1 system changes:** the package installs, and the unprivileged storage + OSTree probe on `/mnt/data`.
3. **Flatpak on the HDD:** the partially relocated user installation described in §7.2 (recommended). The alternative is to keep Flatpak on the SSD only.
4. **Optional phase 6:** do you want managed payload mounts for native packages on Linux-native drives at all? They are not applicable to this HDD.
5. **Name and app ID:** keep "Cygnus"? Which reverse-DNS ID (e.g. `io.github.<your-user>.Cygnus`)?
6. **AI discovery:** skip for now, or wire an optional provider later?
7. **Your system (outside Cygnus):** do you want to add `nosuid,nodev` to the `/mnt/data` fstab entry (§2.5)? This change is entirely up to you.

---

## Appendix A — Evidence log (selected)

- **pacman relocation sandbox runs:** scratchpad `pacman/tmp/pactest-*` (§2.2); `lib/libalpm/add.c` cases 1–6 and `ARCHIVE_EXTRACT_UNLINK`.
- **pyalpm:**
  - dry-run `ADD: [('ncdu','2.9.2-1.1','cachyos-extra-v3','x86_64_v3')]`;
  - the conflict segfault and the unused `questioncb` reproduced in sandbox `review/qtest`.
- **flatpak:**
  - `flatpak_dir_use_system_helper`, `flatpak_find_deploy_for_ref(_in)`, `--print-updated-env` (`app/flatpak-main.c`);
  - `flatpak-transaction.c` dependency sources (563–597);
  - unused-ref computation;
  - `realpath` of the user base dir (`flatpak-dir.c` 8348);
  - polkit rules file;
  - `Flatpak.Error` / `get_eol*` / transaction signals introspected via GI.
- **ntfs3:** `xattr.c` `ntfs_get_wsl_perm` / `ntfs_save_wsl_perm` and the `$LX*` change restriction; `system.*` attribute handlers; `inode.c` `inode_init_owner` and the default ids; `file.c:ntfs_setattr`.
- **KDE:**
  - `ksycoca.cpp` `ensureCacheValid` (lazy rebuild, mtime checks);
  - `kcm_componentchooser` uses `KApplicationTrader` and `mimeapps.list` for `x-scheme-handler/http(s)`, with no `BrowserApplication`.
- **MIME:** `/usr/share/mime/packages/freedesktop.org.xml` defines `vnd.appimage`, `vnd.flatpak(.ref/.repo)`, `vnd.debian.binary-package` and `x-rpm`; `*.pkg.tar.zst` resolves to `application/x-zstd-compressed-tar`.
- **WhatPulse:**
  - whatpulse.org downloads, the help docs, the beta release notes;
  - GitHub `whatpulse/linux-external-pcap-service` v1.5.1 (assets, source: TCP 127.0.0.1:3499);
  - the Web Insights MV3 manifest (CWS `fnfhoihlmikplapbgegdmpifhgmaigbf`, AMO `webinsights@whatpulse.org`, `ws://127.0.0.1:3488`);
  - local state: `/etc/udev/rules.d/99-whatpulse-input.rules`, `/dev/input/event*` `root:input 0640`, `input:x:992:brltty`.
- **Helium:** the README (formats, Flatpak refusal, signing key); AppImage signature verification (good signature, key `BE677C1989D35EAB2C5F26C9351601AD01D6378E`); the AUR `helium-browser-bin` PKGBUILD.

## Appendix B — Review findings incorporated in rev. 2

| Area | Finding | Change |
|---|---|---|
| Security | The NTFS root-owned store was unsound (offline EA forgery; symlink planted before the mount; possible `system.*` attribute bypass) | NTFS is never a root trust boundary (§2.5, §6). Stores exist only on `posix` drives, with pre-mount verification (§6.4). |
| Security | Client-computed plan hashes plus broad `auth_admin_keep` | The helper owns the plan and uses `plan_id` + `Commit` (§16). `auth_admin_keep` is limited to signed-repo installs and upgrades. Allow-listed flags, protected set, `python -I`. |
| Correctness | pyalpm conflicts segfault; question callbacks unusable; `DownloadUser` lost | pyalpm is used for analysis only, crash-isolated. Commits go through the pacman CLI (§10.1). |
| Correctness | No defined DB snapshot for partial-upgrade-safe plans | Fast path vs. full-upgrade + targets, with sync DB snapshot binding (§10.1). |
| Correctness | Flatpak runtimes shared across installations can be pruned; library transactions lack dependency sources | Same-installation runtimes, pinning, explicit `add_default_dependency_sources()` (§2.4, §7.2, §9). |
| Correctness | Symlinking the whole user Flatpak dir would move `db/` and `overrides/` | Relocate only `repo/`, `app/`, `runtime/` and `.removed/` (§7.2). Found in real test 3: without `.removed/`, uninstalling failed with EXDEV; `flatpak repair` deletes the `.removed` link, so Cygnus restores relocation links before every operation. |
| Correctness | A logind `delay` inhibitor is capped at about 5 s; the journal was GUI-side | The helper holds a `block` inhibitor, keeps a privileged journal, ignores client disconnects, and keeps a rollback cache (§11). |
| Testing | Isolated Flatpak tests would have hit `/var/lib/flatpak` | Tests use `FLATPAK_USER_DIR` only (§23). |
| Facts | Default browser storage; HDD mount facts; user Flatpak dir contents; Shelly present; soname coverage | §1, §2.7 and §15 corrected; Shelly coexistence added. |
| Spec gaps | Repair/Reinstall, missing diagnosis codes, AppImage prerequisites, companion taxonomy, native messaging hosts, the "likely failing" state, offline tests | §12.3, §12.4, §8.1, §7.1, §15, §12.1 and §23 added. |
