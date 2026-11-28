-- 020: members' own choice about bank DMs (only used when staff allow members to choose: setting dm_member_choice).
-- A row exists only when a member changed something; no row = the default (receive it).
CREATE TABLE member_dm_prefs (
    nation_id  INTEGER NOT NULL,
    category   TEXT NOT NULL CHECK (category IN ('deposits','withdrawals','transfers','changes')),
    enabled    INTEGER NOT NULL CHECK (enabled IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (nation_id, category)
);
