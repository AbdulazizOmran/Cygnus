# Draft announcement for the beta (edit freely)

**Cygnus (public beta): an application installer and manager for CachyOS / KDE Plasma**

Cygnus lets you choose which drive each application lives on (SSD, HDD, a second NTFS drive) and then installs and manages
AppImages, Flatpaks, AUR packages and local Arch packages, and converts `.deb` / `.rpm` files into proper pacman packages
(so Google Chrome from Google's `.deb` just works, and updates). It resolves the runtimes and companion components an
application needs, checks that everything works afterwards, and can always finish or undo what it started.

What makes it different:
- it **shows what it is about to do** and asks first, including a plain-language report of what a vendor's install scripts
  would have done (those scripts are never run);
- the part that runs as administrator is small, every action is approved in the polkit dialog, and untrusted package
  files are read by an unprivileged sandboxed process;
- Flathub's Install button can open Cygnus (opt-in).

**It is a beta.** It has been used on one machine (CachyOS, KDE Plasma, x86_64) and has not had an independent security
review. There is a [list of known limits](docs/user-guide.md#what-cygnus-cannot-do-known-limits), a
[guide for anyone who wants to audit it](docs/audit-guide.md), and `cygnus report` produces a paste-able report for bug
reports. Security problems: [SECURITY.md](SECURITY.md).

Requirements, install and build instructions are in the README. GPL-3.0-or-later.
