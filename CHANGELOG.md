# Changelog

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
  that nothing of the vendor's package is lost, and updates for 13 known vendors (plus your own sources).
- **Privileged helper:** every action is planned by the helper, shown in full in the authentication dialog and approved
  by you; untrusted package files are read by an unprivileged, sandboxed reader, never by the helper itself.
- **Everything else:** progress bars with real counts, health checks, interrupted-operation recovery, background checks,
  becoming the default program for Flathub links (opt-in), `cygnus report` for bug reports.
