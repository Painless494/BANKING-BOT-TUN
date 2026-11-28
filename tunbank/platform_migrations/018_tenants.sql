-- 018: the platform registry (data/platform.db ONLY). One row per alliance the owner has approved.
-- Alliance accounting databases never contain this table: each alliance has its own database file.
CREATE TABLE tenants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alliance_id  INTEGER NOT NULL UNIQUE,                 -- the Politics & War alliance id, frozen once created
    guild_id     INTEGER NOT NULL UNIQUE,                 -- the Discord server this alliance uses the bot in
    name         TEXT NOT NULL,
    admin_ids    TEXT NOT NULL DEFAULT '',                -- comma separated Discord user ids who administer THIS alliance
    status       TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED')),
    keys_enc     TEXT,                                    -- the alliance's PnW keys, encrypted (never plain text); NULL until /setup keys
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
-- Registry rows are never deleted, and the alliance a row belongs to can never be changed.
CREATE TRIGGER tenants_no_delete BEFORE DELETE ON tenants
BEGIN SELECT RAISE(ABORT, 'tenants rows are never deleted: suspend the alliance instead'); END;
CREATE TRIGGER tenants_alliance_frozen BEFORE UPDATE OF alliance_id, created_at, created_by ON tenants
BEGIN SELECT RAISE(ABORT, 'a tenant''s alliance id and creation record can never be changed'); END;

-- Owner-maintained game data shared by every alliance (see /grantset rule and /grantset projectimport).
CREATE TABLE platform_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);
