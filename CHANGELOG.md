# Changelog

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
