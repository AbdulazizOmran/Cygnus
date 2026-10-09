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
| Arch package (`.pkg.tar.zst`) | Asks for administrator approval, then installs with pacman. The approval dialog shows exactly what will be installed. To describe the package before you approve anything, Cygnus has it read by a locked-down helper account (`cygnus-reader`, created when you install Cygnus) with no network and no rights, so a damaged or hostile file cannot harm the system just by being looked at. |
| `.deb` / `.rpm` | Explains the safest option: an Arch package you already have, the vendor's or your repositories' Arch package, a conversion, or why the file cannot be installed safely. |

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
translated with confidence, so conversion is blocked by default. Because conversion **never runs** them, you can
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
- Flatpaks: their repository.

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

## Uninstalling

**Uninstall…** removes what Cygnus created: menu entry, icon, launcher and, for Flatpaks, any runtime
Cygnus installed that nothing else uses. Your settings and data are kept.
- **AppImages:** the application file is deleted only if you tick the box.
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
supported.

## Optional parts

The packages Cygnus needs to run are installed with it by pacman. A few extra tools are only suggested, and Cygnus uses them
when they are there: `fuse3` and `fuse2` (start AppImages the normal way), `zsync` (download only what changed when an
AppImage is updated), `base-devel` (build AUR packages) and `kservice` (refresh the Plasma menu at once). **Settings →
Optional parts** lists the ones that are missing and installs any of them on request: it shows the helper's plan (exactly what
pacman will install) and asks for your password, like every other installation. Cygnus never installs them by itself.
What an application needs (Flatpak runtimes, build dependencies of an AUR package, libraries of a converted program) is
installed by Cygnus as part of installing that application.

## What Cygnus cannot do (known limits)

- **Some .deb and .rpm files cannot be converted**, and Cygnus says why: programs that bring a kernel module, need a newer
  system C library than yours, use another distribution's library folders, install files into `/var`, or add a udev rule that
  runs a program as administrator (Steam does). Many of those have a native version (the repositories, the AUR, Flathub), which
  Cygnus offers first.
- **Install scripts are never run.** What a script creates only when it runs (generated files, caches) cannot be reproduced; a
  script Cygnus cannot read is shown to you, and you decide. This is deliberate: nothing in a vendor's package is executed
  during conversion.
- **Only some vendors can be checked for updates** (see "Updating a converted program"); add others with `cygnus feed add`.
  The vendor's package list is read over HTTPS only, not checked by signature.
- **Cygnus never adds a software repository** that a file or a link names (`.flatpakrepo` files are explained, not followed).
- **Tested on one setup:** CachyOS, KDE Plasma, x86_64. Other desktops and distributions may work and have not been tried.
- **One helper, one plan at a time.** If another user of the same computer is preparing a package plan, yours is asked to try
  again in a moment.
- **Not built:** root-owned storage setup (mount units) and custom system Flatpak installations; AI-assisted discovery.
- **No security audit by a person yet.** `docs/audit-guide.md` is written for whoever does it.
