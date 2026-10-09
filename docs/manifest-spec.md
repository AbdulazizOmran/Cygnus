# Cygnus Application Manifest, version 1 (CAM-1)

**Status:** normative. Version 1, 2026-10-07.
**Machine-readable schema:** [`cam-1.schema.json`](cam-1.schema.json), generated from the reference
implementation (`cygnus/core/manifest/schema.py`). Where this document and the schema differ, the
schema decides validity and this document decides meaning.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be read as described in RFC 2119.

---

## 1. Purpose

A manifest describes one application:

- where official builds come from, and how each one is verified and updated;
- which features it has, and which components each feature needs, such as a system service, a group
  membership or a browser extension;
- how a consumer can check that each component works, and how a missing one can be installed;
- which builds must not be installed side by side, and where the application keeps user data.

A manifest is **data, never code**. It cannot express a shell command, a script, a regular expression
or a path outside a small set of confined locations. Every change it can describe comes from a closed
vocabulary of actions (§7). A consumer (such as Cygnus) still decides whether to offer each action,
shows it to the user, and asks for approval.

## 2. Encoding and limits

- A manifest is a UTF-8 JSON document. The recommended file name is `<application.id>.json`.
- It MUST NOT exceed **512 KiB**. Consumers MUST reject larger documents without parsing them.
- Unknown members are an error at every level (`additionalProperties: false`). A consumer MUST reject
  a manifest with members it does not know rather than ignore them. New members require a new
  `manifest_version`.

## 3. Trust

Every loaded manifest gets exactly one trust level:

| Level | How it is obtained | May propose actions (§7) |
|---|---|---|
| `curated` | shipped inside the consumer's own package, or signed with the consumer project's key | yes |
| `vendor-signed` | signed with a key pinned for the vendor's domain (§3.2) | yes |
| `unverified` | anything else: unsigned, unknown signer, signer/domain mismatch, expired | **no** |

An `unverified` manifest MAY still be used to *explain*: to name features and to run probes for a
health report. Its actions MUST NOT be offered or executed.

### 3.1 Signatures

- Signatures are **minisign-compatible Ed25519** signatures in a detached file next to the manifest,
  named `<manifest file>.minisig`. Both the prehashed (`ED`, BLAKE2b-512) and the legacy (`Ed`)
  algorithms are accepted.
- The signature covers the exact bytes of the manifest file. Consumers MUST verify those bytes and
  MUST NOT re-serialise the JSON before verifying.
- The global signature over the trusted comment MUST also verify.

### 3.2 Binding vendor keys to domains

- A vendor key speaks only for its domain (`application.vendor.domain`). A vendor-signed manifest
  is accepted as `vendor-signed` only if **both** of these hold:
  1. the key's pinned owner domain equals `application.vendor.domain`;
  2. every identifier in `application.id`, `application.appstream_ids` and
     `application.flatpak_ids` lies under the reverse of that domain. For example, `whatpulse.org`
     may only claim `org.whatpulse.*`.
- Otherwise the manifest is `unverified`, and the consumer SHOULD say why.
- Package names and desktop-file names cannot be bound to a domain. A consumer MUST NOT use them to
  match a file or an installation to a manifest that is not curated (they are descriptive hints
  there). `appstream_ids` and `flatpak_ids` MUST be reverse-DNS names.
- Vendor keys are published at `https://<domain>/.well-known/cygnus/keys.json` (rotation is
  described in the architecture document, §13.3), or shipped in the consumer's curated trust store.
  They are pinned on first use: a consumer MUST refuse to assign a key it already trusts to a
  different owner.
- **Status in Cygnus:** the signature check, the domain binding and the pin-on-first-use rule
  exist in the loader, but `keys.json` is not fetched yet and no vendor key is pinned. Cygnus
  uses only the manifests bundled with it (`curated`).

### 3.3 Freshness and rollback

- `serial` is a positive integer that MUST increase with every published change. A date-based
  serial such as `2026100601` is RECOMMENDED.
- A consumer MUST refuse a manifest whose `serial` is lower than the highest serial it has already
  accepted for that application. Equal serials are accepted. **Status in Cygnus:** the check is in
  the loader, but the highest accepted serial is not stored between runs yet, so it only protects a
  caller that supplies it.
- `expires` is an RFC 3339 timestamp **with a time zone**. After it, the manifest is treated as
  `unverified` (§3) until a fresh one is obtained. Publishers SHOULD keep expiry within one year.

### 3.4 Identity collisions

When several manifests match the same file or installation, the most trusted wins, in this order:
`curated` > `vendor-signed` > package metadata > `unverified`. A lower-trust manifest MUST NOT
shadow a higher-trust one by claiming the same identifiers.

## 4. Top-level object

| Member | Type | Required | Meaning |
|---|---|---|---|
| `manifest_version` | `1` | yes | This specification. |
| `serial` | integer ≥ 1 | yes | §3.3. |
| `expires` | timestamp with zone | yes | §3.3. |
| `application` | object (§5.1) | yes | Identity of the application. |
| `sources` | array (§5.2) | yes | Official builds. |
| `features` | array (§6.1) | yes | What the application does; exactly one is `core`. |
| `components` | array (§6.2) | no | What features need beyond the application itself. |
| `conflicts` | array (§8) | no | Builds that must not be installed together. |
| `user_data` | object (§9) | no | Where the application keeps its data. |

Cross-references MUST resolve. A consumer MUST reject the manifest if any of these occurs:

- an id is duplicated within `sources`, `components` or `features`;
- a feature `requires` an unknown component;
- a component's `applies_to_sources` names an unknown source;
- a conflict names an unknown source;
- there is not exactly one `core` feature.

## 5. Application and sources

### 5.1 `application`

| Member | Meaning |
|---|---|
| `id` | Reverse-DNS identifier, the primary key, e.g. `org.whatpulse.WhatPulse`. |
| `appstream_ids`, `flatpak_ids`, `desktop_ids`, `package_names` | Other identifiers under which the application is found: in AppImage metadata, Flatpak refs, `.desktop` files, pacman. Used for matching (§3.4). |
| `name` | Display name. |
| `vendor` | `{name, domain?}`. `domain` binds vendor keys (§3.2). |
| `homepage` | `https://` URL. |
| `license`, `supported_versions` | Informational. |
| `platforms` | `[{os: "linux", arch: [...], sessions?: ["x11","wayland"]}]`. |

### 5.2 `sources[]`

A source is one official way to obtain the application.

| Member | Meaning |
|---|---|
| `id` | Component-style id (`^[a-z0-9][a-z0-9-]{0,63}$`). |
| `label` | Short name shown to the user when several sources are offered. Plain text. |
| `format` | `appimage`, `flatpak-bundle`, `flatpak-remote`, `pacman-repo`, `aur`, `pacman-local`, `deb`, `rpm`, `tarball`. |
| `url`, `ref`, `package`, `runtime` | Where it comes from. URLs MUST be `https://`. |
| `channel` | `stable` (default) or `beta`. |
| `update` | §5.3. |
| `verification` | §5.4. |
| `vendor_supported` | `false` marks community builds. |
| `recommended_when` | Hints for choosing between sources: `relocatable` (can live on a second drive), `system-package`, `wayland-window-tracking`, `default`. |
| `known_issues`, `notes` | Shown to the user, never interpreted. |

### 5.3 `update`

| Member | Meaning |
|---|---|
| `type` | `zsync`, `gh-releases`, `flatpak-remote`, `pacman`, `aur`, `manual`, `none`. |
| `self_updating` | The application replaces its own files. A consumer SHOULD expect the payload's digest to change and SHOULD re-verify identity rather than report damage (§10). |
| `url` | Update endpoint, `https://`. |

### 5.4 `verification`

| `type` | Requirement |
|---|---|
| `appimage-openpgp` | The AppImage's embedded signature MUST verify against `openpgp_fingerprint`. The key embedded in the file only supplies key material and is never trusted by itself. Revoked or expired keys fail. |
| `openpgp-detached` | A detached signature (`signature_url`, required) MUST verify against `openpgp_fingerprint`. **Status in Cygnus:** not implemented yet; no bundled manifest uses it. |
| `pkgbuild-openpgp` | AUR sources only. `openpgp_fingerprint` names the vendor key that the package's build files are expected to pin (`validpgpkeys`). The check itself is `makepkg`'s: it verifies the vendor's tarball against a signature file that the build files list, using the key in your keyring (it fails if the key is missing; Cygnus never imports keys and never skips the check). Cygnus does not yet compare the build files' `validpgpkeys` with this fingerprint; you see the build files in the review. |
| `sha256` | The download MUST match `sha256`. |
| `pacman-signature` | pacman's own repository signature checking applies. |
| `tls-only` | Only HTTPS protects the download. Consumers SHOULD say so. |

If a source pins a key or digest and the check fails, the consumer MUST refuse the file. It MUST NOT
fall back to a weaker check.

## 6. Features and components

### 6.1 `features[]`

A feature is something the user notices: "counts keystrokes", "network statistics", "web insights".

| Member | Meaning |
|---|---|
| `id`, `name` | Identity. |
| `core` | Exactly one feature is the application itself. If it fails, the application is broken. |
| `optional` | The user may reasonably not want this feature. Its absence is reported, never as a fault. |
| `requires` | Component ids this feature needs. |
| `notes` | Shown to the user, never interpreted. |

### 6.2 `components[]`

| Member | Meaning |
|---|---|
| `id`, `name`, `description` | Identity. |
| `type` | `permission`, `system-service`, `user-service`, `browser-extension`, `native-messaging-host`, `companion-app`, `runtime`, `codec`, `driver`, `plugin`, `cli-helper`, `kernel-feature`. |
| `relation` | `hard`: the feature cannot work without it. `recommended`: it works worse without it. `optional`: extra. |
| `verify` | Probes (§6.3). |
| `verify_mode` | `all` (default): every probe must pass. `any`: one passing probe is enough, e.g. the extension is installed in any one browser. |
| `action`, `platform_actions.arch`, `post` | How to install it (§7). `platform_actions.arch` replaces `action` on Arch-based systems. `post` runs after it, e.g. enabling the service the package installed. |
| `applies_to_sources` | Source ids the component matters for. Empty means all sources. |
| `browsers` | Required for `browser-extension`: `{chromium?: {id, store_url?}, firefox?: {id, store_url?}}`. |
| `privileges` | Plain-language list of what the component can do. It is shown before approval. |
| `requires_relogin` | The change takes effect only after logging in again, e.g. group membership. |
| `security_note` | Shown before approval. |
| `notes` | Shown to the user, never interpreted. |
| `discouraged_vendor_instructions` | Vendor instructions the consumer deliberately does **not** follow, quoted for transparency, e.g. a `curl … \| sudo bash` installer. Text only, MUST NOT be executed. |

### 6.3 Probes

A probe is a read-only check that returns `pass`, `fail` or `unknown`. Consumers MUST evaluate probes
without side effects and with bounded time and output. They MUST NOT create files, start services
or contact the network. A probe that cannot reach a definite answer returns `unknown`, never `fail`.

| `probe` | Parameters | Passes when |
|---|---|---|
| `group_membership` | `group`, `effective` (`session` default \| `configured`) | The user is in the group. `session` means the current login has it. `configured` means `/etc/group` lists the user, or the group is the user's primary group, so it takes effect after relogin. |
| `readable` | `path`, confined to `/dev`, `/sys`, `/proc` (never through `/proc/*/root`, `cwd`, `fd` and similar, or `/dev/fd`), simple globs allowed | There is at least one match and the user can read every match. |
| `systemd_unit` | `unit` (no leading `-`), `scope` (`system`\|`user`), `expect` (`enabled`, `active`) | The unit is in every expected state. `enabled` means the unit file state is `enabled` (or `enabled-runtime`); `static` and `alias` units are not enabled. |
| `journal_recent_match` | `unit`, `contains` (1–10 plain substrings), `within_minutes` (1–10080) | A recent log line of the unit contains one of the substrings. These are substrings, never regular expressions. |
| `tcp_listener` | `addr`, `port`, `owner_exe_contains?` | Something is bound so that a connection to `addr` reaches it, optionally owned by a matching executable. `0.0.0.0` serves every IPv4 address and `::` every address (Linux sockets are dual-stack by default), so a wildcard listener satisfies a loopback `addr`. A loopback-only listener does not satisfy `0.0.0.0` or `::` ("all interfaces"), and an IPv4-only listener does not satisfy `::1`. A `::` listener that set `IPV6_V6ONLY` cannot be told from a dual-stack one (the kernel's socket table does not show it), so it is treated as dual-stack. |
| `tcp_peer` | `addr` (loopback), `port` | An established connection to that local port exists. |
| `pacman_installed` | `name` | The package is installed. |
| `browser_extension_present` | `family` (`chromium`\|`firefox`), `id` | The extension is present in any profile of a browser of that family. |
| `sysctl` | `key`, exactly one of `expect` or `minimum`, `absent_ok?` | The value equals `expect`, or is at least `minimum`. A setting that does not exist is `unknown`, unless `absent_ok` is true: then a kernel without it passes (it restricts nothing). |
| `process_running` | `exe_contains` | A process of the user runs a matching executable. |

### 6.4 Health semantics

Consumers MUST derive health as follows. The states are listed from least to most severe.

1. A component is **ok** if its probes pass (per `verify_mode`). It is **missing** if any
   required probe fails. Otherwise, including when it has no probes, it is **unknown**.
2. The **core** feature takes the state of the application itself: payload present and runnable,
   runtime installed, drive connected. A hard component that is definitely missing makes it
   **broken**. Missing *evidence* (`unknown`) never makes an application broken.
3. Any other feature is:
   - **ok** when all its components are ok;
   - **optional_unavailable** when it is `optional`, or when only non-hard components are missing;
   - **missing_component** when a hard component is missing;
   - **unknown** otherwise.
4. If the drive holding the application is not connected, every feature is **offline**. That is
   not a fault.
5. The overall state is the most severe feature state. Optional features that are merely
   unavailable do not count, except that an otherwise healthy application with such a feature is
   reported as `optional_unavailable`.

## 7. Actions (closed vocabulary)

Only `curated` and `vendor-signed` manifests may propose actions (§3). Each action MUST be shown to
the user, together with the component's `privileges` and `security_note`, and approved before it
runs. Privileged actions (marked ✱) need administrator approval for the exact change. Flatpak
actions are not marked: they run as the user (user installation, user-level overrides), and a
system-wide installation authorizes through Flatpak's own polkit rules. A consumer
that does not implement an action MUST treat the fix as unavailable. It MUST NOT approximate the
action with something else.

| `kind` | Parameters | Effect |
|---|---|---|
| `pacman.install_repo` ✱ | `names` | Install packages from the configured repositories, as part of a full system upgrade if the databases are stale (never a partial upgrade). |
| `pacman.install_local` ✱ | `url` (`https://`), `sha256` | Download a package file. Install it only if it matches `sha256`. |
| `aur.build` ✱ | `pkgbase` | Build from the AUR after the user has reviewed the build files. |
| `flatpak.install` | `ref`, `remote` | Install a ref from a configured remote. |
| `flatpak.install_bundle` | `url`, `sha256?` | Install a bundle. Its runtime is resolved by the consumer from repositories the user already trusts. A bundle's own `RuntimeRepo` is never added silently. |
| `flatpak.override` | `app` (one of this manifest's `flatpak_ids`), `permissions` | Grant narrow sandbox permissions (§7.1). |
| `appimage.install` | `url` | Download and install an AppImage, subject to the source's `verification`. |
| `systemd.enable_now` ✱ / `systemd.user.enable_now` | `unit` | Enable and start a unit that an installed package provides. Units from anywhere else are refused. |
| `group.add_user` ✱ | `group` | Add the current user (only) to a group. Reversible only by the consumer that added it. |
| `browser.open_store` | none | Open the extension's store page from `browsers.*.store_url`. The user installs it. |
| `info.show` | `text` (≤ 2000 chars) | Show instructions. Nothing runs. |

### 7.1 Flatpak overrides

`flatpak.override` may only target the application's own Flatpak ids. Each permission MUST match
one of these:

- `--share=network|ipc`
- `--socket=x11|wayland|fallback-x11|pulseaudio|pcsc|cups`
- `--device=dri|input|usb|kvm`
- `--filesystem=xdg-{download,music,videos,pictures,documents,desktop}[/subdir][:ro|:rw|:create]`
- `--talk-name=<name>`, except names under `org.freedesktop.Flatpak`,
  `org.freedesktop.portal.Flatpak`, `org.freedesktop.systemd1`,
  `org.freedesktop.impl.portal.PermissionStore`, `org.kde.KWin` and `org.freedesktop.secrets`
- `--env=NAME=value`, except `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PATH`, `PYTHONPATH` and
  `GIO_EXTRA_MODULES`

Host filesystem access, `--device=all`, session/system bus wildcards and anything else that escapes
the sandbox cannot be expressed.

## 8. Conflicts

`conflicts: [{between: [source ids], reason}]` names builds that interfere when installed together,
e.g. two builds fighting over the same input devices. A consumer SHOULD detect such duplicates among
installed copies and explain `reason`. It MUST NOT remove a copy without approval.

## 9. User data

`user_data.paths` lists where the application keeps its data, so that uninstalling can offer to keep
or remove it explicitly. Each path:

- MUST start with `~/`, `$XDG_DATA_HOME/`, `$XDG_CONFIG_HOME/`, `$XDG_CACHE_HOME/` or
  `$XDG_STATE_HOME/`;
- MUST name a specific folder: no globs, no `.`/`..`, no `//`, at most 200 characters, and not
  merely `~/.config`, `~/.local` or `~/.cache`.

Consumers MUST keep user data unless the user explicitly asks for it to be removed.

## 10. Consumer obligations (summary)

A conforming consumer:

1. validates strictly (§2, §4) and assigns trust before using anything (§3);
2. never executes text from a manifest. The only effects are the actions of §7, after approval;
3. evaluates probes read-only, bounded and offline (§6.3), and never turns missing evidence into a
   fault (§6.4);
4. refuses files that fail a pinned verification, without falling back to a weaker check (§5.4);
5. journals every change it makes so that it can be undone. It removes only what it created, and
   only if that is still unchanged;
6. treats the manifest as advice. Package managers and signatures stay authoritative; a manifest can
   never override them.

## 11. Example

A minimal manifest for an AppImage with one companion service:

```json
{
  "manifest_version": 1,
  "serial": 2026100701,
  "expires": "2027-10-01T00:00:00Z",
  "application": {
    "id": "org.example.Recorder",
    "name": "Recorder",
    "vendor": {"name": "Example", "domain": "example.org"},
    "platforms": [{"os": "linux", "arch": ["x86_64"]}]
  },
  "sources": [{
    "id": "appimage",
    "format": "appimage",
    "url": "https://downloads.example.org/Recorder-x86_64.AppImage",
    "update": {"type": "zsync", "url": "https://downloads.example.org/Recorder-x86_64.AppImage.zsync"},
    "verification": {"type": "appimage-openpgp", "openpgp_fingerprint": "0123456789ABCDEF0123456789ABCDEF01234567"},
    "recommended_when": ["relocatable", "default"]
  }],
  "features": [
    {"id": "app", "name": "Recording", "core": true},
    {"id": "capture-service", "name": "System audio capture", "requires": ["capture"]}
  ],
  "components": [{
    "id": "capture",
    "name": "Capture service",
    "type": "system-service",
    "relation": "hard",
    "action": {"kind": "pacman.install_repo", "names": ["example-capture"]},
    "post": [{"kind": "systemd.enable_now", "unit": "example-capture.service"}],
    "verify": [{"probe": "systemd_unit", "unit": "example-capture.service"}],
    "privileges": ["runs as root", "reads audio devices"]
  }],
  "user_data": {"paths": ["~/.config/example-recorder"]}
}
```

The curated manifests shipped with Cygnus (`cygnus/data/manifests/`) are complete real-world
examples, e.g. WhatPulse with its pcap service, input permissions and browser extension.

Check a manifest with `cygnus manifest validate FILE [FILE…]`, which also verifies a `.minisig` next
to it. Print the schema with `cygnus manifest schema`.
