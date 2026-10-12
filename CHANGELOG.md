# Changelog

## Next (planned 0.2.0, in progress) — what other package managers have

- **Translations, ready for translators.** The text on the screens is now translatable (387 texts, about seven in ten of the pieces of text on the screens; the rest is
  built from pieces at run time and stays English for now), with a template (`cygnus/gui/i18n/cygnus.ts`), tools (`packaging/translations.sh`) and a loader
  that picks the language of your desktop (or `CYGNUS_LANGUAGE`). Cygnus ships no translation and nothing is machine-translated: add a language
  by following `docs/translating.md`. Errors, plans and the terminal are produced in Python and stay in English. A test keeps the template equal to
  the screens' texts.
- **Flatpak permissions, in plain words.** **Permissions…** on a Flatpak's page (and `cygnus permissions ID`) lists what the application may do
  outside its sandbox, grouped, with what lets it leave the sandbox marked first and the changes you made with Flatseal or `flatpak override`
  applied the way Flatpak applies them and labelled as yours (the escapes Flatpak 1.17 added, `host-root`, `!host:reset` and `if:` conditions included). Read-only: Cygnus shows permissions, it does not change them.
- **An update icon in the system tray (optional).** **Settings → Update icon** (or `cygnus tray on`) starts a small separate program at login
  that shows an icon while updates are waiting and says how many. The icon reads only the stored results of the last checks (no network, no installs,
  no root helper; its menu's "Look for updates now" runs the normal background check). Clicking it opens Cygnus; switching it off removes it.
- **Discover: browse what you can get.** A new **Discover** page (and `cygnus discover search | info`) lists the programs on Flathub
  and your other Flatpak remotes, in your repositories (when the data package `archlinux-appstream-data` is installed: a new optional part)
  and the ones their makers publish as a `.deb` or `.rpm` (14 known programs from nine makers, plus your own sources, including stable Visual Studio Code),
  and **the makers' own downloads from GitHub**: an AppImage, `.deb`, `.rpm`, archive or Arch package for 24 hand-checked programs (Helium, Zen Browser,
  Obsidian, Heroic, RustDesk, Joplin, DBeaver, FreeCAD and others; 21 of them are merged with their Flathub entry by exact id). A download button asks GitHub
  for the newest release only when pressed, picks the one matching file (none or several is an error in words), checks GitHub's published SHA-256, and opens
  the file on the Install page; these downloads are not watched for updates, and a card says so when a program of that name is already managed by Cygnus (a second copy would replace the first one's menu entry). A file inside Cygnus's own download folder is now copied, never adopted in
  place (it would have been deleted by the next download's cleanup). Search
  by words, filter by kind or by how it can be got, and open a program to see every way to get it, in plain words. The same program from
  several places is one entry, merged only by its exact id (or a hand-checked Flathub id for a maker's package), never by a similar name.
  Nothing is installed from Discover: a Flatpak goes to the Install page, a repository package to its page in Packages, and a maker's
  package is downloaded over HTTPS (the `.deb` against the checksum the maker publishes; the `.rpm` has none and the page says so) and opened
  on the Install page for the usual look before anything is converted. The application lists are read as a stream (a document type or entity
  declaration is refused in any encoding, every field is capped), cached by file identity, and screenshots are not fetched.
- **Choose your default browser, and know it took.** A web browser Cygnus manages (Helium, for one) gets **Make default
  browser…** on its page and `cygnus default-browser status | list | set | undo` in the terminal. Cygnus sets it with
  `xdg-settings` (what browsers' own "make default" button runs), then reads the result back from every place the desktop looks
  and undoes everything with a plain message if any of them disagree; what was set before is remembered and can be put back
  ("Put back the earlier browser…"). It also tells you when a browser is first only by accident (nothing chosen), and checks the menu
  entry first: one that does not claim web links and pages cannot be chosen, and the menu entry Cygnus writes for a program that
  files itself as a browser now claims them (Repair fixes one made earlier). Found while building it: outside a full KDE session
  environment `xdg-settings` fails and never writes KDE's own setting, so the session variables are passed on.

- **Packages from your repositories, by name.** A new **Packages** page (and `cygnus packages search | info | installed | install |
  remove`) searches every configured repository (name, description, what a package provides, its groups; every word must match, and
  nothing you type is a pattern), lists what is installed (chosen by you, installed for another package, no longer needed, with
  updates, from no repository), and opens a package's details: what it needs and whether that is installed, available or missing,
  optional extras, what needs it, what it provides and conflicts with, its size, and for an installed package its file list. Install
  and Remove go through the helper's usual plan and confirmation; no new administrator action was added. Cygnus checks first and says
  in words: a protected package, one that another installed package needs, or one Cygnus manages as an application (remove those under
  Applications) cannot be removed; a package that is already installed
  or that no repository offers cannot be installed; with updates waiting, an install is refused with "update the whole system first"
  (installing on a system that is not up to date is what breaks Arch systems). A package Cygnus did not install is removed only
  after you tick a box on purpose (terminal: `--not-installed-by-cygnus`; `--yes` never answers that). Search reads your current
  package lists and says how old they are: Cygnus never refreshes them on their own, because that is a partial upgrade; updating the
  whole system does.

- **Remove everything no longer needed, and clean the package cache.** The Installed tab's **No longer needed** filter now has tick boxes
  and **Remove selected…** (up to 200 packages in one plan; protected packages, packages another package needs and applications
  Cygnus manages are never offered; a package Cygnus did not install needs the usual explicit confirmation, once for the plan).
  **Clean cache…** shows what deleting old files from `/var/cache/pacman/pkg` would remove (keep the installed version and N older
  ones, default 2; optionally every version of packages that are not installed) and, after the usual password, the helper deletes only files
  of that list. This is a NEW administrator action (`cache.clean`, polkit `auth_admin` every time), the only one that deletes files
  from pacman's package cache (the helper's other deletions are its own staged copies and a stale lock): the client sends two numbers, the helper chooses the files, only package files and their `.sig` in a root-owned,
  link-free, not-world-writable folder, ordered by libalpm's version rules, removed through the folder's descriptor under pacman's lock,
  and only what was shown and is still chosen. Terminal: `cygnus packages orphans | remove --unneeded | cache | clean`. See
  `docs/security.md` and `docs/audit-guide.md`: this action is now the first thing to audit.
- **What protects you during an update, said honestly.** The update dialog and `cygnus upgrade` now say whether a snapshot is taken:
  snap-pac (judged from its hooks as pacman will really use them, its settings and Snapper's list of configurations: "normally",
  and "cannot tell" when a setting cannot be read), Snapper without snap-pac, Timeshift, or nothing. Cygnus takes no snapshot itself.
  The old text "Snapper takes a snapshot first" in the partial-upgrade message, which was not always true, is gone.
- **Go back to an older version.** An installed package's page lists the older versions pacman's cache still holds, and installs
  the one you pick through the helper's ordinary package-file plan (`cygnus packages versions NAME`, `cygnus packages downgrade NAME
  [--to VERSION]`). The helper reads the file itself and it must be the package and version chosen; a protected package is refused;
  nothing is recorded as an application. The helper does not check a package file's signature (it says UNSIGNED); the file's
  standing is that it comes from pacman's own, administrator-only cache. Where pacman requires signatures for package files
  the downgrade is refused up front, in words. Cleaned-out versions cannot be offered. A downgrade does not make Cygnus's ledger claim the
  package (a new `claim_ownership` request field), so removing it later still asks.
- **Arch news before a system update.** Starting an update (the Updates page, or `cygnus upgrade`) now shows the Arch Linux news
  since your last full upgrade. An item that says it needs a manual step must be acknowledged first (a box that starts clear;
  `--ack-news` in the terminal, which `--yes` never implies). An unreadable feed is said and never holds an update back. The feed is
  read only from `archlinux.org` over HTTPS, size-capped, parsed with document-type and entity declarations refused, shown as plain
  text, with only `archlinux.org` links. `archlinux.org` is added to the privacy list; nothing is fetched until you start an update.

## Unreleased — install scripts you can choose to run

- **A way to support the project.** Settings, About has a **Support Cygnus** button that opens the Gumroad page in your browser
  (Cygnus sends nothing); the repository has a Sponsor button (`.github/FUNDING.yml`), the README a short "Support Cygnus" section,
  and the AppStream data a donation link, which software centres show as "Donate".

- **Run the install scripts that come with a file, if you want to.** Until now no install script was ever run. The rule was changed at
  the user's request: nothing runs by default, but for a program in an archive (Discord's `postinst.sh`) and for a `.deb` or `.rpm`
  you can now show the scripts, read them, and tick a box. Archives: run **as you** or **as administrator** (a new, narrowly built
  helper action that asks for your password every time; the script runs like `sudo` would run it); removal scripts are offered when
  uninstalling, and an installed program's page has **Install scripts…**. `.deb`/`.rpm`: the vendor's scripts are written into the
  converted package so pacman runs them when it is installed and removed. What runs is exactly what was shown (checked by SHA-256),
  once, last (a failing script never undoes the install), with a clean environment and a time limit. Terminal: `--run-scripts` and
  `--scripts-as-admin`; `-y` never implies them; exit status 2 when a script you asked for did not finish. See `docs/security.md` ("Running install scripts") and `docs/audit-guide.md`: the new
  helper action is the first thing to audit.

## 0.1.2 — programs from archives, and a notice for new versions

- **Programs in archives (`.tar.gz`, `.tar.xz`, `.tar.zst`, `.tar.bz2`, `.tar`).** A file like Discord's `.tar.gz` used to be refused
  with "archive has no .PKGINFO: not an Arch package". Cygnus now recognises a program archive by its contents, shows what is in it
  (the program to start, the drive it goes on, what its install scripts would have done: they are never run), unpacks it into the
  applications folder of the drive you choose, adds a menu entry and launcher, checks afterwards that the libraries it needs are
  installed, and removes only the folder it made. Unsafe archives (paths that leave the folder, hard links, devices, links that lead
  out, duplicates, source code, system trees) are refused with the reason. A drive that cannot hold the files means the system
  drive is used and the plan says why; too little room stops the install instead. Compressions read: zstd, xz, gzip, bzip2 or
  none (lz4, lzo, lrz and `compress` are named as unreadable). Terminal: `cygnus install FILE --to DRIVE`. Not yet:
  update, move and repair of such programs. An Arch package is recognised by its contents too, in any compression, and as `.pkg`;
  macOS and FreeBSD `.pkg` files are named as such.
- **A notice when a newer Cygnus exists.** The Updates page, `cygnus updates --check` and the background check ask GitHub's list of
  releases and tell you once per version (a pre-release counts while Cygnus is in beta), with the release page's address. Nothing
  is downloaded or installed. GitHub sees your address and `Cygnus/<version>`, like any web request. `cygnus updates` now also
  exits with 1 when a newer Cygnus exists.
- **Fixed:** the test suite downloaded a file from GitHub during the package build; tests now fail at once if they try to reach the
  internet.

## 0.1.1 — optional AI assistant

- **New, off by default: "What else does this need?"** On an installed program's page, Cygnus reads that program's own public
  documentation and asks an AI assistant what else it mentions (a group, a service, a library, a browser extension). It only
  suggests: each item must quote a sentence that really is on the page and must exist (a package or the `input` group) or be a
  valid service name or store page; every suggestion is labelled as an unverified AI suggestion, and nothing is installed without
  the usual confirmation. Choose the shared assistant (hosted on Cloudflare, free during the beta, nothing to set up), Google Gemini
  with your own key (kept in KWallet), or a server of your own such as Ollama. Terminal: `cygnus ai …`, `cygnus needs APP`. What is
  sent is stated in Settings, the [user guide](docs/user-guide.md#the-ai-assistant-optional) and the privacy section of the README.
- **New dependency:** `libsecret` (the desktop wallet library).
- **Found by an independent review of the assistant and fixed:** an address written so that two parsers read it differently could
  get round the check of where a redirect leads (this affected every download); `~/.netrc` logins are no longer sent with page
  requests; a request that trickles in slowly is now cut off after a total time; text from a server or a page is cleaned of control
  characters before it is shown; a key for your own server is kept for that server's address only; and more (see the tests
  `tests/test_ai_review11.py`, `tests/test_http.py`).

## 0.1.0 — public beta

First public release. Not independently reviewed; see [SECURITY.md](SECURITY.md) and the known limits in the
[user guide](docs/user-guide.md#what-cygnus-cannot-do-known-limits).

- **Storage:** choose which drive each application lives on; NTFS and other drives are probed for what they support.
- **AppImage:** install by copying or adopt in place, pinned signatures verified, updates (zsync / GitHub), move, repair, uninstall.
- **Flatpak:** install by id, from a bundle, or from Flathub's Install button; the user installation can live on another
  drive; updates, move, repair, uninstall (with the runtimes it brought).
- **Arch and AUR:** install local packages and AUR packages through the privileged helper (AUR build files are always
  shown for review first, built as you, never as root); full system upgrades only (never partial).
- **DEB and RPM:** converted into pacman packages when safe, with missing libraries found and installed from the
  repositories, a report of what the vendor's install scripts would have done (the scripts are never run), enforced checks
  that nothing of the vendor's package is lost, and updates for 14 known vendors (plus your own sources).
- **Privileged helper:** every action is planned by the helper, shown in full in the authentication dialog and approved
  by you; untrusted package files are read by an unprivileged, sandboxed reader, never by the helper itself.
- **Everything else:** progress bars with real counts, health checks, interrupted-operation recovery, background checks,
  becoming the default program for Flathub links (opt-in), `cygnus report` for bug reports.
