# Changelog

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
