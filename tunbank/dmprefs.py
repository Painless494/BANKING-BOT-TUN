"""Who gets which bank DM. One place decides, so the queued DMs (outbox) and the direct DMs follow exactly the same rules.

A DM is sent only if ALL of these allow it:
  1. the master switch `dm_members`;
  2. the switch of its category (`dm_deposits`, `dm_withdrawals`, `dm_transfers`, `dm_changes`), set by staff for the whole alliance;
  3. the member's own choice, only when staff allow members to choose (`dm_member_choice`). A member can never mute the notice that money
     left their account outside TUN Bank (EXTERNAL_WITHDRAWAL): only staff can switch that one off, through the withdrawals switch.
The decision is taken when the DM is about to be delivered. The movement itself is never affected.
"""
from __future__ import annotations

from .config import cfg_bool
from .util import now_iso

CATEGORIES = {
    "deposits": ("Deposits", "A deposit of yours was credited to your account"),
    "withdrawals": ("Withdrawals", "Money was withdrawn from your account (by you, by staff, or outside TUN Bank)"),
    "transfers": ("Transfers", "Another member sent you a balance transfer, or you sent one"),
    "changes": ("Staff changes", "Staff adjusted your balance, reserved or released funds, or an overdue loan was settled from your deposit"),
}
KIND_CATEGORY = {"WITHDRAWAL": "withdrawals", "EXTERNAL_WITHDRAWAL": "withdrawals", "CREDIT": "transfers", "DEBIT": "transfers",
                 "ADJUSTMENT": "changes"}
MEMBER_CANNOT_MUTE = {"EXTERNAL_WITHDRAWAL"}


def category_of(kind: str | None) -> str | None:
    return KIND_CATEGORY.get(kind or "")


def staff_enabled(conn, category: str) -> bool:
    return cfg_bool(conn, f"dm_{category}")


def members_may_choose(conn) -> bool:
    return cfg_bool(conn, "dm_member_choice")


def member_choice(conn, nation_id: int, category: str) -> bool:
    r = conn.execute("SELECT enabled FROM member_dm_prefs WHERE nation_id=? AND category=?", (nation_id, category)).fetchone()
    return True if r is None else bool(r["enabled"])


def decide(conn, *, category: str | None, nation_id: int | None = None, discord_id=None, kind: str | None = None) -> tuple[bool, str]:
    """(send?, why not). `category` None means a DM with no category: only the master switch applies."""
    if not cfg_bool(conn, "dm_members"):
        return False, "member DMs are switched off (dm_members)"
    if category is None:
        return True, ""
    if not staff_enabled(conn, category):
        return False, f"{CATEGORIES[category][0].lower()} DMs are switched off by staff (dm_{category})"
    if members_may_choose(conn) and kind not in MEMBER_CANNOT_MUTE:
        if nation_id is None and discord_id is not None:
            r = conn.execute("SELECT nation_id FROM members WHERE discord_id=?", (str(discord_id),)).fetchone()
            nation_id = r["nation_id"] if r else None
        if nation_id is not None and not member_choice(conn, nation_id, category):
            return False, f"the member switched {CATEGORIES[category][0].lower()} DMs off"
    return True, ""


def effective(conn, nation_id: int) -> dict:
    """What this member would get right now, per category: (on?, who decided)."""
    master = cfg_bool(conn, "dm_members")
    choose = members_may_choose(conn)
    out = {}
    for cat in CATEGORIES:
        if not master or not staff_enabled(conn, cat):
            out[cat] = (False, "alliance")
        elif choose and not member_choice(conn, nation_id, cat):
            out[cat] = (False, "you")
        else:
            out[cat] = (True, "alliance" if not choose else "you")
    return out


def set_choice(conn, nation_id: int, category: str, enabled: bool) -> None:
    conn.execute("INSERT INTO member_dm_prefs(nation_id,category,enabled,updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(nation_id,category) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                 (nation_id, category, int(enabled), now_iso()))
