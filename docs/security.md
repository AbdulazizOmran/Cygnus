# Cygnus security model

This document describes what the code enforces today. The design rationale is in
[architecture §16](architecture-proposal.md#16-security-model). Where the implementation is still
narrower than that design, this document says so.

## 1. Two processes, one boundary

| Process | Runs as | Does |
|---|---|---|
| **Cygnus** (`cygnus`, `cygnus-gui`) | you | Everything in your account: analysis, health checks, AppImages, the Flatpak *user* installation, menu entries, the registry and journal. |
| **Helper** (`io.github.omranabdulaziz.Cygnus.Helper1`) | root, started on demand by systemd over D-Bus | Only system changes: pacman installs, enabling services that installed packages ship, adding *you* to an allow-listed group. |

There is never a root GUI.
- **Integrity:** the helper and the pyalpm worker run Python in isolated mode (`-I`): user
  `site-packages`, `.pth` files and `PYTHON*` variables are ignored. The GUI, the command line and
  the background check are ordinary programs of your own account (they can do only what you can).
- **Lifetime:** the helper exits when idle.
- **Sandboxing:** the helper's unit has a private `/tmp` and a read-only `/home`.

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
| `packages.remove`, `services.manage`, `permissions.manage`, `recovery.manage` | `auth_admin` | each time, with details |

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

## 3. Everything from outside is hostile

**Package files.** AppImage, Flatpak bundle, `.pkg.tar.*`, `.deb` and `.rpm` parsers:
- cap every size and count: compressed and decompressed streams, members, headers;
- never follow symlinks out of an image;
- extract single named files with `unsquashfs -cat -no-wildcards`;
- turn any unexpected parser exception into a clean "cannot read this file".

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

**Converted DEB/RPM packages** carry no install scripts and no setuid bits, and pacman owns every
file. Files go only to `/usr`, `/etc` and `/opt` (anything else is refused when the package is
analysed, and again when it is built).
- **Scripts are read, never run.** Cygnus splits each maintainer script into its commands and
  recognises a small set of them (cache updates such as `ldconfig` and `update-desktop-database`,
  messages, systemd calls, Debian packaging helpers). Anything it cannot read with confidence blocks
  the conversion by default: command or process substitution, here-documents, functions, wrapper programs
  (`env`, `xargs`, `sh -c`, `exec`…), redirections to real files, a script in another language
  (found from its `#!` line, or from the RPM program tag), and any command it does not know. File
  commands on system paths, recursive removals, and user or group creation (including RPM
  `sysusers` entries) block it too. File commands elsewhere are shown to you for review.
- **Scripts that could not be read may be gone past knowingly, never run.** Conversion drops every script, so
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
- **Vendor scripts are never run, not even in a sandbox (a decision, not a gap).** Running a maintainer script in a sandbox
  to watch what it does was considered and rejected. Debian's scripts expect Debian's own tools (`dpkg`, `debconf`,
  `update-alternatives`, `adduser`), so on Arch they would mostly fail or need imitations that make the observation a guess; what
  they could tell that the text reading above cannot (files generated at run time) is small; and every sandbox is a promise that
  a kernel or sandbox flaw can break, while "nothing in the vendor's package is executed during conversion" is a promise that
  needs no trust at all. If a package needs a step Cygnus cannot read, it says so, shows the script, and the person decides.
- **Updates of converted programs download, they do not install.** For the vendors that publish an apt `Packages` list (a built-in list of thirteen well known
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
- **Uninstall scripts and `/var`.** `rm`, `rmdir` and `unlink` in an uninstall script (prerm, postrm, preun, postun) are
  ignored, because those scripts are never run and what they clean up was never made; the same commands in an install
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

**Vendor scripts are never run.**
- A manifest cannot express a command (see the [manifest spec](manifest-spec.md) §7).
- DEB/RPM maintainer scripts are classified, never executed.
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
