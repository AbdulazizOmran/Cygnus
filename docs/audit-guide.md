# A guide for whoever audits Cygnus

Cygnus has been reviewed many times, by its author and by independent AI reviewers (eight rounds, each finding checked by hand
and fixed with a test). That is not a human security audit, and the part that matters most to audit is small. This guide says
what that part is, in what order to read it, what it is supposed to guarantee, and where the known soft spots are, so that
a day of a security engineer's time goes where it is worth most. `docs/security.md` is the description of the model; this
file is the map.

## 1. What can hurt you, and where

Three kinds of process, in order of power:

| Process | Runs as | Can do |
|---|---|---|
| `cygnus-helper` (`cygnus/helper/service.py`, `actions.py`) | root, started over the system bus when needed, exits when idle | run `pacman`, `systemctl enable`, `gpasswd`; every operation first needs the user's polkit approval |
| the package reader (`cygnus/helper/inspect_worker.py`, `seccomp.py`) | `cygnus-reader` (locked account, uid from `data/sysusers/cygnus.conf`), no network, no new processes, seccomp filter | open an untrusted package file and report what is in it; it never installs |
| the GUI and the CLI (`cygnus/gui`, `cygnus/cli`, `cygnus/core`) | the person | everything else: analysis, conversion (`makepkg` as the person), downloads, the registry |

The one thing to prove or disprove: **a local unprivileged process, or a hostile file the person opens, cannot make the
helper do anything the person did not approve, and cannot reach root without that approval.**

## 2. Read in this order (about 2,000 lines)

1. `data/polkit/*.policy`, `data/dbus/*.conf`, `data/systemd/cygnus-helper.service`: who may talk to the helper and with what
   authentication.
2. `cygnus/helper/service.py` (D-Bus methods: `PlanPackages`, `PlanUnit`, `PlanGroup`, `PlanClearStaleLock`, `Commit`,
   `GetLedger`, `GetOperation`): caller identity (`_caller_uid`), plan ownership (`plan.caller_sender`/`caller_uid`), single-use
   plans that expire, staging limits, the planning slot and its fairness rule, the start-up clean-up that must only run once the
   bus name is owned.
3. `cygnus/helper/actions.py`: everything that becomes a command line. Check the allow-lists (package and unit names,
   groups), the fixed pacman argv (no `--overwrite`, `--nodeps`, `--noscriptlet`, `--ask`), protected packages and
   `HoldPkg`, the staged package copy (opened by descriptor, hashed from it, root-owned, 0644 in a 0711 folder), and
   `_checked_facts` (what the helper believes about a package the reader described).
4. `cygnus/helper/inspect_worker.py` and `seccomp.py`: the lockdown order (namespaces, `no_new_privs`, `setgroups`/`setgid`/
   `setuid`, the proof that root is gone, the filter) and the filter's list (it is a denylist: judge whether it is enough).
5. `cygnus/helper/ledger.py` and `cygnus/core/privilege.py`: what is recorded, who can read it, how a client waits for a result.

Then, because they handle hostile input but run as the person: `cygnus/core/backends/foreign.py` (package analysis),
`translate.py` (script text reading), `convert.py` (building the converted package), `cygnus/gui/launch.py` (links and files
from a browser), `cygnus/core/backends/vendor_feeds.py` + `cygnus/core/util/http.py` (downloads).

## 3. The invariants worth attacking

- The helper never opens an untrusted package itself; the reader does, without privilege. Try to make the helper act on facts
  the reader did not honestly report (documented: a taken-over reader can misreport the name, so the protected-package check
  is only as honest as the reader).
- A plan is created by the helper, belongs to one caller, can be committed once, and expires in ten minutes. Try replay,
  cross-user commit, a commit racing an expiry sweep (fixed in round 6; check the fix).
- The commands the helper runs are built from validated fields only. Try names with option syntax, newlines, unicode.
- Converted packages carry no install scripts, no setuid/setgid, nothing group- or world-writable on files, and every vendor
  file survives (two enforced checks). Try archives with odd names, links, owners with spaces, huge entries.
- Vendor scripts are never run, not even sandboxed (a decision: see `security.md`).
- A download goes to a folder of its own and is never written through a link.

## 4. Known soft spots (do not spend your time rediscovering these)

- The reader's seccomp filter is a denylist for x86_64 only (CachyOS is x86_64-only).
- The planning slot can be kept busy by one local user; fairness and a time budget limit it, nothing prevents the delay entirely.
- The vendor package lists used for update checks are read over TLS only; their GPG signatures are not checked.
- Conversion reads install scripts as text with a tolerant scanner; a script it misreads can make it list or reproduce
  something the real script would not have done (reproduced steps are limited to icons, `/usr/bin` links, empty folders and
  plain permissions on the package's own files).
- Tested on one machine (CachyOS, KDE Plasma, x86_64).

## 5. Running it

- `python3 -m pytest -q -p no:cacheprovider` (about 80 seconds; 2,000+ tests; they use private D-Bus daemons and temporary
  folders, never the real `~/.config`, Flatpak or `/etc`).
- `tests/test_package_reader.py`, `tests/test_seccomp.py`, `tests/test_helper_bus.py`, `tests/test_helper_coexist.py`,
  `tests/test_helper_protected.py` cover the privileged side; `tests/test_nothing_lost.py`, `tests/test_translate.py`,
  `tests/test_missing_libraries.py`, `tests/test_convert.py` cover conversion.
- The helper can be run on a private bus without root and with commands faked: see `tests/test_helper_bus.py` (`Bus`).

Report anything you find to the author with the file, the line, and what a local attacker or a hostile file would do with it.
