"""/economy report and /economy auto: what the alliance earned and spent, on demand or on a weekly / monthly schedule."""
from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import econ_report as ER
from . import ledger as L
from .config import cfg_bool, cfg_get, cfg_set
from .ui import Services, need, reply, thinking, xlsx_file

PERIOD_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in ER.CHOICES.items()]
DONATION_CHOICES = [app_commands.Choice(name="Use the saved setting", value="setting"),
                    app_commands.Choice(name="Include donations as revenue", value="include"),
                    app_commands.Choice(name="Tax only (leave donations out)", value="exclude")]
ONOFF = [app_commands.Choice(name="On", value="1"), app_commands.Choice(name="Off", value="0")]


def register(economy: app_commands.Group, svc: Services):
    def uid(i) -> str:
        return str(i.user.id)

    @economy.command(name="report", description="Revenue, expenditure and net result of the alliance for a week, a month or any dates")
    @app_commands.describe(period="Which period (default: last 7 days)", start="Custom only: first day, like 2026-10-01",
                           end="Custom only: last day (default: today)", donations="Count alliance donations as revenue?",
                           detail="Also attach every line as a spreadsheet file")
    @app_commands.choices(period=PERIOD_CHOICES, donations=DONATION_CHOICES)
    async def report(interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None, start: str = "", end: str = "",
                     donations: Optional[app_commands.Choice[str]] = None, detail: bool = False):
        if not await need(svc, interaction, "FLAG:bank_view_alliance_holdings"):
            return
        await thinking(interaction)
        try:
            p = ER.period_for(period.value if period else "7d", start=start, end=end)
        except ER.ReportError as exc:
            return await reply(interaction, str(exc))
        mode = donations.value if donations else "setting"
        card, rep, snap = await ER.generate(svc, p, None if mode == "setting" else mode == "include")
        file = xlsx_file(await asyncio.to_thread(ER.detail_csv, rep, snap), f"economy-{p.start:%Y%m%d}-{(p.end - ER.dt.timedelta(days=1)):%Y%m%d}.csv") if detail else None
        await reply(interaction, card=card, file=file)

    @economy.command(name="auto", description="ECON: show or change the automatic weekly / monthly economy report")
    @app_commands.describe(weekly="Post a report every Monday for the week that just ended", monthly="Post a report on the 1st for the month that just ended",
                           donations="Count alliance donations as revenue in reports", channel="Where to post (default: the ECON log channel)")
    @app_commands.choices(weekly=ONOFF, monthly=ONOFF, donations=ONOFF)
    async def auto(interaction: discord.Interaction, weekly: Optional[app_commands.Choice[str]] = None, monthly: Optional[app_commands.Choice[str]] = None,
                   donations: Optional[app_commands.Choice[str]] = None, channel: Optional[discord.TextChannel] = None):
        changes = {k: v.value for k, v in (("econ_report_weekly", weekly), ("econ_report_monthly", monthly), ("econ_report_donations", donations)) if v}
        if channel is not None:
            changes["econ_report_channel_id"] = str(channel.id)
        if changes:
            if not await need(svc, interaction, "MINISTER"):
                return

            def save():
                with svc.db.tx() as conn:
                    for k, v in changes.items():
                        old = cfg_get(conn, k)
                        if old != v:
                            cfg_set(conn, k, v, uid(interaction))
                            L.audit(conn, uid(interaction), "CONFIG_CHANGED", k, {"value": v, "previous": old})
            await asyncio.to_thread(save)
        elif not await need(svc, interaction, "FLAG:bank_view_alliance_holdings"):
            return
        with svc.db.read() as conn:
            wk, mo, don = cfg_bool(conn, "econ_report_weekly"), cfg_bool(conn, "econ_report_monthly"), cfg_bool(conn, "econ_report_donations")
            chan = cfg_get(conn, "econ_report_channel_id") or cfg_get(conn, "econ_log_channel_id")
        yes = lambda b: "✅ on" if b else "⬜ off"   # noqa: E731
        c = A.Card("📊 Automatic economy reports", "Weekly = every Monday for Mon–Sun. Monthly = on the 1st for the month before. "
                   "Switching one on never posts an old report: the first one is the next to finish.", A.GREEN if changes else A.BLUE)
        c.add("Weekly", yes(wk), True)
        c.add("Monthly", yes(mo), True)
        c.add("Donations count as revenue", yes(don), True)
        c.add("Posts to", f"<#{chan}>" if str(chan).isdigit() else "⚠️ no channel yet: set one here or with `/bankset setlogchannel`")
        await reply(interaction, card=c)
