# Cygnus user guide

## Storage locations

A *storage location* is a drive or folder that Cygnus may put applications on. The system drive is
always available. To add another drive, open **Storage**, or run:

```sh
cygnus storage scan                                   # what could hold applications
cygnus storage add /mnt/data --label HDD --apps-dir apps/CachyOS
```

Cygnus first tests what the filesystem supports. It does this by creating and removing a temporary
folder. Depending on the result, a location can hold:

| Location can hold | Needs |
|---|---|
| AppImages and portable apps | programs may run from it, and file permissions persist |
| Flatpak apps (your personal installation) | additionally symlinks, hardlinks and atomic renames |
| System packages (pacman) | only the system drive: pacman installs to fixed system folders |

**NTFS and exFAT drives.** Cygnus uses them for applications that run as you. It never puts
anything there that runs as administrator, because anyone with the disk can change the file
ownership recorded on NTFS.

**When a drive is unplugged.** Applications on it show as *not connected* (⏏). Their menu entries
stay. Starting one shows a notification instead of an error, and Cygnus never wakes or mounts the
drive by itself.

## Installing

Open **Install** and choose a file, or drop it onto the window. You can also right-click a file in
Dolphin and choose **Open with → Cygnus**. Before anything happens, Cygnus shows a plan:

- **Where each part goes.** The application, its runtime, menu entries and your settings, with
  sizes per drive.
- **What else is needed.** Components such as a system service, a group membership or a browser
  extension, and whether they are already present.
- **What it found.** Problems, such as an obsolete Flatpak runtime, a missing update source or
  broad sandbox permissions, each with the options available.

| File | What Cygnus does |
|---|---|
| AppImage | Verifies a pinned vendor signature where one is known. Copies the file to the chosen drive, then adds a menu entry and launcher. |
| Flatpak bundle (`.flatpak`) | Installs into the system installation (system drive), or into your personal installation stored on the chosen drive. Installs the runtime it needs from a repository you already use. |
| Arch package (`.pkg.tar.zst`, `.pkg.tar.xz`, `.pkg.tar.gz` ...) | Asks for administrator approval, then installs with pacman. The approval dialog shows exactly what will be installed. To describe the package before you approve anything, Cygnus has it read by a locked-down helper account (`cygnus-reader`, created when you install Cygnus) with no network and no rights, so a damaged or hostile file cannot harm the system just by being looked at. |
| Archive (`.tar.gz`, `.tgz`, `.tar.xz`, `.tar.zst`, `.tar.bz2`, `.tar`) | A program that comes as a folder to unpack and run, such as Discord's or Telegram's download. Unpacks it into the chosen drive's applications folder, adds a menu entry and launcher, and never runs the scripts inside it. See "Programs in archives" below. |
| `.deb` / `.rpm` | Explains the safest option: an Arch package you already have, the vendor's or your repositories' Arch package, a conversion, or why the file cannot be installed safely. |

**Programs in archives.** Many programs are offered as a plain archive (a `.tar.gz` or `.tar.xz`) with no installer: you unpack the
folder and run the program in it. Cygnus recognises such a file by what is inside it, not by its name (a file called `Discord.tar`
that is really a gzip is fine), and does the unpacking for you:

- It reads the archive once, without unpacking it, and shows what is in it: which program will start, how big it is, which drive it
  goes on, and what its install scripts (such as Discord's `postinst.sh`) would have done. **Those scripts are not run unless you choose to** (see "Install scripts: running them" below).
- The program to start is the one the archive's own menu entry names (it usually says `/usr/bin/discord`: Cygnus uses the file of
  that name inside the archive). When that cannot be told, you choose it from a list before installing.
- It goes into the **applications folder of the drive you choose** (so it can live on your HDD), as a folder named after the
  program. The menu entry, icon and launcher are added exactly as for an AppImage; the launcher says so when the drive is not
  connected.
- A drive that cannot hold it (it does not keep file permissions, cannot make links the archive needs, or refuses some of its
  file names) is said so in the plan, and your system drive is used instead. **Too little room is different:** the plan says how
  much is needed and how much is free, and nothing is installed until you free some or choose another drive (Cygnus never
  quietly fills the system drive).
- Nothing it unpacks is given extra rights: the special "run as the owner" marks (setuid, setgid) are removed, and nothing is
  writable by other users. The program runs with your own rights, like anything you download, so install only what you trust.
- **It is refused, with the reason,** when the archive is source code to build (use the AUR or the project's instructions), is
  laid out like a system folder tree to unpack over `/`, is a FreeBSD package, has nothing to run, is bigger than 8 GiB when
  unpacked, or contains anything unsafe: a path that leaves its folder, a hard link, a device file, a link that leads outside, the
  same file twice, or a file written through a link.
- When it has been installed, Cygnus checks that the libraries the program needs are on this computer, and offers to install
  the missing ones from your repositories (**Check libraries…** on the program's page does this again at any time).
- **Not supported yet:** updating (Cygnus cannot tell where a new version is; many such programs update themselves), moving to
  another drive, and repair. To update by hand, remove the program and install the newer archive. `.zip` and `.7z` archives are
  not supported, and neither are tar archives compressed with lz4, lzo, lrz or `compress` (Cygnus says so; unpack those yourself
  or recompress them as `.tar.xz` or `.tar.gz`). What is read: zstd, xz, gzip, bzip2, or no compression.

An Arch package is recognised the same way, whatever its compression, and also when it is called just `.pkg`. A `.pkg` that is
a macOS installer, or a FreeBSD package, is named as such: they only work on those systems.

**Flatpak apps by id.** Type an app id in the Install page, for example `org.kde.kcalc` (from
Flathub) or `myremote:org.example.App`. Add `//BRANCH` when an app has several branches. On the
system drive it goes into the system installation. On another drive it goes into your personal
installation stored there; its runtime goes alongside it. If the system installation already has
the repository (for example Flathub) with its signing key, Cygnus copies that repository, key
included, into your personal installation. It never adds one that isn't already signed and trusted
on your system. **Move to…** moves a Flatpak between the two.

**AUR packages.** Type `aur:NAME`. Cygnus shows the package's build files, warnings worth a second
look, and (for an update) only what changed since the version you approved. Nothing runs until you
tick "I have read the build files". Cygnus then:
1. installs any build dependencies from your repositories (password);
2. builds the package as you;
3. installs it (password).

In the terminal: `cygnus aur review NAME`, then `cygnus aur install NAME`.

**.deb and .rpm files.** When the analysis finds a package safe to convert, **Convert and install**
turns it into a pacman package:
- folders move to the places Arch uses;
- vendor cron and repository files are left out;
- no install scripts or setuid bits are kept.

Removing it later removes every file.

*Libraries it needs.* A program that cannot start without a library gets that library from your
repositories: it is listed in the confirmation and installed with the program. Cygnus first looks
for the packages' declared "provides"; if that finds nothing, it searches pacman's file lists, which it
downloads once into its own cache (about 80 MB, refreshed weekly, no administrator rights). A library only an
optional part of the program wants (Chrome's Qt 5 integration, for example) is offered as a tick box:
**Also install qt5-base (optional)**. Left unticked, the package lists it as an optional dependency and the
program works without that part. If no repository has a library, a program that needs it is refused, and a program
that only wants it for an optional part is converted with a note.

*Install scripts Cygnus cannot read.* Scripts that use functions, command substitution and the like cannot be
translated with confidence, so conversion is blocked by default. Unless you choose to run them (see "Install scripts: running them"), conversion **does not run** them, so you can
choose to convert anyway: Cygnus shows what the scripts mention (creating a group, adding a software
repository, registering alternatives…), puts what could matter first, lets you read the whole scripts, and
asks you to confirm that they will not be run. What they would have done does not happen, and a program that
updates itself through its vendor's repository (Google Chrome, for example) will not: update it by converting
the newer file. Scripts that Cygnus *can* read and that create users or load kernel modules still stop the
conversion.

*Nothing of the vendor's package goes missing quietly.* A conversion keeps every file, link and folder of the vendor's package
(even empty folders, man pages under their own names, `.la`, `.a` and `.pod` files), except what it tells you it leaves out: the
vendor's repository and cron files, setuid bits, and a `/var` that holds only empty folders. Two checks enforce it. After
unpacking, every entry of the vendor's file must be in the unpacked tree. After building, the pacman package must hold exactly
that tree, the same files, links and folders, each file the same size. If anything differs, the conversion stops and says what.
What the checks cannot cover is what the vendor's install scripts *create* while installing (generated files, users, settings):
those are listed (see the next paragraph), and only the simple cases are reproduced.

*What the scripts would have done.* Whenever a package has install scripts, the Install page has a button, **Show what the
scripts would have done to your system**. It lists, grouped, every file the scripts write or change, folder they make,
permission and owner they set, link and command they register, icon they install, user or group they create, service they
start, repository, key or download they set up, kernel module they load and file they delete. Each line says whether the path
is written out in the script or only worked out while it runs, and whether the step happens only under a condition (an `if`,
a `case`, a function, a `while` loop, after `&&` / `||`). It is Cygnus's reading of the script's text, not a record of what
would really happen: nothing is run, and a script that is too tangled to read as a whole is listed as far as it can be
read. In the terminal, `cygnus install FILE.deb` prints the same groups.

*What Cygnus adds for you.* Some of what a vendor's install script does is simple and only involves the package's own
files, so Cygnus does it while converting, and lists it before you confirm: the **menu icon** (for example Chrome's
`product_logo_*.png` become its icon in every size the package ships) and **command links** (`/usr/bin/google-chrome`
pointing at `google-chrome-stable`). It never adds a file that already exists or that another package owns, never a link
outside `/usr/bin`, and never one of Debian's generic aliases (`x-www-browser`, `editor`, `java`…). A script that makes an
empty folder (`mkdir -p /opt/app/logs`) or sets a plain permission (`chmod +x`, `chmod 755`) on one of the package's own
files gets the same, when the step is not conditional. A permission that would let everyone write to a file is never copied,
and no file of a converted package is ever writable by anyone but its owner, whatever the vendor's package said. Anything
else the script did is only mentioned.

*What does not stop a conversion any more.* Commands in an **uninstall** script that remove files (the vendor tidying
up what its install script made) are ignored: nothing was made, and pacman removes the package's own files. A `/var`
that holds only empty folders is left out (the program creates them when it runs). A `/var` with files in it, a program
that needs another distribution's kernel module, or a udev rule that runs a program as administrator still refuse the
package: Cygnus tells you which.

*Reinstalling.* Installing a file you chose always installs it, even when that version is already installed (pacman is no
longer allowed to skip it as "up to date"); the confirmation says "REINSTALLING the same version". A converted package's release
number (the `-2` in `155.0.8059.39-2`) is Cygnus's own conversion revision: when a newer Cygnus converts the same vendor file
better (for example with the menu icon and command added), the result is a newer release and installs as an upgrade.

*Updating a converted program.* pacman does not update a converted program (its vendor's repository setup is deliberately left
out), so Cygnus does the looking. For the vendors that publish the package list apt reads (built in: Google Chrome stable, beta
and unstable, Microsoft Edge, Vivaldi, Brave, Opera, Signal, 1Password, TeamViewer, Slack, VS Code Insiders, Google Earth Pro),
the **Updates** page shows the newest version the vendor publishes next to the one you have,
without downloading the program: it reads that list over HTTPS. When a newer one is there, **Download and review…** downloads
exactly that file (checked against the checksum the vendor publishes) and opens it on the Install page, where it is analysed and
confirmed like any file you open yourself, and replaces the old copy. Nothing is installed from the Updates page, and nothing
is updated in the background without you: the background check only tells you that a new version exists. For a program Cygnus
has no source for, the Updates page says so; open the newer file you downloaded and it replaces the old one the same way. If
the vendor publishes such a list that Cygnus does not know, you can add it yourself:
`cygnus feed add PACKAGE --index https://…/Packages.gz --base https://…/` (the package name is the one on the .deb's `Package:`
line; Cygnus refuses a list it cannot read or that does not name that package), `cygnus feed list`, `cygnus feed remove PACKAGE`.
This protects against a damaged or mixed-up download, not against a vendor whose own server has been taken over (Cygnus does not
check the vendor's GPG signature on the package list): the same protection you have when you download from the vendor's website.

*Replacing a copy Cygnus made earlier.* A program you converted before is not "an Arch package that pacman keeps up to
date": nothing updates it. Opening a newer file for the same program offers to replace it, and says so.

In the terminal: `cygnus install FILE.deb [--with-optional] [--accept-scripts]`.

**Opening Flathub links with Cygnus.** In **Settings → Opening software**, switch on *Open Flathub links and software
files with Cygnus*. Flathub's Install button then opens Cygnus (the button itself opens a `flatpak+https:` link; Cygnus also takes a downloaded
`.flatpakref` file and an `appstream:` link), and so do
`.flatpak`, `.deb`, `.rpm` and AppImage files. Cygnus only shows what the link or file is and asks first; it never
installs by itself, and it never adds a repository a file names (a `.flatpakrepo` file is explained, not followed). This
changes only your own default applications (`~/.config/mimeapps.list`), remembers what opened each type before (Shelly,
Discover…) and puts it back when you switch it off. If a Cygnus window is already open, the link is handed to it instead of
opening a second one. A browser remembers its own "always open with…" choice: if you once told Firefox or Chrome to
always use another program, reset that in the browser's settings.

**Progress.** Installs, updates, moves, repairs and uninstalls show a progress bar with the step it is on.
The bar shows a real count only: the step of the operation, pacman's own package counter, the bytes of a
download of known size, or Flatpak's percentage. Where no count exists (building, looking things up), the bar
only shows that something is happening.

**Already have an AppImage?** Open its page and choose **Manage with Cygnus**. Cygnus adds a menu
entry and a launcher that keep working when its drive is unplugged. The file stays where it is.

## Packages from your repositories

**Packages** lists what your configured repositories offer (**Available**) and what is installed (**Installed**). Type at least two
characters; every word you type must be found in a package's name, description, the names it provides or its groups, and the best
matches come first (an exact name, then names that start with the word). What you type is plain text, never a pattern. The first
200 matches are shown, and the page says how many there were. The **Installed** tab filters what is on this computer: chosen by
you, installed for another package, **no longer needed** (a dependency nothing needs any more, what `pacman -Qdt` lists; a package
that is only an optional extra of another counts as no longer needed), with updates, or from no repository (a file or the AUR).

Open a package to see: what it needs (each item marked installed, will be installed, or **not available** from your repositories,
which stops an install), optional extras, what needs it, what it provides, conflicts with and replaces, its download and installed
size, and for an installed package its files. An installed package that has an install script is marked: pacman runs it as
administrator (the package lists do not say this for a package that is not installed yet).

**Install…** and **Remove…** show the helper's plan (every package, versions, download size) and ask for your password at the end,
like every other change. Cygnus refuses, in words, and says why (all but the "updates waiting" case before the helper is asked at all;
that one is the helper's own check):
- to install a package that is already installed, or that no repository offers;
- to install anything while system updates are waiting: update the whole system first (the **Updates** page). Installing one
  package into a system that is not up to date is the classic way to break an Arch system;
- to remove a protected package (the base system, kernels, firmware, boot loaders, anything in `HoldPkg`) or one that another
  installed package needs. Removing leaves the packages that were installed for it; they show up as **no longer needed**;
- to remove a package Cygnus manages as an application (a converted `.deb`/`.rpm`, a package file, an AUR build): remove those from
  their own page under **Applications**, so that the application list and the system agree.

**Going back to an older version.** On an installed package's page, **Go back to an older version** lists the older versions that
pacman's package cache (`/var/cache/pacman/pkg`) still holds, with their dates and sizes; **Install this version…** shows the plan
and asks for your password like any other change. It is the same plan as installing a package file, so the helper reads the file
itself (it must be the package and version you chose) and refuses a protected package (the base system, kernels, firmware, boot
loaders). The helper does not check a package file's signature: it says UNSIGNED and shows the file's SHA-256, and the reason to
trust this file is that it comes from pacman's own cache, which only the administrator can write, filled after pacman checked the
signature. If another installed package needs the newer version, pacman refuses and changes nothing. The older version stays until
the next system update installs the newer one again. Terminal: `cygnus packages versions NAME`, `cygnus packages downgrade NAME
[--to VERSION]` (the newest older version unless you say). A package that is not in the cache any more (cleaned) cannot be gone
back to from here.

**No longer needed.** On the **Installed** tab the filter **No longer needed** lists packages that were installed for another
package and that nothing needs now (what `pacman -Qdt` lists; a package that is only an optional extra of another counts). Tick the ones
to remove (**Select all** ticks the list) and press **Remove selected…**: one plan, one confirmation, one password. Look before you
remove: a tool you use by hand, or built something with (build tools such as `cmake` show up), can be on the list, and removing some can
leave others that only they needed; they appear the next time. Protected packages and ones Cygnus manages as applications are never on
the list (they are left out of it, so they cannot be ticked). Terminal: `cygnus packages orphans`, `cygnus packages remove --unneeded`.

**Cleaning the package cache.** pacman keeps every package it ever downloaded (`/var/cache/pacman/pkg`). **Clean cache…** (the toolbar
of the Packages page) shows what a cleaning would delete with your settings (how many older versions of each package to keep besides the
installed one, default 2; whether packages that are not installed lose every cached version) and, after you press **Clean…** and
confirm, the administrator helper deletes only files of that list (it chooses again when it starts, so a package installed meanwhile can only shrink it). The installed version of every package always stays; the order of
versions is libalpm's, not file dates; only package files and their signature files in a folder that is root's own are touched. A
version deleted here that is no longer on the mirrors cannot be fetched again, and is then gone for "going back to an older version".
Terminal: `cygnus packages cache [--keep N] [--uninstalled]` (looks only) and `cygnus packages clean [--keep N] [--uninstalled]`.

A package that **Cygnus did not install** is removed only after a second question with a box you tick on purpose. The helper
checks all of this again: nothing the page decides is trusted.

The search reads your **current package lists** and shows how old they are. Cygnus never refreshes them by themselves: refreshing the
lists without upgrading is what causes partial upgrades. **Update system…** refreshes them together with the upgrade. If an install
cannot download a file, the lists are old: update the system.

Terminal: `cygnus packages search WORDS…`, `info NAME [--files]`, `installed [--explicit --deps --unneeded --updates --foreign]`,
`install NAME…`, `remove NAME… [--not-installed-by-cygnus]`, `versions NAME`, `downgrade NAME [--to VERSION]`, `orphans`, `cache`, `clean`.

## Checking an application

Each application's page lists its features and their state:

| Symbol | Meaning |
|---|---|
| ✓ | works |
| ⚠ | a needed component is missing; the feature does not work |
| ○ | an optional feature is not set up |
| ? | Cygnus could not tell, which is not the same as broken |
| ✗ | the application itself does not work |
| ⏏ | its drive is not connected |
| – | an optional feature you switched off (**Not interested**) |

**Not interested?** Optional features you do not want, like a browser extension, can be switched
off with **Not interested**. They are then shown as "–" and no longer suggested or reported.
**Show again** undoes it; in the terminal, `cygnus dismiss APP COMPONENT [--undo]`.

When a component is missing, **Install missing component…** shows exactly what will change,
including any security note. Changes that need administrator rights ask for your password in a
dialog that names the exact change. Browser extensions are installed by you from the browser's
store; Cygnus opens the right page.

Cygnus also warns when the same application is installed more than once, for example as an AppImage
and as a Flatpak, if the two copies interfere.

## Updates

**Updates** shows each application's state from the last check. **Check now** asks each source
directly:
- AppImages: the vendor's update information (zsync or GitHub releases);
- Flatpaks: their repository;
- Cygnus itself: GitHub's list of releases. If a newer Cygnus exists you are told once per version (and the Updates page says so),
  with the address of its release page. While Cygnus is in beta, a pre-release counts as a new version too. Cygnus does not
  update itself: get it the way you got this one. Each check tells GitHub your internet address and that the program asking is
  `Cygnus/<version>`, like any web request; nothing else is sent.

**Update** downloads the new build next to the old one and checks it before anything is replaced:
- it must match the checksum the vendor published;
- it must be the same application;
- it must carry the vendor's signature, where one is pinned.

Only then does Cygnus replace the file, and it deletes the old version last. If anything fails, the
previous version is put back. An application that is running is not replaced until you close it.

System packages are never updated one at a time. The **System packages** card shows how many updates
your repositories have. Cygnus checks this without administrator rights.

**Update system…** shows the full plan before anything happens:
- every package, with its old and new version;
- the download size;
- whether a new kernel is included (restart afterwards).

The plan always installs fresh package databases together with every upgrade, so it is never a
partial upgrade. In the terminal: `cygnus upgrade`.

**Snapshots.** Next to the news the dialog (and `cygnus upgrade`) says what will protect you if the update goes wrong. Cygnus takes
no snapshot itself and runs neither Snapper nor Timeshift; it only looks, and says "normally" because a hook can still fail later:
- **snap-pac** (Snapper's pacman hooks) snapshots around every package change, including this update, when its hook is in place
  (not replaced by a link to `/dev/null`), `/etc/snap-pac.ini` does not turn the root configuration off and `/etc/conf.d/snapper`
  lists `root`. If it cannot take the snapshot, pacman stops before changing anything. A setting it cannot read is reported as "cannot
  tell", never as protection;
- **Snapper without snap-pac**, or **Timeshift**: named, with what to do by hand before updating;
- **nothing found**: said, so that you know there is nothing to roll back to.

**Arch news.** While the plan is made, Cygnus reads the Arch Linux news feed (`archlinux.org`, over HTTPS; nothing about you is
sent beyond your address and `Cygnus/<version>`) and shows the items published since your last full upgrade (the date comes from
pacman's log; with no record, the last 30 days). An item whose text says it **needs a manual step** (Arch's usual "requires manual
intervention") is marked, and the **Update** button stays disabled until you tick that you have read it and know what to do. In
the terminal `cygnus upgrade` asks the same question; `--yes` skips only the password question, never this one, and
`--ack-news` answers it in advance. News that does not need anything is shown and does not hold the update. If the feed cannot be
read, that is said and the update is not held back (security updates must not wait for a web page): read
`archlinux.org/news` yourself if you can. This is Arch's news only: a distribution built on Arch (CachyOS, for one) has
announcements of its own, which you need to follow separately.

**Background checks.** Under **Settings → Background checks** (or `cygnus watch --enable`), Cygnus
checks every few hours and sends a notification about anything new:
- an application update;
- an application that stopped working;
- an operation that did not finish;
- pending system updates, at most once a day.

It never installs anything by itself.

## Moving and repairing (AppImages)

- **Move to…** copies the application to another location. Its menu entry, launcher and autostart
  follow it. The old copy is deleted only after everything else worked.
- **Repair…** lists what is wrong with what Cygnus set up and puts it right.
  - **AppImages:** a deleted menu entry or a modified launcher is rewritten.
  - **Flatpaks:** a missing storage link, an app Flatpak lost, or its runtime is reinstalled. Flatpak
    then checks every file of your personal installation. If the application updated itself, Repair records the new
  version. If the file now belongs to a different application, Repair refuses.

## Install scripts: running them

Some files come with install scripts: a `postinst.sh` in a program's archive, or the maintainer scripts inside a `.deb` or `.rpm`.
**Cygnus does not run any of them unless you choose to.** Nothing is ticked by default, for any file, and a box only appears once the
script's whole text is on screen, next to what it mentions (creating a user, loading a kernel module, restarting a service…).
If you hide the text again, the choice is taken back; what will run is written next to the Install button.

- **A program in an archive.** Show the scripts on the Install screen, read one, and tick *Run it after installing*. Choose **as me**
  (Cygnus runs it in the program's folder, with your own rights) or **as administrator** (your password is asked, every time, and the
  dialog says what the script is: its size, the program it runs with, its first line and its checksum). It runs after the program is
  installed, so a script that fails never undoes the install; the screen then says "the program is installed, but the script did not
  finish" and shows what the script printed. Only scripts directly in the program's folder count as install scripts. Later, an
  installed program's page has **Install scripts…**, which lists them again and runs one on request (only while it is still exactly as
  it was when the program was installed). Removal scripts (`prerm`, `postrm`, `uninstall`) are offered, to run as you, when you remove
  the program; a script that fails is reported and the removal goes on. In the terminal a removal script can also run as administrator.
- **A `.deb` or `.rpm`.** The Install screen has a section with the vendor's scripts in full. Tick *Run them as administrator* and the
  scripts are put into the converted package, so pacman runs them when the package is installed and when it is removed (with the
  arguments Debian or rpm would have given them). On an **upgrade** pacman runs only the new package's pre- and post-install scripts,
  never the old package's removal scripts. A script that fails or refuses cannot stop pacman: the install or removal goes on, and
  pacman shows what the script said. Scripts for programs Cygnus does not know how to run (such as lua), debconf's `config` and trigger
  scripts are left out, and the screen lists them. This also lets you convert a package whose only problem was its scripts; it never
  lets through a package that ships files Cygnus refuses (setuid files, udev rules, files outside `/usr`, `/etc` and `/opt`), or one that
  declares system users.
- **In the terminal:** `--run-scripts` shows each script in full, asks, and runs it (`-y` never implies this). For archives add
  `--scripts-as-admin` to use your password instead of your own rights (it is refused without `--run-scripts`);
  `cygnus uninstall PROGRAM --run-scripts` does the same for removal scripts, after you have confirmed the removal. The exit status is
  **2** when the change was made but a script you asked for did not run or did not finish.

A script is only offered when Cygnus can show it exactly as it would run: plain text without invisible or look-alike characters, whole,
written for sh, bash, zsh, Python or Perl, with a first line that asks for nothing Cygnus does not follow (`#!/bin/sh -e` is followed).
One that is binary, too long to show in full, for another language, or beyond the first eight is listed with the reason and is not
offered; you can still run it yourself from the program's folder if you trust it.

What to keep in mind: a script you choose to run can do anything your account (or, as administrator, the whole computer) can do, and
Cygnus cannot undo it or tell who wrote it. The administrator helper cannot know that anything was shown to you; the password dialog is
the gate, so only go on when you just asked Cygnus to run that script. Scripts written for Debian or Fedora expect that system's tools; one
that cannot find them fails, and the install goes on. As you, a script runs once (not again if Cygnus is interrupted while it runs) and
is stopped after five minutes; as administrator, after ten.

## Uninstalling

**Uninstall…** removes what Cygnus created: menu entry, icon, launcher and, for Flatpaks, any runtime
Cygnus installed that nothing else uses. Your settings and data are kept.
- **AppImages:** the application file is deleted only if you tick the box.
- **Programs from archives:** the program's folder is removed only when you tick the box, only if it is the folder Cygnus made
  (it carries a marker file with this installation's number; a link, or a folder without the marker, is never touched), and you
  are told first how many files are in it and how many changed or were added since it was unpacked (a program that updates
  itself changes its own folder). What the program saved in your home folder stays.
- **Autostart entries you made yourself** are left alone unless you ask (`--remove-autostart` in the
  terminal).

## If something was interrupted

If Cygnus was closed or the computer shut down during a change, the **Applications** page shows what
did not finish. You can then:

- **Finish it.** Run the remaining steps.
- **Undo it.** Reverse the completed steps. Your own files are never deleted by an undo.

In the terminal: `cygnus recover`, `cygnus recover --finish ID` or `cygnus recover --undo ID`.

If a package manager crashed and left pacman locked, every install and update fails. The
**Applications** page then offers **Remove it…**. Cygnus removes the lock only when no package
manager is running. In the terminal: `cygnus recover --pacman-lock`.

## Terminal

Every GUI action is also a command; `cygnus --help` lists them. Commands that install, remove, repair,
move or update software show the steps and ask first; `--yes` skips that question. Settings-style
commands (`storage add/remove/set-default`, `dismiss`, `watch --enable/--disable`) take effect at
once and can be undone the same way. `--json` gives machine-readable output where
supported. A program in an archive: `cygnus install ~/Downloads/Discord.tar.gz --to HDD` (add `--program PATH` to name the
program to start when Cygnus cannot tell, `--yes` to skip the question). `--run-scripts` (with `--scripts-as-admin` for archives)
runs the install scripts that come with a file, after showing them; `--yes` never implies it. `cygnus updates` exits with 1 when
something can be updated, and that includes a newer Cygnus.

## Optional parts

The packages Cygnus needs to run are installed with it by pacman. A few extra tools are only suggested, and Cygnus uses them
when they are there: `fuse3` and `fuse2` (start AppImages the normal way), `zsync` (download only what changed when an
AppImage is updated), `base-devel` (build AUR packages) and `kservice` (refresh the Plasma menu at once). **Settings →
Optional parts** lists the ones that are missing and installs any of them on request: it shows the helper's plan (exactly what
pacman will install) and asks for your password, like every other installation. Cygnus never installs them by itself.
What an application needs (Flatpak runtimes, build dependencies of an AUR package, libraries of a converted program) is
installed by Cygnus as part of installing that application.

## The AI assistant (optional)

Off by default; Cygnus works fully without it. Some programs need more than their package says: a group, a service, a library or
a browser extension that only their documentation mentions. When the assistant is on, an application's page has **What else does
this need?**: Cygnus finds the program's website itself (from its own information, never guessed; you may add one page),
downloads a few public pages from that vendor's own site or code project, and asks an AI assistant what else they say it needs.

**What is sent:** the program's name, its id and its format, and the text of those public pages. Nothing about you, your files,
your drives or your other programs, and never an install script. **To whom:** to the assistant you chose in **Settings → AI
assistant**:

- *Cygnus hosted assistant* (the default, nothing to set up): a small shared service, hosted on Cloudflare, that holds the key and
  asks Google's Gemini on your behalf. It needs no account and stores nothing about you; like any website it sees your internet
  address, and Google may keep the public text under the terms of its free service. It has a daily limit for everyone and a
  short limit per person; when a limit is reached Cygnus says so.
- *Google Gemini with your own key*: the text goes straight to Google with your key. The key is kept in KWallet, never in a file.
- *An OpenAI-compatible server*: for example Ollama on your own computer (plain `http` is accepted only for this computer).

**What you get back is checked, not trusted.** The assistant must give, for each item, a sentence quoted from one of the pages.
Cygnus keeps an item only when that sentence really is in that page and the item fits a shape it can verify: a package whose
exact name exists in your repositories (or in the AUR, shown as community-built and reviewed first), the `input` group, a
systemd service name, a browser extension's store page, or a plain note. A package or a group is checked to exist; a service name
is only checked to be a valid name (it belongs to a package you may still have to install first), and a store page to be on a
known store. Anything else is dropped and listed under "Left out". Each suggestion shows the quote and where it came from, is
marked as an unverified AI suggestion, and goes through the same confirmation as any other change (the password dialog names the
exact change; a browser extension only opens its store page). The assistant can never run anything.

The pages are read with care: only the vendor's own site (or the project's page on GitHub or GitLab), only public internet
addresses, and your saved website logins (a `.netrc` file) are never sent with these requests.

In the terminal: `cygnus ai status|on|off|test`, `cygnus ai use hosted|gemini|compatible [--model M] [--base-url U]`,
`cygnus ai key set|clear gemini|compatible` (the key is typed or piped in, never given on the command line), and
`cygnus needs APP [--url PAGE]`, which only lists suggestions.

## What Cygnus cannot do (known limits)

- **Some .deb and .rpm files cannot be converted**, and Cygnus says why: programs that bring a kernel module, need a newer
  system C library than yours, use another distribution's library folders, install files into `/var`, or add a udev rule that
  runs a program as administrator (Steam does). Many of those have a native version (the repositories, the AUR, Flathub), which
  Cygnus offers first.
- **Programs from archives are not updated, moved or repaired by Cygnus yet.** Remove the program and install the newer archive,
  or the same archive onto another drive. They also carry no vendor signature Cygnus can check: where the archive came from is up
  to you.
- **Install scripts are not run unless you choose to.** What a script creates only when it runs (generated files, caches) cannot be
  reproduced without running it; a script Cygnus cannot read is shown to you, and you decide, and you can choose to run it (see
  "Install scripts: running them"). Cygnus never runs a script just to find out what it does.
- **Only some vendors can be checked for updates** (see "Updating a converted program"); add others with `cygnus feed add`.
  The vendor's package list is read over HTTPS only, not checked by signature.
- **Cygnus never adds a software repository** that a file or a link names (`.flatpakrepo` files are explained, not followed).
- **Tested on one setup:** CachyOS, KDE Plasma, x86_64. Other desktops and distributions may work and have not been tried.
- **One helper, one plan at a time.** If another user of the same computer is preparing a package plan, yours is asked to try
  again in a moment.
- **Not built:** root-owned storage setup (mount units) and custom system Flatpak installations.
- **The AI assistant can be wrong and can be unavailable.** It only suggests; what it names must exist and must be quoted from the
  program's own documentation, and nothing is installed without your approval. The shared hosted assistant has a daily limit for
  everyone; when it is used up, try again tomorrow or use your own key.
- **No security audit by a person yet.** `docs/audit-guide.md` is written for whoever does it.
