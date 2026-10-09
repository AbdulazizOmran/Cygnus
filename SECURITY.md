# Security policy

Cygnus is in **public beta** and has **not had an independent security review**. Part of it runs as administrator (the
helper, started over D-Bus and authorised by polkit for each action), so security reports are welcome and taken seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use one of these, in this order:

1. GitHub's private vulnerability reporting for this repository ("Security" tab → "Report a vulnerability"), if it is enabled;
2. an email to the maintainer: abdulazizomran10@proton.me (the address in the package's `PKGBUILD`).

Say what an attacker needs (a local unprivileged process? a hostile package file? a web page the browser opens in
Cygnus?), what they get, and how to reproduce it. A failing test, or the exact file and line, is the best report.
You will get an answer, and a fix, as quickly as one person can manage; there is no bounty.

## What is in scope

The privileged helper and the package reader (`cygnus/helper/`), how packages and links from outside are handled
(`cygnus/core/backends/foreign.py`, `translate.py`, `convert.py`, `cygnus/gui/launch.py`), downloads and update checks
(`cygnus/core/util/http.py`, `cygnus/core/backends/vendor_feeds.py`), and the packaging (`packaging/`, `data/`).
[docs/security.md](docs/security.md) is the model; [docs/audit-guide.md](docs/audit-guide.md) is a map for a reviewer
and lists the weak spots that are already known.

## Supported versions

Only the newest release.
