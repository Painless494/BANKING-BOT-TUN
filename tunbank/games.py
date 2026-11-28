"""Games framework: the part every game shares. Spin of Fortune is the first game; a lottery and others can be added the same way.

What a game gets from here
  * a configurable prize table (game_outcomes) with odds, scores and a jackpot flag - nothing about prizes is hard-coded;
  * limits per day that the database itself enforces (a double click or two simultaneous presses can never give a second play);
  * every play recorded forever, WITH the odds as they were at that moment, so any result can be checked later;
  * prizes recorded as what the alliance OWES (game_prizes). A win never creates or moves money. A prize is paid only through the
    normal bulk-payment pipeline: alliance-owned funds, limits, second approver, confirmation, never sent twice;
  * stats, leaderboards (per season) and history, all calculated from the plays so they can never drift out of line.

To add a game: create a GameDef, register it in GAMES, give it its settings in config.py / config_view.py, and write its play logic
using `play_daily()` (or its own function that records through `record_play()`). The /games and /gameset commands, the prize table,
payouts, stats and the leaderboard then work for it automatically.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import sqlite3

from . import ledger as L
from . import money as M
from .util import ISO_FMT, jdump, now_iso, utcnow
from .valuation import value_amounts


class GameError(Exception):
    """The message is safe to show to the player."""


@dataclasses.dataclass(frozen=True)
class GameDef:
    key: str
    name: str
    emoji: str
    blurb: str
    defaults: tuple = ()          # (label, weight, "money=5m coal=100" or "", score, jackpot)


SPIN_DEFAULTS = (
    ("300 steel", 22, "steel=300", 30, False),
    ("200 uranium", 20, "uranium=200", 40, False),
    ("$250,000", 18, "money=250k", 10, False),
    ("$5,000,000", 10, "money=5m", 100, False),
    ("1,000 gasoline", 10, "gasoline=1000", 50, False),
    ("1,000 aluminum", 5, "aluminum=1000", 70, False),
    ("Try again tomorrow", 15, "", 0, False),
    ("$30,000,000 JACKPOT", 1, "money=30m", 500, True),
)

GAMES: dict = {
    "spin": GameDef("spin", "Spin of Fortune", "🎰", "Spin the wheel for a prize. Prizes are paid by the alliance bank.", SPIN_DEFAULTS),
}


@dataclasses.dataclass
class Outcome:
    id: int
    game: str
    label: str
    weight: int
    amounts: dict
    score: int
    jackpot: bool
    enabled: bool

    @property
    def pays(self) -> bool:
        return bool(self.amounts)


@dataclasses.dataclass
class PlayResult:
    play_id: int
    outcome: Outcome
    prize_id: int | None
    value_cents: int | None
    spins_left: int
    day_key: str
    chance_pct: float


# ---------------------------------------------------------------------------------------------------- days
def day_key(now: dt.datetime, start_hour: int = 0) -> str:
    """The game day a moment belongs to. With start_hour 0 this is the UTC date, the same day PnW uses."""
    return (now - dt.timedelta(hours=start_hour)).strftime("%Y-%m-%d")


def next_reset(now: dt.datetime, start_hour: int = 0) -> dt.datetime:
    base = (now - dt.timedelta(hours=start_hour)).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + dt.timedelta(days=1, hours=start_hour)


def until(now: dt.datetime, then: dt.datetime) -> str:
    secs = max(0, int((then - now).total_seconds()))
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h {m}m" if h else f"{max(m, 1)}m"


# ---------------------------------------------------------------------------------------------------- the prize table
def _outcome(r) -> Outcome:
    return Outcome(r["id"], r["game"], r["label"], r["weight"], json.loads(r["amounts_json"]), r["score"], bool(r["jackpot"]), bool(r["enabled"]))


def load_outcomes(conn, game: str, only_enabled: bool = True) -> list:
    sql = "SELECT * FROM game_outcomes WHERE game=?" + (" AND enabled=1" if only_enabled else "") + " ORDER BY id"
    return [_outcome(r) for r in conn.execute(sql, (game,))]


def validate_outcome(label: str, weight: int, amounts: dict, score: int):
    label = (label or "").strip()
    if not label or len(label) > 60:
        raise GameError("Give the prize a name of 1-60 characters.")
    if not isinstance(weight, int) or weight < 0 or weight > 1_000_000:
        raise GameError("Weight must be a whole number from 0 to 1,000,000 (the bigger it is, the more often it comes up).")
    if score < 0 or score > 1_000_000:
        raise GameError("Score must be 0 or more.")
    return label


def save_outcome(conn, game: str, actor: str, *, outcome_id: int | None, label: str | None, weight: int | None, amounts: dict | None,
                 score: int | None, jackpot: bool | None, enabled: bool | None) -> int:
    now = now_iso()
    if outcome_id is None:
        if label is None or weight is None:
            raise GameError("A new prize needs at least a name and a weight.")
        label = validate_outcome(label, weight, amounts or {}, score or 0)
        cur = conn.execute("INSERT INTO game_outcomes(game,label,weight,amounts_json,score,jackpot,enabled,created_at,updated_at,updated_by) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (game, label, weight, jdump(M.clean(amounts or {})), score or 0, int(bool(jackpot)), int(enabled is not False), now, now, actor))
        oid = cur.lastrowid
        L.audit(conn, actor, "GAME_PRIZE_ADDED", f"{game}:{oid}", {"label": label, "weight": weight, "amounts": amounts or {}})
        return oid
    row = conn.execute("SELECT * FROM game_outcomes WHERE id=? AND game=?", (outcome_id, game)).fetchone()
    if not row:
        raise GameError(f"There is no prize #{outcome_id} in this game's table.")
    new = {
        "label": row["label"] if label is None else label, "weight": row["weight"] if weight is None else weight,
        "amounts_json": row["amounts_json"] if amounts is None else jdump(M.clean(amounts)),
        "score": row["score"] if score is None else score, "jackpot": row["jackpot"] if jackpot is None else int(bool(jackpot)),
        "enabled": row["enabled"] if enabled is None else int(bool(enabled)),
    }
    validate_outcome(new["label"], new["weight"], {}, new["score"])
    conn.execute("UPDATE game_outcomes SET label=?, weight=?, amounts_json=?, score=?, jackpot=?, enabled=?, updated_at=?, updated_by=? WHERE id=?",
                 (new["label"], new["weight"], new["amounts_json"], new["score"], new["jackpot"], new["enabled"], now, actor, outcome_id))
    L.audit(conn, actor, "GAME_PRIZE_CHANGED", f"{game}:{outcome_id}", {"before": {k: row[k] for k in new}, "after": new})
    return outcome_id


def remove_outcome(conn, game: str, actor: str, outcome_id: int) -> str:
    row = conn.execute("SELECT * FROM game_outcomes WHERE id=? AND game=?", (outcome_id, game)).fetchone()
    if not row:
        raise GameError(f"There is no prize #{outcome_id} in this game's table.")
    conn.execute("DELETE FROM game_outcomes WHERE id=?", (outcome_id,))        # plays keep their own copy of what was won, so history is safe
    L.audit(conn, actor, "GAME_PRIZE_REMOVED", f"{game}:{outcome_id}", {"label": row["label"], "weight": row["weight"], "amounts": json.loads(row["amounts_json"])})
    return row["label"]


def load_defaults(conn, game: str, actor: str) -> int:
    """Fill an EMPTY prize table with the game's classic prizes. Refuses to touch a table that already has prizes."""
    if conn.execute("SELECT 1 FROM game_outcomes WHERE game=?", (game,)).fetchone():
        raise GameError("This game already has prizes. Remove them first if you really want to start from the classic table.")
    for label, weight, amounts_text, score, jackpot in GAMES[game].defaults:
        save_outcome(conn, game, actor, outcome_id=None, label=label, weight=weight, amounts=M.parse_amounts(amounts_text) if amounts_text else {},
                     score=score, jackpot=jackpot, enabled=True)
    return len(GAMES[game].defaults)


def odds(outcomes: list) -> list:
    """[(outcome, chance %)] for every prize that can come up."""
    live = [o for o in outcomes if o.enabled and o.weight > 0]
    total = sum(o.weight for o in live)
    return [(o, 100.0 * o.weight / total) for o in live] if total else []


def expected_value(outcomes: list, snap) -> dict:
    """What one play costs the alliance on average, and the most it can cost, valued at the given prices."""
    table = odds(outcomes)
    ev, top, complete = 0.0, 0, True
    for o, pct in table:
        if not o.amounts:
            continue
        v = value_amounts(o.amounts, snap)
        if v.total_cents is None or not v.complete:
            complete = False
        cents = v.total_cents or 0
        ev += cents * pct / 100
        top = max(top, cents)
    return {"average_cents": int(round(ev)), "max_cents": top, "complete": complete and bool(table)}


# ---------------------------------------------------------------------------------------------------- playing
def _draw(pool: list, rng) -> Outcome:
    total = sum(o.weight for o in pool)
    pick = rng.randrange(total)
    for o in pool:
        if pick < o.weight:
            return o
        pick -= o.weight
    return pool[-1]                                                          # unreachable; keeps the function total


def play_daily(conn, *, game: str, nation_id: int, discord_id: str, per_day: int, day_start_hour: int, cap_cents: int, snap,
               rng=None, now: dt.datetime | None = None) -> PlayResult:
    """One play of a daily game, all inside the caller's transaction: check the limit, apply the prize cap, draw, record.

    The draw uses the operating system's secure random source. The daily limit is enforced twice: by counting, and by the UNIQUE
    play_key, so even two presses at the same instant cannot both succeed."""
    rng = rng or secrets.SystemRandom()
    now = now or utcnow()
    day = day_key(now, day_start_hour)
    used = conn.execute("SELECT COUNT(*) FROM game_plays WHERE game=? AND nation_id=? AND day_key=?", (game, nation_id, day)).fetchone()[0]
    if used >= max(per_day, 0):
        raise GameError(f"You have already played today. Come back in **{until(now, next_reset(now, day_start_hour))}**.")
    outcomes = [o for o in load_outcomes(conn, game) if o.weight > 0]
    if not outcomes:
        raise GameError("This game has no prizes set up yet. Ask staff to check `/gameset show`.")

    eligible, remaining = outcomes, None
    if cap_cents > 0:
        if snap is None:
            raise GameError("Live prices are not available right now, so the daily prize budget can't be checked. Try again in a few minutes.")
        spent = conn.execute("SELECT COALESCE(SUM(value_cents),0) FROM game_plays WHERE game=? AND day_key=?", (game, day)).fetchone()[0]
        remaining = cap_cents - spent
        eligible = []
        for o in outcomes:
            if not o.amounts:
                eligible.append(o)
                continue
            v = value_amounts(o.amounts, snap)
            if v.complete and v.total_cents is not None and v.total_cents <= remaining:
                eligible.append(o)
        if not eligible:
            raise GameError("Today's prize budget has been used up. Please try again after the daily reset.")
    won = _draw(eligible, rng)
    total_w = sum(o.weight for o in eligible)
    value = None
    if won.amounts and snap is not None:
        v = value_amounts(won.amounts, snap)
        value = v.total_cents if v.complete else None
    table = [{"id": o.id, "label": o.label, "weight": o.weight, "in_draw": o in eligible, "chance_pct": round(100 * o.weight / total_w, 4) if o in eligible else 0}
             for o in outcomes]
    odds_json = jdump({"total_weight": total_w, "table": table, "cap_cents": cap_cents or None, "remaining_before": remaining})
    try:
        cur = conn.execute(
            "INSERT INTO game_plays(game,play_key,nation_id,discord_id,played_at,day_key,outcome_id,outcome_label,amounts_json,value_cents,score,jackpot,"
            "odds_json,price_snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (game, f"{game}:{nation_id}:{day}:{used + 1}", nation_id, str(discord_id), now.strftime(ISO_FMT), day, won.id, won.label, jdump(won.amounts), value,
             won.score, int(won.jackpot), odds_json, getattr(snap, "id", None)))
    except sqlite3.IntegrityError:
        raise GameError("You have already played today.") from None
    play_id = cur.lastrowid
    prize_id = None
    if won.amounts:
        prize_id = conn.execute("INSERT INTO game_prizes(play_id,game,nation_id,amounts_json,value_cents,status,created_at,updated_at) "
                                "VALUES(?,?,?,?,?,'PENDING',?,?)",
                                (play_id, game, nation_id, jdump(won.amounts), value, now_iso(), now_iso())).lastrowid
    L.audit(conn, str(discord_id), "GAME_PLAY", f"{game}:{play_id}", {"nation": nation_id, "outcome": won.label, "amounts": won.amounts, "prize_id": prize_id})
    return PlayResult(play_id, won, prize_id, value, max(per_day - used - 1, 0), day, 100.0 * won.weight / total_w)


# ---------------------------------------------------------------------------------------------------- prizes and payments
STATE_SQL = ("CASE WHEN p.status='CANCELLED' THEN 'CANCELLED' WHEN p.batch_id IS NULL THEN 'PENDING' ELSE COALESCE("
             "(SELECT CASE i.status WHEN 'COMPLETED' THEN 'PAID' WHEN 'FAILED' THEN 'FAILED' ELSE 'SENDING' END "
             "FROM bulk_items i WHERE i.batch_id=p.batch_id AND i.row_no=p.row_no),'PENDING') END")
STATE_TEXT = {"PENDING": "⏳ waiting to be paid", "SENDING": "📤 being sent", "PAID": "✅ paid", "FAILED": "⚠️ payment failed, will be retried", "CANCELLED": "🚫 cancelled"}


def refresh_payments(conn) -> None:
    """Bring bulk items in line with their real transactions, so a prize shows as paid as soon as the payment settled."""
    from . import bulk as B
    for r in conn.execute("SELECT DISTINCT p.batch_id FROM game_prizes p JOIN bulk_items i ON i.batch_id=p.batch_id AND i.row_no=p.row_no "
                          "WHERE i.status IN ('PENDING','UNCERTAIN') AND i.tx_id IS NOT NULL").fetchall():
        B.refresh_items(conn, r["batch_id"])


def payable_prizes(conn, game: str | None = None, ids: list | None = None, limit: int = 100) -> list:
    """Prizes nobody is paying yet (or whose last payment attempt FAILED), oldest first."""
    sql = f"SELECT p.*, {STATE_SQL} AS state FROM game_prizes p WHERE 1=1"
    args: list = []
    if game:
        sql += " AND p.game=?"
        args.append(game)
    if ids:
        sql += f" AND p.id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    sql += " ORDER BY p.id"
    rows = [dict(r) for r in conn.execute(sql, args)]
    return [r for r in rows if r["state"] in ("PENDING", "FAILED")][:limit]


def cancel_prize(conn, prize_id: int, actor: str, reason: str) -> dict:
    row = conn.execute(f"SELECT p.*, {STATE_SQL} AS state FROM game_prizes p WHERE p.id=?", (prize_id,)).fetchone()
    if not row:
        raise GameError(f"There is no prize #{prize_id}.")
    if row["state"] not in ("PENDING", "FAILED"):
        raise GameError(f"Prize #{prize_id} is {row['state'].lower()}, so it can't be cancelled.")
    if not reason.strip():
        raise GameError("Give a reason: it is kept in the record.")
    conn.execute("UPDATE game_prizes SET status='CANCELLED', cancelled_by=?, cancelled_reason=?, updated_at=? WHERE id=?",
                 (actor, reason.strip()[:300], now_iso(), prize_id))
    L.audit(conn, actor, "GAME_PRIZE_CANCELLED", f"prize:{prize_id}", {"reason": reason.strip()[:300], "amounts": json.loads(row["amounts_json"])})
    return dict(row)


# ---------------------------------------------------------------------------------------------------- stats
def player_stats(conn, game: str, nation_id: int, since: str = "") -> dict:
    since = since or ""
    r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(score),0) s, COALESCE(SUM(value_cents),0) v, MAX(played_at) last FROM game_plays "
                     "WHERE game=? AND nation_id=? AND played_at>?", (game, nation_id, since)).fetchone()
    rank = None
    if r["n"]:
        rank = conn.execute("SELECT COUNT(*)+1 FROM (SELECT nation_id, SUM(score) s FROM game_plays WHERE game=? AND played_at>? GROUP BY nation_id "
                            "HAVING s > ?)", (game, since, r["s"])).fetchone()[0]
    wins = conn.execute("SELECT COUNT(*) FROM game_plays WHERE game=? AND nation_id=? AND played_at>? AND amounts_json!='{}'", (game, nation_id, since)).fetchone()[0]
    best = conn.execute("SELECT outcome_label, value_cents FROM game_plays WHERE game=? AND nation_id=? AND played_at>? ORDER BY score DESC, id DESC LIMIT 1",
                        (game, nation_id, since)).fetchone()
    return {"plays": r["n"], "score": r["s"], "value_cents": r["v"], "last": r["last"], "rank": rank, "wins": wins,
            "best": best["outcome_label"] if best and r["n"] else None}


def leaderboard(conn, game: str, since: str, limit: int) -> list:
    rows = conn.execute("SELECT g.nation_id, COUNT(*) plays, SUM(g.score) score, COALESCE(SUM(g.value_cents),0) value_cents, "
                        "(SELECT discord_id FROM members m WHERE m.nation_id=g.nation_id) did, (SELECT nation_name FROM members m WHERE m.nation_id=g.nation_id) nname "
                        "FROM game_plays g WHERE g.game=? AND g.played_at>? GROUP BY g.nation_id HAVING score>0 "
                        "ORDER BY score DESC, plays ASC, MIN(g.id) ASC LIMIT ?", (game, since or "", max(1, min(limit, 25)))).fetchall()
    return [dict(r) for r in rows]


def history(conn, game: str, nation_id: int, limit: int = 15) -> list:
    return [dict(r) for r in conn.execute(
        f"SELECT g.id, g.played_at, g.outcome_label, g.amounts_json, g.jackpot, p.id AS prize_id, "
        f"CASE WHEN p.id IS NULL THEN NULL ELSE {STATE_SQL} END AS state FROM game_plays g LEFT JOIN game_prizes p ON p.play_id=g.id "
        f"WHERE g.game=? AND g.nation_id=? ORDER BY g.id DESC LIMIT ?", (game, nation_id, limit))]


def plays_today(conn, game: str, day: str) -> dict:
    r = conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT nation_id) players, COALESCE(SUM(value_cents),0) v FROM game_plays WHERE game=? AND day_key=?", (game, day)).fetchone()
    return {"plays": r["n"], "players": r["players"], "value_cents": r["v"]}
