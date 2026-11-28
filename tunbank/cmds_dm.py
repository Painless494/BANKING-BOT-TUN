"""Bank DM controls: /nation dmalerts (staff: which DMs the alliance sends) and /nation alerts (a member's own choice, if staff allow it)."""
from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import dmprefs as D
from . import ledger as L
from .config import cfg_bool, cfg_get, cfg_set
from .ui import Services, need, reply

ON, OFF = "✅ on", "⬜ off"


def register(nation: app_commands.Group, svc: Services):
    @nation.command(name="dmalerts", description="Admin: choose which bank DMs members get (deposits, withdrawals, transfers, staff changes)")
    @app_commands.describe(all_dms="Master switch for every bank DM", deposits="DM when a deposit is credited", withdrawals="DM when money leaves an account",
                           transfers="DM about balance transfers between members", staff_changes="DM about adjustments, reserved funds, loans settled",
                           members_choose="Let each member switch these DMs off for themselves (/nation alerts)")
    async def dmalerts(interaction: discord.Interaction, all_dms: Optional[bool] = None, deposits: Optional[bool] = None, withdrawals: Optional[bool] = None,
                       transfers: Optional[bool] = None, staff_changes: Optional[bool] = None, members_choose: Optional[bool] = None):
        asked = {"dm_members": all_dms, "dm_deposits": deposits, "dm_withdrawals": withdrawals, "dm_transfers": transfers,
                 "dm_changes": staff_changes, "dm_member_choice": members_choose}
        changes = {k: ("1" if v else "0") for k, v in asked.items() if v is not None}
        if not await need(svc, interaction, "ADMIN"):
            return
        if changes:
            def save():
                with svc.db.tx() as conn:
                    for k, v in changes.items():
                        old = cfg_get(conn, k)
                        if old != v:
                            cfg_set(conn, k, v, str(interaction.user.id))
                            L.audit(conn, str(interaction.user.id), "CONFIG_CHANGED", k, {"value": v, "previous": old})
            await asyncio.to_thread(save)
        with svc.db.read() as conn:
            now = {k: cfg_bool(conn, k) for k in asked}
        flag = lambda b: ON if b else OFF   # noqa: E731
        c = A.Card("🔔 Bank DMs to members", "Each DM is checked when it is about to be sent. A DM that was skipped is not sent later if you switch it on again.",
                   A.GREEN if changes else A.BLUE)
        c.add("All bank DMs", flag(now["dm_members"]) + ("" if now["dm_members"] else "  — nothing below is sent"))
        for cat, key in (("deposits", "dm_deposits"), ("withdrawals", "dm_withdrawals"), ("transfers", "dm_transfers"), ("changes", "dm_changes")):
            c.add(D.CATEGORIES[cat][0], f"{flag(now[key])}\n{D.CATEGORIES[cat][1]}", True)
        c.add("Members may switch them off themselves", flag(now["dm_member_choice"]) + "\nThey use `/nation alerts`. The notice for money that left "
              "their account outside TUN Bank can only be switched off here, never by the member.")
        await reply(interaction, card=c)

    @nation.command(name="alerts", description="Choose which bank DMs you get (if your alliance allows it), or see what you get")
    @app_commands.describe(deposits="DM me when a deposit is credited", withdrawals="DM me when money leaves my account",
                           transfers="DM me about balance transfers", staff_changes="DM me about staff changes to my account")
    async def alerts(interaction: discord.Interaction, deposits: Optional[bool] = None, withdrawals: Optional[bool] = None,
                     transfers: Optional[bool] = None, staff_changes: Optional[bool] = None):
        wanted = {"deposits": deposits, "withdrawals": withdrawals, "transfers": transfers, "changes": staff_changes}
        asked = {k: v for k, v in wanted.items() if v is not None}
        with svc.db.read() as conn:
            member = L.member_by_discord(conn, interaction.user.id)
            may = D.members_may_choose(conn)
        if not member:
            return await reply(interaction, "Link your nation first with `/nation link`. Then you can see and choose your bank DMs here.")
        note = ""
        if asked:
            if not may:
                note = "Your alliance decides which bank DMs are sent, so nothing was changed.\n\n"
            else:
                def save():
                    with svc.db.tx() as conn:
                        for cat, on in asked.items():
                            D.set_choice(conn, member["nation_id"], cat, on)
                        L.audit(conn, str(interaction.user.id), "MEMBER_DM_CHOICE", f"nation:{member['nation_id']}", {k: v for k, v in asked.items()})
                await asyncio.to_thread(save)
                note = "Saved.\n\n"
        with svc.db.read() as conn:
            eff = D.effective(conn, member["nation_id"])
        lines = []
        for cat, (label, what) in D.CATEGORIES.items():
            on, who = eff[cat]
            lines.append(f"{'✅' if on else '⬜'} **{label}** — {what}" + ("" if on or who != 'alliance' else "  _(off for everyone)_"))
        c = A.Card("🔔 Your bank DMs", note + "\n".join(lines), A.BLUE)
        c.add("Who decides", "You can switch each one with this command, for example `/nation alerts withdrawals:False`." if may else
              "Your alliance decides which bank DMs are sent. Ask staff if you want a change.")
        c.add("Always on", "If money leaves your account outside TUN Bank, the alliance's notice about it can't be switched off by you." if may else "-")
        await reply(interaction, card=c)
