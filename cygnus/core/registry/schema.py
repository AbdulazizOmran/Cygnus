"""Registry schema migrations (architecture §5).

Migrations are append-only: never edit a released step, add a new one.
Each entry is (version, description, SQL script).
"""

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
CREATE TABLE storage_location (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    fs_uuid         TEXT,
    partuuid        TEXT,
    fs_type         TEXT NOT NULL,
    view_root       TEXT NOT NULL DEFAULT '/',
    subpath         TEXT NOT NULL DEFAULT '',
    canonical_mount TEXT,
    class           TEXT NOT NULL CHECK (class IN ('system','posix','user-owned','network','limited')),
    removable       INTEGER NOT NULL DEFAULT 0,
    rotational      INTEGER NOT NULL DEFAULT 0,
    capabilities    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(capabilities)),
    probed_at       TEXT,
    probe_boot_id   TEXT,
    user_apps_dir   TEXT,
    is_default      INTEGER NOT NULL DEFAULT 0,
    reserve_bytes   INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'online' CHECK (state IN ('online','offline','degraded')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (fs_uuid, view_root, subpath)
) STRICT;
CREATE UNIQUE INDEX storage_location_one_default ON storage_location(is_default) WHERE is_default = 1;

CREATE TABLE manifest (
    id            TEXT PRIMARY KEY,
    app_id        TEXT NOT NULL,
    source_url    TEXT,
    serial        INTEGER NOT NULL,
    expires       TEXT,
    signer_key_id TEXT,
    trust_level   TEXT NOT NULL CHECK (trust_level IN ('vendor-signed','curated','package-metadata','unverified')),
    raw           TEXT NOT NULL CHECK (json_valid(raw)),
    verified_at   TEXT
) STRICT;
CREATE INDEX manifest_app ON manifest(app_id);

CREATE TABLE application (
    id                      TEXT PRIMARY KEY,
    display_name            TEXT NOT NULL,
    vendor                  TEXT,
    appstream_id            TEXT,
    icon                    TEXT,
    primary_installation_id TEXT,
    manifest_id             TEXT REFERENCES manifest(id) ON DELETE SET NULL,
    trust_level             TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE installation (
    id              TEXT PRIMARY KEY,
    application_id  TEXT NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    format          TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(source)),
    version         TEXT,
    arch            TEXT,
    location_id     TEXT REFERENCES storage_location(id),
    origin          TEXT NOT NULL CHECK (origin IN ('installed','adopted')),
    update_provider TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(update_provider)),
    state           TEXT NOT NULL DEFAULT 'ok' CHECK (state IN
                    ('ok','degraded','broken','offline','partial','removed_externally','changed_externally')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;
CREATE INDEX installation_app ON installation(application_id);

CREATE TABLE component (
    id              TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    ref             TEXT NOT NULL,
    relation        TEXT NOT NULL CHECK (relation IN ('hard','recommended','optional','unverified')),
    required_for    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(required_for)),
    placement       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(placement)),
    installed_by    TEXT NOT NULL CHECK (installed_by IN ('cygnus','system','vendor','user')),
    ledger_ref      TEXT,
    pinned          INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX component_installation ON component(installation_id);

CREATE TABLE feature (
    id             TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    key            TEXT NOT NULL,
    name           TEXT NOT NULL,
    required       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (application_id, key)
) STRICT;

CREATE TABLE requirement (
    id           TEXT PRIMARY KEY,
    feature_id   TEXT NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    component_id TEXT REFERENCES component(id) ON DELETE SET NULL,
    probe        TEXT NOT NULL CHECK (json_valid(probe)),
    severity     TEXT NOT NULL CHECK (severity IN ('hard','recommended','optional'))
) STRICT;

CREATE TABLE operation (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    app_id         TEXT,
    plan           TEXT NOT NULL CHECK (json_valid(plan)),
    plan_digest    TEXT NOT NULL,
    helper_plan_id TEXT,
    state          TEXT NOT NULL CHECK (state IN
                   ('planned','running','succeeded','failed','rolled_back','needs_attention')),
    started        TEXT,
    finished       TEXT
) STRICT;

CREATE TABLE operation_step (
    op_id        TEXT NOT NULL REFERENCES operation(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    action       TEXT NOT NULL CHECK (json_valid(action)),
    state        TEXT NOT NULL CHECK (state IN ('pending','running','done','failed','compensated','skipped')),
    compensation TEXT CHECK (compensation IS NULL OR json_valid(compensation)),
    result       TEXT CHECK (result IS NULL OR json_valid(result)),
    started      TEXT,
    finished     TEXT,
    PRIMARY KEY (op_id, seq)
) STRICT;

CREATE TABLE artifact (
    id              TEXT PRIMARY KEY,
    installation_id TEXT REFERENCES installation(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    locator         TEXT NOT NULL,
    scope           TEXT NOT NULL CHECK (scope IN ('user','system')),
    sha256          TEXT,
    created_by_op   TEXT REFERENCES operation(id) ON DELETE SET NULL,
    ownership       TEXT NOT NULL CHECK (ownership IN ('created','adopted')),
    on_uninstall    TEXT NOT NULL DEFAULT 'ask' CHECK (on_uninstall IN ('remove','keep','ask')),
    refcount        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (kind, locator)
) STRICT;

CREATE TABLE health_result (
    installation_id TEXT NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    feature_id      TEXT NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN
                    ('ok','warn','missing','broken','unknown','likely_failing','offline')),
    evidence        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence)),
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (installation_id, feature_id)
) STRICT;

CREATE TABLE issue (
    id              TEXT PRIMARY KEY,
    op_id           TEXT REFERENCES operation(id) ON DELETE SET NULL,
    installation_id TEXT REFERENCES installation(id) ON DELETE CASCADE,
    code            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    facts           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(facts)),
    resolutions     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(resolutions)),
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE recovery_event (
    id               TEXT PRIMARY KEY,
    issue_id         TEXT REFERENCES issue(id) ON DELETE CASCADE,
    resolution_id    TEXT NOT NULL,
    approved_by_user INTEGER NOT NULL,
    outcome          TEXT NOT NULL,
    details          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details)),
    at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE history (
    id              INTEGER PRIMARY KEY,
    installation_id TEXT REFERENCES installation(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    details         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details)),
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
) STRICT;

CREATE TABLE update_check (
    installation_id TEXT PRIMARY KEY REFERENCES installation(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    current         TEXT,
    available       TEXT,
    checked_at      TEXT NOT NULL,
    notes           TEXT
) STRICT;
""",
    ),
]
