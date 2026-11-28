-- 019: games (Spin of Fortune first; lottery and others later). One generic set of tables serves every game.
--
-- Nothing here ever creates or moves money. A win only creates a PRIZE record. A prize becomes money only when staff pay it
-- through the normal bulk-payment pipeline (alliance-owned funds, limits, second approver, confirmation, at-most-once sending).
-- (Version 018 is reserved for the platform-only registry migration, which lives in platform_migrations/.)

CREATE TABLE game_outcomes (                 -- the prize table of a game: what can be won and how likely it is
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game         TEXT NOT NULL,
    label        TEXT NOT NULL,
    weight       INTEGER NOT NULL CHECK (weight >= 0),
    amounts_json TEXT NOT NULL DEFAULT '{}', -- what is paid ('{}' = nothing, e.g. "try again")
    score        INTEGER NOT NULL DEFAULT 0, -- leaderboard points
    jackpot      INTEGER NOT NULL DEFAULT 0 CHECK (jackpot IN (0,1)),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    updated_by   TEXT NOT NULL
);
CREATE INDEX idx_game_outcomes_game ON game_outcomes(game, enabled);

CREATE TABLE game_plays (                    -- every play ever made. Append-only: history is never edited or deleted.
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    game              TEXT NOT NULL,
    play_key          TEXT NOT NULL UNIQUE,  -- e.g. spin:<nation>:<day>:<n>. The database itself refuses a play over the daily limit
    nation_id         INTEGER NOT NULL,
    discord_id        TEXT NOT NULL,
    played_at         TEXT NOT NULL,
    day_key           TEXT NOT NULL,
    outcome_id        INTEGER,
    outcome_label     TEXT NOT NULL,
    amounts_json      TEXT NOT NULL,
    value_cents       INTEGER,
    score             INTEGER NOT NULL,
    jackpot           INTEGER NOT NULL,
    odds_json         TEXT NOT NULL,         -- the odds exactly as they were at that moment, so any result can be checked later
    price_snapshot_id INTEGER
);
CREATE INDEX idx_game_plays_nation ON game_plays(game, nation_id, day_key);
CREATE INDEX idx_game_plays_time ON game_plays(game, played_at);
CREATE TRIGGER trg_game_plays_nodelete BEFORE DELETE ON game_plays
BEGIN SELECT RAISE(ABORT, 'game plays can never be deleted'); END;
CREATE TRIGGER trg_game_plays_noupdate BEFORE UPDATE ON game_plays
BEGIN SELECT RAISE(ABORT, 'game plays can never be changed'); END;

CREATE TABLE game_prizes (                   -- what the alliance owes a winner. Paid through bulk batches, never automatically.
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    play_id          INTEGER NOT NULL UNIQUE REFERENCES game_plays(id),
    game             TEXT NOT NULL,
    nation_id        INTEGER NOT NULL,
    amounts_json     TEXT NOT NULL,
    value_cents      INTEGER,
    status           TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','CANCELLED')),
    batch_id         INTEGER,                -- the bulk batch paying it (its real state is read from bulk_items)
    row_no           INTEGER,
    cancelled_by     TEXT,
    cancelled_reason TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_game_prizes_status ON game_prizes(status, nation_id);
CREATE TRIGGER trg_game_prizes_nodelete BEFORE DELETE ON game_prizes
BEGIN SELECT RAISE(ABORT, 'game prizes can never be deleted'); END;
CREATE TRIGGER trg_game_prizes_fixed BEFORE UPDATE ON game_prizes
WHEN OLD.play_id != NEW.play_id OR OLD.nation_id != NEW.nation_id OR OLD.amounts_json != NEW.amounts_json OR OLD.game != NEW.game
BEGIN SELECT RAISE(ABORT, 'a prize''s winner and amounts can never be changed'); END;
CREATE TRIGGER trg_game_prizes_cancel_once BEFORE UPDATE ON game_prizes
WHEN OLD.status = 'CANCELLED' AND NEW.status != 'CANCELLED'
BEGIN SELECT RAISE(ABORT, 'a cancelled prize can not be brought back'); END;
