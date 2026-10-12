# Draft announcement for the 0.2.0 beta (edit freely)

**Cygnus 0.2.0 (public beta): a store and an app manager for CachyOS / KDE Plasma**

Cygnus lets you choose which drive each application lives on (SSD, HDD, a second NTFS drive) and installs and manages
AppImages, Flatpaks, AUR packages and local Arch packages, and converts `.deb` / `.rpm` files into proper pacman packages
(so Google Chrome from Google's `.deb` just works, and updates). It resolves the runtimes and companion components an
application needs, checks that everything works afterwards, and can always finish or undo what it started.

**New in 0.2.0: Discover**, a store that lists, in one place, what you can get from Flathub, your Arch repositories, the
makers' own downloads, a search of the AUR, and (after one question the first time) AppImageHub, Debian and Fedora. Every way
to get a program is explained in plain words, and each choice goes to the page that already installs it, with its own
look-before-you-confirm. Debian and Fedora packages are converted by Cygnus and marked "may not work"; the Arch version is
always preferred. Also new: a Packages page (search, install, remove, older versions, clean the cache), Arch news and a
snapshot note before system updates, a fix that lets an AppImage browser such as Helium be your default browser, Flatpak
permissions in plain words, an optional update icon, and install scripts you can choose to run.

What makes it different:
- it **shows what it is about to do** and asks first, including a plain-language report of what a vendor's install scripts
  would have done (they run only if you tick a box, after you have read them);
- the part that runs as administrator is small, every action is approved in the polkit dialog, and untrusted package
  files are read by an unprivileged sandboxed process;
- Flathub's Install button can open Cygnus (opt-in).

**It is a beta.** It has been used on one machine (CachyOS, KDE Plasma, x86_64) and has not had an independent security
review. Packages built for other systems often do not convert or do not run. There is a
[list of known limits](docs/user-guide.md#what-cygnus-cannot-do-known-limits), a
[guide for anyone who wants to audit it](docs/audit-guide.md), and `cygnus report` produces a paste-able report for bug
reports. Security problems: [SECURITY.md](SECURITY.md).

Free during the beta (the ready-made package is on Gumroad; or build it yourself from this repository). Requirements,
install and build instructions are in the README. GPL-3.0-or-later.
