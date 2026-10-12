# Cygnus security model

This document describes what the code enforces today. The design rationale is in
[architecture §16](architecture-proposal.md#16-security-model). Where the implementation is still
narrower than that design, this document says so.

## 1. Two processes, one boundary

| Process | Runs as | Does |
|---|---|---|
| **Cygnus** (`cygnus`, `cygnus-gui`) | you | Everything in your account: analysis, health checks, AppImages, the Flatpak *user* installation, menu entries, the registry and journal. |
| **Helper** (`io.github.omranabdulaziz.Cygnus.Helper1`) | root, started on demand by systemd over D-Bus | Only system changes: pacman installs, enabling services that installed packages ship, adding *you* to an allow-listed group, and (only when you chose it, for a script you were shown) running one install script as administrator, see [Running install scripts](#running-install-scripts-an-opt-in). |

There is never a root GUI.
- **Integrity:** the helper and the pyalpm worker run Python in isolated mode (`-I`): user
  `site-packages`, `.pth` files and `PYTHON*` variables are ignored. The GUI, the command line and
  the background check are ordinary programs of your own account (they can do only what you can).
- **Lifetime:** the helper exits when idle.
- **Sandboxing:** the helper's unit has a private `/tmp` and a read-only `/home`. The one deliberate exception is a script you chose to run as administrator: it runs in a system unit of its own without those limits, as `sudo` would run it (see below).

## 2. The helper owns every plan

1. Cygnus sends an **intent**, for example "install the package in file descriptor 3, expected SHA-256 X",
   or "add me to group `input`".
2. The helper:
   - validates the intent;
   - copies any file it receives (by **fd passing**, never by path) into its root-owned staging
     area and checks the hash;
   - **computes the plan itself**;
   - returns a single-use `plan_id` and a plain-language summary.
3. `Commit(plan_id)` asks polkit. The authentication dialog shows the helper's summary through
   `polkit.message`, not text supplied by the GUI.
4. The helper executes exactly the stored plan. Plans are bound to the D-Bus connection that
   requested them, single-use and short-lived.

Your own processes can *request* plans, but they cannot change what runs after you have seen the
summary.

### Polkit tiers

| Action | Default | Used for |
|---|---|---|
| `packages.install-repo`, `system.upgrade` | `auth_admin_keep` | installs and full upgrades from your configured, signed repositories |
| `packages.install-local` | `auth_admin` | downloaded or locally built packages: unsigned code that runs as root |
| `packages.remove`, `services.manage`, `permissions.manage`, `recovery.manage`, `scripts.run`, `cache.clean` | `auth_admin` | each time, with details |

Inactive and remote sessions are always denied (`allow_any` and `allow_inactive` are `no`).

### What the helper refuses

- **Names:** package and unit names must match strict patterns. Units must belong to an installed
  package, and a leading `-` is rejected.
- **pacman:** commands come from a fixed allow-list of programs and flags. It never runs `--nodeps`,
  `--dbonly`, `--noscriptlet`, `--overwrite`, `--assume-installed` or `--ask`. Installs never cause a
  partial upgrade: if your package databases are newer than the installed system (or that cannot be
  checked), an install is refused until the whole system is upgraded. A full upgrade is computed
  against freshly downloaded databases, and those exact database files are installed before
  `pacman -Su`.
- **Protected packages:** the kernel, firmware and microcode, core system packages, `pacman` itself
  and `HoldPkg` entries are never removed. The only replacement allowed is one your repositories
  declare during a full upgrade, and the authorization message names it. If pacman's configuration
  cannot be read, the helper refuses to plan instead of ignoring `HoldPkg`.
- **Replacements follow libalpm.** During a full upgrade the helper computes replacements the way
  libalpm does: the first repository that either carries an installed package under its own name or
  names it in `replaces` (a literal match of name and version constraint) decides. `provides` never
  triggers a replacement, so a package that merely provides another's name cannot displace it. The
  dialog names every replacement. Dependencies that only a replacement package needs are installed by
  pacman as usual, but are not yet listed in the summary.
- **Groups:** only your own account, only allow-listed groups (currently `input`). Removal is
  allowed only if the helper's ledger shows the helper added you.
- **Audit:** every privileged step is recorded in journald (structured) and in the helper's ledger. The ledger file is readable by root only (`0600`); users see their own entries through the helper.

### Package files are read by libalpm

For a package file, the plan and the authentication message take every fact from libalpm itself,
in the crash-isolated worker. These facts are:
- the name and version;
- dependencies, conflicts, provides and replaces;
- whether it runs an install script.

Cygnus's own `.PKGINFO` reader is never used for these decisions. A file crafted to read one way to
a simple parser and another way to pacman cannot disguise itself, for example `glibc` posing as
"harmless".

A local package is refused if it:
- is protected;
- conflicts with a protected package;
- replaces a protected package.

If it would replace an installed package, the message says so, with both versions. Dependencies it
needs from your repositories become part of the same plan, so `pacman -U` never pulls anything in
silently.

**What any local user can make the helper hold is bounded.** `PlanPackages` needs no password,
because planning is harmless, so its resources are limited:
- **Staged files:** limited per request and per user, and refused if free space is short.
- **Snapshots:** at most one full-upgrade database snapshot per user.
- **Cleanup:** whatever a refused, expired or denied plan held is deleted, as is anything left
  when the helper exits.
- **Privacy:** `GetLedger` shows each user only their own operations, and the ledger file itself
  is readable by root only.

**Untrusted package files are not opened by the root process before you approve anything.** To describe
a local package in the authorization dialog, the helper needs to know what the file says about itself.
Planning needs no password, so any local user can ask for it, and the file may be hostile. So the helper
never opens it itself. It starts a separate reader that prepares itself as root, before it has read
anything untrusted, and then gives root up completely before it opens the file:
- it leaves the network, IPC and hostname namespaces (no network, not even loopback);
- memory, CPU time, open files and writable file size are limited, core dumps are off, and **no process
  or thread can be created**;
- "no new privileges" is set;
- it becomes the dedicated account `cygnus-reader` (a locked system account that owns nothing and runs
  nothing else; Cygnus's package creates it) with no supplementary groups, then **proves** it: it checks
  that root cannot be regained, that no capability is left, and reports its identity;
- it installs a system-call filter (a denylist, `cygnus/helper/seccomp.py`): no new sockets of any kind
  (so no talking to the system bus or another service through a file), no namespaces, mounts, tracing,
  BPF, io_uring, key management, kernel modules, or changing the clock or hostname;
- only then does it open the file, with the package parser and libalpm, and answer with one line of JSON.

The helper treats that answer as untrusted too: exact shapes, bounded sizes, and **the plan is refused
unless the reader reports that it really was `cygnus-reader` (the user and group the helper told it to
become), with no process creation, no capabilities and the filter on**. If the account does not exist, or
the lockdown fails at any step, the file is not opened and nothing is planned. The staged copy is owned
by root and readable but not writable by `cygnus-reader`, so the reader cannot change what is installed
later. The plan shows who read the file (`read_by_uid`).

What this does and does not do. The filter is built for x86_64 only; on another architecture the reader
reports `seccomp: false` and the helper accepts that, relying on the account, the empty network and
the limits alone. A denylist is not a complete sandbox: a flaw in the parsers could still let a hostile
file run code as `cygnus-reader`, which can read whatever is readable by everyone on the machine and
nothing else, in an empty network, with tight limits, unable to start a process or open a socket. It
could also make the reader **misreport** the package: a wrong name or version in the dialog, and, because the
helper's protected-package rule (it refuses to touch `glibc`, `pacman`, the kernel and the like) is applied to
the name, conflicts and replacements the reader reports, that rule is only as honest as the reader is. It
cannot approve anything or reach root by itself, and the package you approve is the exact file whose checksum
the helper verified; but a package file from a source you do not trust is still something you are asked to
approve, by looking at the dialog, not something the protected-package rule alone would stop. A root-side check
of the file would need root to open it, which is exactly what this design avoids. After you authorize, pacman itself reads that file as root, as it does for any package you
choose to install; the reader exists so that merely *asking* for a plan exposes no root process to the
file.

One helper, one planning slot. The helper prepares one package plan at a time, shared by every user of the machine. So that
one user cannot keep it from the others: no more package files are read once a request has spent two minutes on them (each
single file is also limited to four minutes); whoever was turned away from the slot, including during a long plan, counts as
waiting for half a minute (or until that plan ends), and the user who had the slot last then waits ten seconds before taking
it again, so the user who waited can get the next turn; and a full-upgrade plan (which downloads databases, up to ten minutes) is
limited to one a minute per user. None of this can approve, install or read anything, and none of it prevents the delay
entirely. An operation that was still marked running when the helper died is marked *interrupted* in the ledger the next time
the helper starts, never "failed" or "succeeded", because what it got as far as doing is not known (and only when the new
helper owns the bus name, so a second helper that is about to quit cannot disturb the one that is working). When the GUI
learns that an operation it was waiting for was interrupted, it asks pacman whether the change is in place (the package at the
version it meant to install, or no longer installed for a removal) and records it only if so; otherwise it says the change may
be half done. An operation is looked up by its own id (`GetOperation`, your own operations only), so an old one is found however
many came after it. Updates still waiting for the helper when Cygnus is closed are kept in `late-commits.json` in the state
folder and finished by the next run.

### Links and files handed to Cygnus from outside

When Cygnus is the default program for Flathub's Install button and software files, a web page can make a browser open
it with an `appstream:` link, a `flatpak+https:` link (what Flathub's Install button actually opens) or a `.flatpakref` file.
Every such argument is hostile input (`cygnus/gui/launch.py`):
- an `appstream:` link must be a well-formed application id (at most 255 characters, no path parts, nothing else); any other
  address scheme (`https:`, `javascript:`, a `file://` on another host) opens nothing;
- a `flatpak+https:` link is followed only when it is exactly Flathub's own address for an app's reference file
  (`flatpak+https://dl.flathub.org/repo/appstream/<application id>.flatpakref`; scheme and host in any letter case, nothing
  else: no other host, no user-info, no query, no extra path); **nothing is fetched**, the application id in it is all that is used
  (as for an `appstream:` link). Any other `flatpak+https:` address only produces an explanation naming its host;
- a `.flatpakref` is read with a size cap (64 KiB; Flathub's own is about 5 KB) and must name a valid application id and
  branch; a runtime is refused; its repository address is matched **only to a remote that is already configured**
  (Flathub's own address is always accepted, because Cygnus adds that one itself). A file that names any other
  repository only produces an explanation: Cygnus never adds a repository a file names. A `.flatpakrepo` is explained,
  never followed, and Cygnus does not become the default program for that type;
- the result only opens the Install page with something to analyse and confirm. It never installs, and the usual
  analysis, confirmation and administrator approval still apply.

Making Cygnus the default changes only the user's own `~/.config/mimeapps.list` (through `xdg-mime`), remembers each previous
handler, and restores them when switched off; a type someone else changed meanwhile is left as they set it. A second launch
hands its arguments to the running window over the session bus (under the application's own name) and exits, before any
QML is loaded.

### Translations

A translation is a compiled Qt message file (`cygnus_<language>.qm`) loaded by Qt's translator: data, never code. `cygnus/gui/i18n.py` accepts the
language only as a plain tag, loads only a regular file (not a link, not a folder) of at most 8 MiB from the package folder,
`/usr/share/cygnus/translations` or the absolute folder the user names in `CYGNUS_TRANSLATIONS` (their own environment), and ignores a file Qt cannot
read. Labels show translated text as plain text, but some Kirigami components read markup (inline messages, placeholder explanations and dialog subtitles) and would interpret markup
written in a translation: a translation file is trusted like the software it comes with, so install one only from a source you trust.

### The Flatpak permissions viewer and the update icon

**Permissions** (`cygnus/core/flatpak_permissions.py`) is read-only: it reads the installed application's metadata through libflatpak and the
override files of the installation and of the user (`overrides/global` and `overrides/<application id>`), each only as a regular file of at most
256 KiB (a link into the person's own dotfiles is followed, a pipe, a folder or an oversized file is skipped and said so), parses them as
keyfiles with entry limits, and merges them in Flatpak's order. Names from those files are made printable and bounded before they are shown.
Nothing is written and nothing is run. The reading was compared by hand with `flatpak info --show-permissions` on two real installations (the tests use fakes, as they
cannot see the user's Flatpaks). An application installed both for the user and system-wide is shown as its per-user copy (the one `flatpak run` starts) unless the
registry names the system copy.

**The update icon** (`cygnus/core/tray.py`, `cygnus/gui/tray.py`) is a separate unprivileged program started from an autostart entry in the
user's own `~/.config/autostart`. It reads only the results the update checks already stored (the registry's last known update states and
`~/.cache/cygnus/system-updates.json`, which is size-capped and must parse as a JSON object with a list of packages); it opens no network
connection and does not talk to the helper. Switching on writes the entry (never through a link or over a folder) and remembers the choice in
the preferences; switching off removes the entry only if it carries Cygnus's marker (a link is removed, never what it points at). One copy runs
at a time (a lock file in the runtime folder, or in the person's state folder when there is none; never `/tmp`), it quits within a minute of the choice being switched off (it looks once a minute), and it
leaves politely when there is no tray. The system-update count is dropped once pacman's package database changed after the check that made it.

### Choosing the default browser

`cygnus/core/defaultbrowser.py` changes the user's own browser settings and nothing else: the browser lines of
`~/.config/mimeapps.list` (`xdg-settings set default-web-browser`, which also writes the `about:` and unknown-scheme lines
outside KDE) and, on KDE, `BrowserApplication` in `~/.config/kdeglobals`. No administrator action, no helper involved. One more thing
happens outside KDE: `xdg-settings` itself (xdg-utils, not Cygnus) may add missing types to the user's own menu entry for that browser and write it
with owner-only permissions; on KDE it does not (checked with the real tool). Cygnus does not undo that, and an entry it manages then reads as
"changed after Cygnus wrote it" and is kept when the program is removed.
- The menu entry name is validated (`[A-Za-z0-9][A-Za-z0-9._+-]*.desktop`, never starting with a dash or holding a path) before
  it reaches `xdg-settings`, and the entry must exist in the user's or the system's application folders, claim `http`, `https`
  and `text/html`, not be hidden, and start a program that exists. Names read from a menu entry are stripped of control
  characters before they are shown.
- `xdg-settings` is run with a minimal environment plus the session variables it needs to know the desktop
  (`XDG_*`, `KDE_SESSION_VERSION`, `KDE_FULL_SESSION`, `DESKTOP_SESSION`); without `KDE_SESSION_VERSION` it fails and never
  writes KDE's setting, so the module passes it on (a test proves the difference).
- The result is **read back** from the default-applications list, `gio mime`, KDE's own key and `xdg-settings check`; a desktop-specific list
  in the user's settings folder that overrides the choice is named in the message. If anything did not take, every setting is
  put back to exactly what it was before the attempt, including the lines `xdg-settings` left behind when it failed (the menu-entry rewrite described above is not undone).
- What was set before the very first choice is remembered in the preferences file (never overwritten by later choices) and put
  back by "undo" only where the list is still exactly what the choice left (Cygnus keeps that too): a setting the user changed since, or a name added
  to or taken out of the list since, makes the whole list the user's, and it is left as set. If an undo cannot write, the memory is kept so it can be
  tried again. Names are kept as GLib reads them, including ones a menu entry's own id could not have (an `@`); a malformed file is read more
  leniently than GLib reads it (a stray line without `=` does not hide the file from Cygnus, though GLib would ignore the whole file), and the final
  read-back through `gio mime` is what decides success.
- When Cygnus writes the menu entry of a program that files itself as a web browser but leaves `http`, `https` or `text/html` out
  of its `MimeType`, it adds them (and says so): an entry that does not claim the types cannot be chosen by the desktop.

### Other package managers

pacman allows one transaction at a time. Before running a plan, the helper:
- **Waits for the lock.** While another program holds pacman's lock, the helper waits and names
  it, e.g. "Waiting for paru to finish…". Holders are found by which process has the lock file open
  (libalpm keeps it open during a transaction), so a libalpm program Cygnus has never heard of
  still counts.
- **Reports stale locks.** A lock that no running package manager holds is reported, never
  removed.
- **Refuses outdated plans.** If your installed packages changed after the plan was made, the
  helper refuses it and quotes what pacman's log says happened ("pacman -Syu (upgraded 12)").
  You then review a fresh plan.
- **Holds pacman's lock itself** while it installs a planned database snapshot.

**Stale pacman lock** (`PlanClearStaleLock`, `recovery.manage`, password every time). The helper
removes `/var/lib/pacman/db.lck` only if:
- no package manager is running;
- at commit time, the lock is still the very file you approved, with the same inode and
  modification time. A new, live lock is never removed.

**Not implemented yet** (designed in architecture §16.3): `PlanStore` and `PlanFlatpakInstallation`.
Cygnus therefore offers no root-owned stores on other drives and no custom system Flatpak
installations. On this machine's NTFS drive neither would be allowed anyway.

**System update checks** run without root. Fresh databases are downloaded into Cygnus's own cache
(the way `checkupdates` does it); `/var/lib/pacman` is only read.

**Arch news** (`cygnus/core/news.py`). Before a system update the Arch news feed is read from `archlinux.org` only (HTTPS, redirects
checked hop by hop against that host, 1 MiB cap; the update waits at most 20 seconds for it, in the terminal too). The document is refused if it contains a document type or an entity
declaration, in any encoding (expat's own handlers, after a first look at the bytes; nothing in a feed needs either, and both are how XML is attacked), at most 60 items are read, every text is reduced
to plain text (scripts and styles dropped, non-printable characters replaced), and an item's link is kept only if it is an ordinary
`https://archlinux.org/...` address; it is only ever opened by a click. "Since your last upgrade" is read from the tail of
`/var/log/pacman.log` (world-readable). Every item that says it needs a manual step is among the items shown (wherever it comes in the feed, and the wording is searched in the
whole text, not the shown part), so the acknowledgement is never for something unseen. A feed that cannot be read never blocks an update: the answer says so.

**Snapshot note** (`cygnus/core/snapshots.py`). Only looks: the hook files pacman will really use (the hook folders in `pacman-conf`'s
order, the shipped folder last; a link to `/dev/null` switches a hook off), `/etc/snap-pac.ini` and Snapper's list of configurations
(`/etc/conf.d/snapper`), the way snap-pac itself reads them. snap-pac counts as installed only when the hook its package ships is there.
Cygnus runs no snapshot tool: a Snapper snapshot taken from the helper, as root, would be code nobody could try on a machine that
already has snap-pac, so it was not built. The acknowledgement
for an item that needs a manual step is a courtesy to the person at the keyboard (a disabled button, a terminal question that
`--yes` does not answer), not a security boundary; the helper does not know about it.

### Cleaning pacman's package cache (a helper action that deletes cache files)

`cache.clean` is the helper action that deletes files from pacman's package cache, and it does so itself (there is no command line: no
`pacman -Sc`, no `paccache`; the helper's other deletions are its own staged package copies and a stale `db.lck`), so it is built
narrowly (`cygnus/core/cacheclean.py`, `plan_cache_clean` / `clean_cache` in `cygnus/helper/actions.py`, D-Bus `PlanCacheClean(y, b)`):
- **A client gives two choices and nothing else**: how many older versions to keep (0 to 10) and whether packages that are not
  installed lose all their cached versions. Anything else is refused. The helper reads `pacman-conf` and pacman's own database itself and
  chooses the files; it never accepts a path, a name or a pattern from a client.
- **Which folders**: only the `CacheDir`s `pacman-conf` names, and only a folder that is an absolute path with no odd part in it,
  reached with no link anywhere on the way (each component opened with `O_DIRECTORY|O_NOFOLLOW`), root's own, and not group- or world-writable. A folder that does not
  pass is left alone and said so (the same rule keeps a downgrade from using a user-writable folder). A folder that also holds a repository
  database (`*.db`, `*.files`) is a local repository used as a cache folder, and is left alone too: its database still lists the packages;
  so is a folder with more than 40,000 entries (too many to judge safely). A folder is judged whole or not at all. Every folder on the way
  to the cache folder must be root's own and not writable by anyone else (a sticky folder such as `/tmp` is allowed), the path is walked
  one component at a time with `O_NOFOLLOW`, and when someone other than root asks for a plan, a folder they could not look into
  themselves is refused (what is in it is not theirs to learn from a plan).
- **Which files**: regular files only (never a link, a folder such as `download-*`, or a `.part` file) whose name parses as
  `name-pkgver-pkgrel-arch.pkg.tar[.ext]` for a valid package name, and the `.sig` next to such a package. Nothing else is ever touched.
- **What stays**: per package and architecture the installed version, always, plus the `keep` newest others, ordered by libalpm's own
  version rules in one question to the worker (never by file date). A group whose order does not match exactly the cached versions is
  skipped rather than guessed at.
- **Shown, then removed**: the plan holds the exact list (folder, name, device, inode). At commit the helper waits for pacman's lock, takes
  it (`db.lck`, like the sync-database install does), chooses AGAIN with the database as it is then, and removes a file only if it was in
  the plan, is chosen again, and is still the same regular file; each file is removed with `unlinkat` relative to the folder's descriptor,
  so a link put in its place is never followed. A package installed in between can only make the list shorter.
- The polkit action is `auth_admin` every time, never `_keep`; the message names the count, the size and the folder.

### Discover (browsing what can be installed)

Discover only LISTS (`cygnus/core/discover.py`, `cygnus/core/vendor_catalog.py`, the `discover_*` and `fetch_vendor_package` functions of
`cygnus/gui/service.py`). It adds no administrator action and no new way to install: every choice is handed to a flow that has its own
plan and confirmation. What it reads and how:
- **AppStream files are hostile input.** Flathub's file and the Arch data package's are read as a stream with `xml.parsers.expat` (never
  as one document in memory): a document type or entity declaration is refused in any encoding (so no entity expansion), at most 60,000
  applications (240,000 components of every kind) and 768 MiB uncompressed are read (a file with more is refused as a whole, as is one nested more than 32 levels deep), the
  default description, name and summary are taken (a translated variant is never read, whether it is a whole `<description xml:lang>` or a `<p xml:lang>` inside the default text), every field is cut to a limit, text is reduced to printable plain text, and only
  application components are kept. A data file must be a regular file opened without following links and without blocking on a pipe;
  one that cannot be read costs only itself (it is reported, the others are still listed).
- **Icons** are used only as a bare file name of a known kind (`png`, `jxl`, `webp`) looked up in the size folder the data names, and only
  if it is a regular file, not a link. **Websites** are kept only if they are well-formed `https` addresses. **Screenshots are not fetched**:
  the page makes no request to anything while you browse. (Cygnus's existing hosts list is unchanged.)
- **Merging is by exact identity only.** Two sources are one program only when they have the same AppStream id, or when the maker's package
  is mapped to a Flathub id in a hand-checked table (`vendor_catalog.py`, kept equal to the feeds list by a test and to the verified ids
  by another); never by a similar name. Google Chrome's beta channel has no Flathub id on purpose.
- **A maker's package is downloaded, not converted, from the page.** `fetch_vendor_package` uses the same vendor package lists as the update
  check (HTTPS only, capped, strictly validated), downloads over HTTPS into a folder of its own (`~/.cache/cygnus/downloads/vendor/<program>`,
  mode 0700; only an earlier download of that program is removed, at most three programs' downloads are kept, nothing is deleted
  through a link, and the Updates page's folder is never touched), checks the SHA-256 the maker publishes (the `.deb` has one; the `.rpm`
  address has none, and the page says that only the secure connection vouches for it), and shares the one-download-at-a-time lock with the
  Updates page. The file is then opened on the Install page, where it is analysed and confirmed like any file you chose.
- The catalogue is built off the window's main thread and cached by each file's identity (path, size, date, inode); what is typed in the
  search box is plain words (at most 8), never a pattern, and every argument from the window is checked in `service.discover_*`.

### The Packages page (repository packages by name)

Search, the installed list and a package's details are read-only: three more operations of the same crash-isolated libalpm worker
(`search`, `installed`, `details` in `cygnus/core/backends/alpm_worker.py`), which works on a private scratch database folder whose
`local` and `sync` entries are links to the real ones (nothing is written). The search is Cygnus's own substring search, never
libalpm's, which takes regular expressions: what is typed is plain text, at most 8 words of 100 characters, and results are capped
(200 matches, 200 entries per list, 3,000 file names; the answer says how much was left out). Text that comes from a package
database (names, descriptions, URLs, file names) is made safe in the worker: every character that is not printable is replaced by a
space, so an escape sequence or a direction mark cannot reach a terminal or move text around on screen.

No new administrator action was added. Install and Remove ask the helper for the same package plans as everything else
(`pacman -S --noconfirm --needed -- NAMES`, `pacman -R --noconfirm -- NAMES`, never `-s`, `-c` or `-d`: pacman itself refuses to remove
what another package needs). The page's own checks (the name is valid, the package is offered or installed, nothing needs it, it is
not protected) exist to say why in words; the helper repeats every rule that matters and the page's answer is never trusted: names
must match the helper's pattern, protected packages and `HoldPkg` are refused, the partial-upgrade blocker applies (installing with
updates waiting is refused, and the page then points at the Updates page), and removing a package the ledger does not hold needs
`confirm_not_installed_by_cygnus`, which the page sends only after a box ticked on purpose (the terminal: its own flag, never `--yes`).
Search reads the system's current package lists as they are and shows their age; Cygnus never refreshes them alone (a refresh
without an upgrade is a partial upgrade). Removing everything no longer needed is a package removal like any other (`pacman -R --noconfirm -- NAMES`, up to 200 names, one
question to the worker for all of them): protected packages, packages another installed package needs (unless it is removed in the same
plan), and packages Cygnus manages as applications are refused, and a package Cygnus did not install needs the explicit confirmation
(one for the whole plan, which lists the names). "No longer needed" is what `pacman -Qdt` lists; it is advice, not proof (a tool you use
by hand, or built something with, can be on it), which the screen says.

Going back to an older version (`cygnus/core/pkgcache.py`) adds no helper surface either: the cached versions are listed from
the cache folders `pacman-conf` names (regular files only, never links; names parsed strictly, then compared with libalpm's own
rules in one worker call), and the chosen one goes through the existing package-file plan. The window names a package and a version,
never a path: the file is found again at the moment of use, hashed without following links, and the helper's reading of it (name and
version, through libalpm) must equal what was asked for. A protected package is refused (the helper refuses it too), and nothing is
recorded in Cygnus's application list. The helper does not check a package file's signature (it labels it UNSIGNED and shows the
SHA-256): the file's standing is that the cache is administrator-only and pacman put it there after checking the signature.

The helper takes one more request field, `claim_ownership` (default true): a package FILE that only replaces an installed package the
ledger does not hold (going back to an older version of a repository package) is then not entered in the ledger as installed by
Cygnus, so a later removal still asks about it. Only exactly `false` declines; a package the ledger already holds is still recorded
(its version follows), and a package that was not installed before is recorded as always.

A package installed here is recorded in the helper's ledger like any other and is not an
"application" in Cygnus's own list; and a package that IS one (a converted `.deb`/`.rpm`, a package file, an AUR build) is not removed
from this page, so that the application list never keeps an entry whose package is gone.

## 3. Everything from outside is hostile

**Package files.** AppImage, Flatpak bundle, `.pkg.tar.*`, `.deb` and `.rpm` parsers:
- cap every size and count: compressed and decompressed streams, members, headers;
- never follow symlinks out of an image;
- extract single named files with `unsquashfs -cat -no-wildcards`;
- turn any unexpected parser exception into a clean "cannot read this file".

**Archives that are programs** (`.tar.gz`, `.tar.xz`, `.tar.zst`, `.tar.bz2`, `.tar`; `cygnus/core/detect/archive.py`,
`cygnus/core/ops/archive_ops.py`). The archive is read once, as a stream, with Python's tarfile (names, types, modes, sizes and link
targets as stored), never unpacked to look at it. Caps: 200,000 entries, 8 GiB of file data, the decompressed stream, at most 16 menu
entries (64 KiB each), 32 icons (256 KiB each), 8 install scripts (64 KiB each). Refused, with a reason: an absolute path or a `..`; a
hard link, device, pipe or socket; a link with an absolute target or one that leads above the unpacked folder; the same path twice
(a folder twice is fine); an entry whose parent is a link written earlier; the marker's own name; source code; a system folder
tree; a FreeBSD package; nothing to run. Installing re-reads the file: its SHA-256 must still be the one analysed (carried from
the analysis), every entry is checked again with the same rules, every file is created new with `O_EXCL|O_NOFOLLOW`, modes are
reduced to 0755 (any execute bit) or 0644 and folders to 0755 (no setuid, setgid, sticky or group/world write), each link is checked
again by where it really leads on the unpacked tree, and the whole tree is unpacked into a hidden folder beside the final one
and renamed into place only when complete and equal to what the archive listed. The target drive's abilities
(permissions kept, rename, links if needed, file names it can hold) are checked first, and the system drive is used when the
chosen one cannot; too little free space stops the install instead (never a silent move). Nothing from the archive is run unless you tick one of its install scripts (see [Running install scripts](#running-install-scripts-an-opt-in)); they are shown in full, with what they mention. The folder holds a
marker file with the installation's id; **removal** only ever deletes a real folder (not a link) whose marker has that id, after the
person asked and was told how many files changed or were added since (a program that updates itself changes its own folder), and
never `/` or the home folder; the step is last and has no undo. A program from an archive runs as the person, unsandboxed, like any
download; its origin is not verified (no signature is checked), which the plan says. Libraries it needs are checked afterwards
with the same `readelf` analysis as for conversion.

**Subprocesses.** Always argv lists with no shell. The environment is sanitised (`LC_ALL=C`, safe
`PATH`), and both time and output size are bounded. The one exception is `xdg-open`, which hands a
store page to your browser: it needs your desktop session's variables, so it runs with them, and it
is started detached (only `https://` addresses) instead of being time-bounded.

**Downloads.** Only HTTPS is accepted, and a redirect to plain HTTP fails. Size and overall time are
capped. Digests are verified before a file is used, and partial files are deleted.

**Signatures.**
- AppImage OpenPGP signatures count only against a fingerprint **pinned in a trusted manifest**.
  The key embedded in the file supplies key material only. Revoked and expired keys fail.
- `gpg` always runs with a throwaway `--homedir`, never your keyring.

**AUR build scripts** run only after you review them, and never as root.
- **Static metadata.** Dependencies come from the static `.SRCINFO`;
  `makepkg --printsrcinfo` would already execute the PKGBUILD.
- **Review.** It shows every file, the changes since the version you last approved, and hints
  (pipes into a shell, `sudo`, decoding, sourcing other files, missing checksums).
- **Complete or nothing.** All text is shown with control and bidi characters escaped, so nothing
  can rewrite your terminal. The review is marked incomplete if anything can't be shown in full:
  - a link or a binary file;
  - hidden characters in code (the PKGBUILD, install scripts, anything `install=` names);
  - more than fits.

  An incomplete review is never built.
- **Build.** The build uses exactly the reviewed commit, as you, in a checkout where your git
  configuration and hooks do not apply. Installing the result is a separate `auth_admin` step.

**Converted DEB/RPM packages** carry no install scripts (unless you chose to run the vendor's, see below) and no setuid bits, and pacman owns every
file. Files go only to `/usr`, `/etc` and `/opt` (anything else is refused when the package is
analysed, and again when it is built).
- **Scripts are read first, and run only if you choose to.** Cygnus splits each maintainer script into its commands and
  recognises a small set of them (cache updates such as `ldconfig` and `update-desktop-database`,
  messages, systemd calls, Debian packaging helpers). Anything it cannot read with confidence blocks
  the conversion by default: command or process substitution, here-documents, functions, wrapper programs
  (`env`, `xargs`, `sh -c`, `exec`…), redirections to real files, a script in another language
  (found from its `#!` line, or from the RPM program tag), and any command it does not know. File
  commands on system paths, recursive removals, and user or group creation (including RPM
  `sysusers` entries) block it too. File commands elsewhere are shown to you for review.
- **Scripts that could not be read may be gone past knowingly, not run.** Unless you chose to run them, conversion drops every script, so
  nothing in one can execute either way. When the only reason is that Cygnus cannot read a script, the person
  may convert anyway after Cygnus shows what the scripts mention (a plain word search for notable commands;
  user, group, kernel-module and service commands are listed first and flagged), the whole script text, and a
  confirmation that they will not be run. A script is unreadable as a whole: a `groupadd` inside one is
  *mentioned*, not *found*, which is why the person has to see and confirm it. What Cygnus can positively see in
  a script it reads (user or group creation, kernel modules, debconf) and users or groups a package declares
  (RPM `sysusers`) still cannot be gone past, and neither can any other blocker (privileged files, unsupported
  folders, libraries nothing provides to a program that needs them). The tree that pacman will own is checked
  again just before it is built, whatever was confirmed.
- **Nothing is lost or changed silently.** The packaging tool's own defaults would delete libtool, `.pod` and
  `usr/share/info/dir` files and empty folders and rename man pages, so the converted package is built with those clean-ups
  switched off (`!purge !zipman libtool staticlibs emptydirs`). Two checks make this an enforced rule, not a hope: after
  unpacking, every non-folder entry of the vendor's file must exist in the tree (`foreign.not_unpacked`); after building, the
  package must hold exactly the tree Cygnus meant to ship, same files, links and folders, each file the same size
  (`convert.compare_with_tree`). Any difference stops the conversion with the paths that differ, and the package is not used.
  Folders nobody can enter (a vendor-packaging quirk, mode 0600/0644) are made enterable the moment a package is unpacked
  (`foreign.make_traversable`): before, the analysis silently skipped what it could not open, so a program hidden in such a
  folder was not looked at (its missing libraries went unnoticed) until the conversion step.
- **What Cygnus adds from the scripts, and what it never does.** Without running anything, Cygnus finds the icon
  (`xdg-icon-resource install`) and command-link (`update-alternatives --install`, `ln -s`) lines in the install scripts by
  a text search that skips here-document bodies, and adds only what passes strict rules checked against the package's own
  files: a link only as `/usr/bin/NAME` (a plain name) pointing at a file the package itself ships, written out literally
  (no variables), never over an existing file or one an installed package owns (`pacman -Qo`), never a Debian generic alias;
  an icon only for a name a shipped menu entry asks for, only square standard sizes read from the PNG's own header, at most
  twelve, only from the folder the script names, only into `usr/share/icons/hicolor`. No folder on the way may be a link
  (so nothing is ever written outside the package), and the final tree is scanned again after the additions. Every
  addition is listed in the analysis and in the conversion notes.
- **What the scripts would have done is listed, not done.** The install scripts are also read by a tolerant scanner (it
  follows `if`, `case`, functions, `&&` / `||` and loops, joins lines the way the shell does, and ignores here-document
  bodies and comments) that lists every file written, folder made, permission set, owner changed, link, command registered,
  icon, user or group, service, repository or key, download, kernel module and file deleted, with whether the path is written out
  literally and whether the step only happens under a condition (`if`, `case`, a function, after `&&`/`||` (also from
  the line before), inside a `while`/`until`, inside a `for` loop whose list is worked out while the script runs, or after
  an `exit` that ends the script quietly: status 0, or an unknown one; an `exit 1` that aborts the install does not count). Quoted words are
  never taken for shell keywords. The person can open that list on the Install page ("Show what the scripts would have done to your
  system"). It is a reading of text, never a record of what a script would really do. Of everything on it, only
  unconditional, literal steps that pass the rules above are reproduced: besides the menu icon and command link, an
  empty folder the script makes inside `/usr`, `/etc` or `/opt` (mode 0755) and a plain `chmod` (a number such as `755`,
  or `+x`, `u+x`) on a regular file the package ships. A permission that would make something writable by its group or
  by anyone, or that sets setuid, setgid or sticky, is never copied, and the same is enforced on the finished tree: no
  setuid or setgid bit, and no write bit for group or others, on any file (a folder's setgid bit is cleared too).
- **The package's own file list is parsed strictly.** The vendor's `bsdtar -tvf` listing is what the size bounds
  (4 GiB, 200,000 entries) and the "nothing unpacked was lost" check are made from, so a listing line that does not have
  exactly the expected shape (an owner name with a space in it moves every column) stops the conversion instead of being
  guessed at. A library counts as bundled only when it is a real ELF file in the package (or a link that ends at one, absolute
  link targets read as paths inside the package), so a dangling link or a text file with a library's name does not hide a
  missing library. Files under `/etc/grub.d` (run as administrator whenever the boot menu is rebuilt) block like the other
  files that run as administrator.
- **Cygnus never runs a script to find out what it does (a decision, not a gap).** Running a maintainer script in a sandbox
  to watch it was considered and rejected. Debian's scripts expect Debian's own tools (`dpkg`, `debconf`,
  `update-alternatives`, `adduser`), so on Arch they would mostly fail or need imitations that make the observation a guess; what
  they could tell that the text reading above cannot (files generated at run time) is small; and every sandbox is a promise that
  a kernel or sandbox flaw can break. If a package needs a step Cygnus cannot read, it says so and shows the script. A script is
  run only when you choose to run it for real: see below.
- **Updates of converted programs download, they do not install.** For the vendors that publish an apt `Packages` list (a built-in list of fourteen well known
  vendors, plus any you add yourself with `cygnus feed add`, which must be https and is read once before it is kept) Cygnus reads the newest version from it over HTTPS (TLS only; the list is decompressed with a
  size cap, every field is validated, and a file name that leads outside the vendor's own folder is refused). "Download and review"
  fetches that one file, checks its SHA-256 against the list (an rpm comes from the vendor's fixed "current" address and has no
  published checksum), keeps only that file, in a folder of its own under `~/.cache/cygnus/downloads/updates` (earlier ones are removed; a download folder that is a link is refused and never emptied; one download at a time; the partial file is never written through a link), and opens it on the Install page: it then goes
  through the whole analysis and the confirmation like any file, so an update can never skip a check or a question. A check that
  fails is "unknown", never "up to date".
- **Optional parts of Cygnus itself** (Settings → Optional parts) are installed through the same helper plan as any repository
  package, and only the five packages Cygnus lists (`fuse3`, `fuse2`, `zsync`, `base-devel`, `kservice`) are accepted: any other
  name is refused before the helper is asked.
- **The AI assistant (optional, off by default) cannot act.** It is a source of suggestions, never of commands. Cygnus
  itself downloads the public pages (https only; the program's own vendor host and its subdomains, or exactly the forge project it
  names; only public internet addresses, checked again on every redirect on the host that is really connected to, with an address
  that two readers could understand differently refused; no `.netrc` login is ever sent; the whole request has a time limit that a slow
  trickle cannot get round; because Cygnus does the downloading and not the service, a page cannot send anything to the person's own
  network or to the hosting provider's internal services). The assistant receives the program's name, id and format and that text, and no private data (a test
  checks the exact message). The pages are placed in the message as data between markers and the model is told to ignore any
  instruction in them; because that cannot be relied on, nothing from the answer is trusted: each item must carry a quote that is
  literally present in the named page, and must be an Arch package that exists under exactly that name (a package that merely
  provides the name does not count; the AUR is shown with its review), the `input` group (the helper's own allow-list), a systemd unit
  name the helper itself accepts, a browser-extension store page, or a plain note; anything else is dropped. Text from the
  model is stripped of control and bidirectional characters and shown as plain text. A suggestion is acted on by its token: Cygnus
  remembers what it checked for thirty minutes and plans exactly that through the helper, with the usual confirmation, so a window
  cannot make the helper do anything the checks did not allow. Keys live in KWallet (never a file or a command line) and go only
  in a request header to the provider chosen; a key for a server the person named is kept under that server's address, so changing the
  address never sends the old key to the new one, and a key for a local server is never handed to a proxy. The hosted service (`server/ai_worker`, a Cloudflare Worker) holds Cygnus's key as a Cloudflare secret, accepts only that
  structured request, builds the message itself, answers only in that shape, limits requests per person (by internet address,
  an IPv6 address counting as its whole /64) and for everyone, turns away an address that sends too many requests at all (answers from
  the cache included) before they reach the Durable Object, and remembers identical questions; its limits and cache live in one
  Durable Object, so the counts are exact, and a refused request writes nothing. It keeps no log of requests. Cygnus re-checks its
  answer like any other, so a compromised service can at worst make a wrong suggestion that is dropped, or shown unapproved with a
  quote that is not on the page. The host (Cloudflare) sees the sender's internet address, as any website does, and Google may keep
  the public text under the terms of its free Gemini service.
- **The notice that a newer Cygnus exists** (`cygnus/core/self_update.py`) asks GitHub's list of releases (`api.github.com` only: a
  redirect elsewhere is refused), accepts only plain version tags and this project's own release-page addresses from the answer,
  compares with the installed version, and keeps the last good answer. It downloads and installs nothing: Cygnus does not update
  itself, because its package contains the administrator helper and its releases are not signed.
- **Uninstall scripts and `/var`.** `rm`, `rmdir` and `unlink` in an uninstall script (prerm, postrm, preun, postun) are
  ignored, because by default those scripts are not run and what they clean up was never made; the same commands in an install
  script still block. A `/var` that holds only empty folders is left out; one with any file or link in it is refused.
- **Missing libraries.** A library is *required* when a program of the package (a file with a program
  interpreter) loads it, directly or through a library of the package; a library only a file nothing loads at
  start (a plugin or shim that is loaded on demand) wants is *optional*. A package with no program of its own, or
  one with too many files to check, gets no such leniency. Providers are found by the declared "provides", and
  otherwise by file name in pacman's file lists, kept in Cygnus's own copy of the databases and fetched without
  administrator rights. The names come from files nobody has vetted, so only well-formed library names are
  searched for (never one that starts with `-`, and after `--`), and a package the lookup names is used only if the
  repositories really offer it. Optional libraries are never installed unless ticked; they then become real
  `depends` (installed by the helper, shown in the authorization dialog) and otherwise `optdepends`.
- **Files that run as administrator after install are refused.** Cygnus reads pacman's own hooks to know
  what they apply as administrator the moment a package is installed, and refuses those files:
  `sysusers.d`, `tmpfiles.d`, `binfmt.d`, `sysctl.d` and `modules-load.d` files, and plugin libraries
  in the folders that hooks load while installing (GIO modules, GTK input methods, gdk-pixbuf
  loaders, VLC plugins). It also refuses pacman hooks themselves, `ld.so.preload`, sudoers and
  polkit rules, PAM, certificate stores, login-shell scripts, boot-image and NetworkManager
  dispatcher hooks, udev rules that run a program, modprobe `install` rules, systemd generators,
  `*.wants` links that turn a service on, systemd's own settings in `/etc/systemd`, a unit in
  `/etc/systemd` that would replace one this computer already has, and a service drop-in for a
  service the package does not provide. Plainer cousins (a vendor's own service in `/etc/systemd`,
  udev permission rules, kernel module options, D-Bus policy, autostart entries, units) are listed
  for your review. The tree that pacman will own is checked again just before it is built.
- **What you confirm is what is converted:** the analysis carries the file's SHA-256, and a file
  that changed afterwards is refused.

### Running install scripts (an opt-in)

Until 0.1.2 no install script was ever run. Since then a person can choose to run the scripts that come with a file. This was a
change of the project's rule made at the user's request (2026-10-10), and it is the one place where Cygnus runs code that came out
of a file it did not make. **Nothing runs by default**, and nothing runs for a script the person was not shown.

What is offered, and how each one runs:

| Where the script comes from | Who runs it | How |
|---|---|---|
| A tar archive (Discord's `postinst.sh`, an `install.sh`) directly in the program's folder | **you**, or **administrator** | As you: Cygnus starts the interpreter itself, after the install, in the program's folder. As administrator: through the helper (below). |
| A `.deb` or `.rpm` | **pacman, as administrator** | The vendor's scripts are written into the converted package's pacman install file (`.INSTALL`), so pacman runs them inside the install the person already approves, with the arguments Debian or rpm would have given them. |
| An Arch package or an AUR build | pacman, as administrator | Unchanged: pacman always did this, and the approval dialog says "it runs an install script as root". |
| AppImage, Flatpak | nothing | They have no install scripts. |

What is guaranteed for every script:
- **Off by default, for every file.** The box starts unticked and is reset when another file is opened. It is only there while the script's
  full text is on screen, with what it mentions, and hiding the text takes the choice back; what will run is written next to the Install
  button. (Static tests pin all of this: `tests/test_scripts_qml.py`.) `-y` in the terminal never implies it: `--run-scripts` does, and
  `--scripts-as-admin` is refused without it.
- **A script is only offered if it can be shown exactly as it would run.** It must be valid UTF-8 with no zero byte and none of the
  characters that cannot be seen or that move text around (a carriage return, zero-width and direction marks, a byte-order mark, a
  no-break space, other control characters; only the newline and the tab are allowed besides ordinary characters), must not start with a
  dash (a text that is itself an option, like `-x`, would make the shell run the file on disk instead), and must name an interpreter
  Cygnus knows: `sh`, `bash`, `zsh`, `python3`/`python`, `perl`. The first line is followed as it says or the script is refused:
  the program must be in `/bin` or `/usr/bin` (or found by `env` with no options), and at most one option cluster of a short list
  may follow (`-e`, `-x`, `-u`... for shells, `-u`, `-O`... for Python, `-w` for Perl); `#!/bin/sh -e` runs with `-e`. It must be shown
  whole: an archive's scripts up to 64 KiB are shown in full; a deb's or rpm's up to 60,000 characters, and one the reader only kept
  the start of (`uninspected_scripts`, which includes a maintainer script that is a link) is never written into a package. An
  archive's install scripts are the ones directly in the program's folder (a file called `update` deeper down is part of the program),
  at most eight. Anything else that looks like a script is listed with the reason and never offered: none is dropped without a word.
  Running the vendor's scripts for a deb or rpm also needs the checksum of the analysis that was read, so the scripts that run are those
  of the file that was shown; and a package that *declares* users or groups (rpm `sysusers`) is still refused, because running its
  scripts would not create them. For an rpm the program the package declares is used, and a first line that names another one refuses the script.
- **The bytes that run are the bytes that were shown.** The archive reader records the SHA-256 of each script's raw bytes; the choice
  carries it; before running, the file in the program's folder is read again without following any link on the way and must match.
  As you, and as administrator, the text goes to the interpreter on its command line (`interpreter [options] -c TEXT NAME`), so there is
  no file to swap between the check and the run and the script's standard input stays free. For a deb or rpm the text is written
  out of the install file byte for byte (a test compares the bytes) into a private temporary folder and run from there.
- **Once.** As you, a marker is taken before the run, so a step that was interrupted is never repeated by a resume; a script that may
  have half-run is not run a second time. As administrator, a plan is single-use: it runs once, and running it again takes a new
  confirmation and a new password. (A deb or rpm script runs when pacman installs, upgrades or removes the package, as pacman decides.)
- **A failing script never undoes the install.** Scripts run last, after the program is recorded. One that fails, times out (300 s as
  you; 600 s as administrator, and in a deb/rpm's direct fallback) or is refused is reported ("The program is installed, but the script
  ... did not finish: ...", and a non-zero exit status 2 in the terminal) and nothing is rolled back. There is no undo for what a script
  did. pacman ignores the exit status of an install script, so a deb or rpm script that refuses (exits with an error) cannot stop the
  install or the removal; its message is shown.
- **The time limit holds.** As you, the limit is enforced from outside: a program the script started and left running (even in a session
  of its own) never makes Cygnus wait, though one that called `setsid` is not stopped when the time is up (the script's own process
  group is). A script that starts a program on purpose and ends is allowed to: the program is not stopped, and what it prints is thrown away.
- **A clean environment.** As you, a script sees your home, name and desktop session variables and a plain `PATH`; nothing else of
  Cygnus's environment (a token in it is not handed over). What it prints is kept to the last 16 KiB, with control characters removed.
- **Removal scripts** (`prerm`, `postrm`, `uninstall`) run first when the program is uninstalled, while the folder they live in exists.
  The window offers them to run as you; the terminal can also run them as administrator, after the removal has been confirmed. They are
  not runnable from the "Install scripts…" dialog, which is for install scripts. For a deb or rpm they run when pacman removes the
  package, if it was converted with them. **On an upgrade pacman runs only the new package's pre- and post-install steps**, so the old
  package's removal scripts (Debian's `prerm`/`postrm upgrade`, rpm's `preun`/`postun` with 1) are not run, and the screen says so.

**The administrator action of the helper (`PlanScript`, polkit action `scripts.run`)** is the part to audit first:
- It is the only way the helper runs text it was handed. The client sends the script's bytes and the checksum it says it showed; the
  helper refuses unless they match, then keeps the bytes in the plan (the client cannot change them afterwards). It refuses names with
  control characters, a script over 64 KiB or that is not shown-exactly text, an interpreter or first-line option it does not follow,
  and a folder that is not a plain absolute path (no trailing slash, `.`, `..` or `//`), not a real folder (a link is refused) or not owned
  by the person who asked (so `/tmp` and system folders are refused). The folder's device and inode are noted, and checked again right
  before the run (a best effort: the unit's own start resolves the path again).
- The plan is the usual single-use, ten-minute plan bound to the caller's D-Bus connection and user id, with the usual limits on pending plans.
- **The helper cannot know that anything was shown.** Any process running as the same user can call `PlanScript` with a script of its own
  and a matching checksum; the only gate is the password dialog. So the dialog's message says what the script is (name, size, the program
  it runs with, its first line, its full checksum) and that the person should only go on if they just asked Cygnus to run it.
- Authorisation is `auth_admin` and **never** `auth_admin_keep`, so a password is asked for every script and nothing is cached for another
  program on the same session to reuse.
- At run time the helper builds the one command from the plan's own bytes (re-hashed first) and checks the command against the plan. `systemd-run`
  is not in the helper's general allow-list; only `check_script_command`, for a script plan, lets it through, in exactly that shape: a fixed
  set of flags, a fixed set of environment variables (`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, and `SUDO_USER`/`SUDO_UID`/`SUDO_GID` as
  `sudo` would set them), the plan's own folder, an interpreter from a short fixed list and the script text.
- **The script runs outside the helper's own restrictions** (a transient system unit started by systemd: no private `/tmp`, no read-only
  `/home`), because that is what someone who chose to run it as administrator expects, and a script such as Discord's cache clean-up
  would otherwise appear to succeed and change nothing. The unit stops itself after 600 s. This is a deliberate weakening of the
  helper's containment for this one action: a script you chose can do anything root can.
- What a script prints is read as bytes by the helper: a line is cut at 2,000 bytes (the rest of it is dropped as it arrives, never held),
  1 MiB is passed on and the rest read and thrown away, and the ledger keeps 500 lines with the script's name and checksum but not its text.

What this does **not** do, and cannot:
- It is not a sandbox and not a security boundary. A script you choose to run can do anything your account (or root) can. Cygnus
  cannot tell who made an archive; the plan says so every time.
- Reading a script's text (the list of what it mentions) can miss what a script builds at run time, and can over-report. It helps a
  person decide; it is not a verdict.
- Scripts written for Debian or Fedora expect that system's own tools (`dpkg`, `debconf`, `update-alternatives`, `adduser`). One that
  cannot find them fails (and the install goes on). `config` and trigger scripts, and scripts for lua and the like, are left out and said to be.
  The script runs from a temporary path, not from `/var/lib/dpkg/info/<package>.postinst`, so one that looks at `$0` sees that path.
- A deb or rpm script run by pacman cannot be shown a second confirmation at the moment it runs: the person confirmed it, with the text
  in front of them, when choosing to convert; the helper's approval dialog says that the package runs an install script as root.
- A script run through the long-running pool of the window can occupy one of its three threads for up to five minutes.
- Running scripts never lets a package through that is refused for what it ships (files outside `/usr`, `/etc` and `/opt`, setuid files,
  udev rules that run programs): only blocks that were about the scripts themselves are lifted. (The analysis still treats `rm` in a
  removal script as clean-up and does not block on it; when the box is ticked the screen's list of what the scripts mention includes it.)

- A manifest cannot express a command (see the [manifest spec](manifest-spec.md) §7).
- Vendor instructions such as `curl … | sudo bash` are quoted to you, never followed.

## 4. Manifests

- Only manifests bundled with Cygnus (`curated`) are used today, and only they may propose fixes.
  You approve each one.
- Manifests signed by a key bound to the vendor's domain (`vendor-signed`) are specified, and the
  loader can check them, but nothing downloads them yet: no vendor key is pinned and no manifest
  from the network or from disk is used for fixes. The loader refuses to give a trusted key to a
  different owner.
- A manifest's `expires` date is enforced by the loader. Rollback protection by `serial` is in
  the loader too, but the highest accepted serial is not stored yet, so it is not active.
- Identity hijacking is blocked in the loader: a vendor key can only claim identifiers under its
  own domain.

Details: [manifest-spec.md](manifest-spec.md) §3.

## 5. Flatpak

- **Repositories.** A bundle's `RuntimeRepo` is never added. Cygnus fetches that small
  `.flatpakrepo` description (HTTPS, at most 64 KiB) only to see whether you already have the
  repository. If you don't, it explains how you could add it yourself. Cygnus resolves the runtime
  itself, from repositories you already trust.
- **Copying a remote.** When the system has Flathub but your personal installation doesn't, Cygnus
  copies that remote with **its own signing keyring**. Unsigned repositories are never copied.
- **Obsolete runtimes.** An end-of-life runtime is installed only with explicit approval, and never
  substituted with a different branch.
- **Second drives.** Your *user* installation can be stored on another drive. Only its `repo/`,
  `app/`, `runtime/` and `.removed/` folders move. Cygnus restores these links if another tool
  removes them (`flatpak repair` does). Portal permissions and overrides stay on the system drive.
  No root process touches the relocated folders.
- **Moving Flatpak's storage loses nothing.** A folder is copied, the copy is checked against what the folder
  looked like when it was copied, and only then is the folder renamed and linked to the drive, back to back.
  The old folder is deleted last, only if it still matches the copy (all of them are checked before any is
  deleted, and each is renamed before it is removed). Undoing a move that was interrupted puts a folder back only
  when the old copy is certainly complete and the drive copy has nothing more. If the drive is not connected, a
  folder was created again meanwhile, or a copy was written to after the link went live, **both copies are kept**
  and you are told where they are; nothing is deleted on a guess.

## 6. Second drives (NTFS and other non-POSIX filesystems)

- **Ownership can be forged.** Anyone with the disk can edit NTFS ownership offline, so such
  drives hold **only user-run payloads**: AppImages, portable apps and the user Flatpak
  installation.
- **Nothing root-owned goes there.** Nothing root-owned or root-executed is ever placed there.
- **Mount options.** `nosuid,nodev` is recommended for `/mnt/data`.
- **Offline drives.** Launchers check `/dev/disk/by-uuid` without touching the mount point. If the
  drive is absent, they show a notification instead of failing silently or waking an automount.

## 7. Undo and recovery

- **Journal and rollback.** Every change Cygnus makes in your account is a journaled step with a
  compensation. If a step fails, completed steps are undone in reverse.
- **Interruptions.** If Cygnus is interrupted, the next start offers to finish or undo. A per-operation
  lock (released by the kernel when a process dies) makes sure an operation still running elsewhere
  is never touched.
- **Only unchanged files are removed or restored.** Cygnus removes or restores a file only if it is
  still byte-for-byte what Cygnus wrote. Payload deletion is always the last step.
- **Irreversible steps last.** Flatpak uninstalls and the previous version's deletion after an
  update run only after everything else succeeded.
- **What Cygnus never does:** format, partition, edit the bootloader, delete user data without an
  explicit request, or remove anything it did not create.

## 8. Tests

The suite never touches the real system:
- XDG, runtime and Flatpak user/system directories are redirected per test;
- GnuPG homes are throwaway;
- the helper runs on a private bus with a fake polkit.

The hostile-input regressions live in `tests/test_hostile.py` and `tests/test_review2.py`.
