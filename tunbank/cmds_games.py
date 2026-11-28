"""/games (players) and /gameset (staff): Spin of Fortune, and the shared tools every future game uses (prize table, payouts, stats)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from . import alerts as A
from . import bulk as B
from . import fmt
from . import games as G
from . import icons
from . import identity
from . import ledger as L
from . import limits as LIM
from . import money as M
from .config import cfg_get, cfg_int, cfg_set
from .pnw import PnWRejected, PnWUncertain
from .tenantui import TenantView
from .ui import Services, confirm, has_flag, need, reply, role_ids, thinking
from .util import utcnow
from .valuation import value_amounts

log = logging.getLogger("tunbank.games")
GAME = "spin"
DEF = G.GAMES[GAME]
PERIODS = [app_commands.Choice(name="This season", value="season"), app_commands.Choice(name="All time", value="all")]
PUBLIC = [app_commands.Choice(name="Every result", value="all"), app_commands.Choice(name="Prizes only", value="wins"),
          app_commands.Choice(name="Jackpots only", value="jackpots"), app_commands.Choice(name="Nothing (private only)", value="none")]
GOLD = 0xF1C40F
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def register(games: app_commands.Group, gameset: app_commands.Group, svc: Services):
    def uid(i) -> str:
        return str(i.user.id)

    def settings() -> dict:
        with svc.db.read() as conn:
            return {
                "on": cfg_int(conn, "games_enabled") == 1 and cfg_int(conn, "spin_enabled") == 1, "per_day": max(cfg_int(conn, "spin_per_day"), 0),
                "hour": min(max(cfg_int(conn, "games_day_start_hour_utc"), 0), 23), "cap": cfg_int(conn, "spin_daily_prize_cap") * 100,
                "anim": cfg_int(conn, "spin_animation") == 1, "odds": cfg_int(conn, "games_show_odds") == 1, "stay": cfg_int(conn, "spin_panel_stay_last") == 1, "role": cfg_get(conn, "games_play_role_id").strip(),
                "public": cfg_get(conn, "games_public_results") or "all", "announce": cfg_get(conn, "games_announce_channel_id").strip(),
                "season": cfg_get(conn, "games_season_start"), "board": max(cfg_int(conn, "games_leaderboard_size"), 1),
            }

    def who(member) -> str:
        return identity.label(member["nation_id"], member["nation_name"])

    # ------------------------------------------------------------------------------------------------ the spin itself
    async def do_spin(interaction: discord.Interaction):
        st = settings()
        if not st["on"]:
            return await reply(interaction, "🎰 The games are closed right now.")
        with svc.db.read() as conn:
            member = L.member_by_discord(conn, interaction.user.id)
        if not member:
            return await reply(interaction, "Link your nation first with `/nation link`: prizes are paid to your nation.")
        if member["frozen"]:
            return await reply(interaction, "Your bank account is frozen, so you can't play right now. Ask the ECON team.")
        if st["role"].isdigit() and int(st["role"]) not in [getattr(r, "id", None) for r in getattr(interaction.user, "roles", [])]:
            return await reply(interaction, f"You need the <@&{st['role']}> role to play.")
        await thinking(interaction)
        snap = None
        try:
            snap = await svc.prices.get()
        except Exception:  # noqa: BLE001 - prices are only needed for the prize budget; the spin itself does not depend on them
            snap = None

        def work():
            with svc.db.tx() as conn:
                return G.play_daily(conn, game=GAME, nation_id=member["nation_id"], discord_id=uid(interaction), per_day=st["per_day"],
                                    day_start_hour=st["hour"], cap_cents=st["cap"], snap=snap)
        try:
            res = await asyncio.to_thread(work)
        except G.GameError as exc:
            return await reply(interaction, str(exc))
        o = res.outcome
        shown = None
        if st["anim"]:
            try:
                shown = await reply(interaction, card=A.Card(f"{DEF.emoji} Spinning…", "🎡 The wheel is turning…", A.BLUE))
                for frame in ("🎡 …still spinning…", "🎡 …slowing down…"):
                    await asyncio.sleep(0.9)
                    await interaction.edit_original_response(embed=A.to_embed(A.Card(f"{DEF.emoji} Spinning…", frame, A.BLUE)))
                await asyncio.sleep(0.9)
            except (discord.HTTPException, AttributeError):
                shown = None
        with svc.db.read() as conn:
            stats = G.player_stats(conn, GAME, member["nation_id"], st["season"])
        card = result_card(res, member, stats, st)
        try:
            if shown is not None:
                await interaction.edit_original_response(embed=A.to_embed(card))
            else:
                await reply(interaction, card=card)
        except (discord.HTTPException, AttributeError):
            await reply(interaction, card=card)
        await announce(interaction, res, member, st)

    def result_card(res: G.PlayResult, member, stats: dict, st: dict) -> A.Card:
        o = res.outcome
        if o.jackpot and o.pays:
            c = A.Card("💰 JACKPOT!", f"🎉 You hit the jackpot: **{o.label}**!", GOLD)
        elif o.pays:
            c = A.Card(f"{DEF.emoji} You won!", f"🏆 **{o.label}**", A.GREEN)
        else:
            c = A.Card(f"{DEF.emoji} No prize this time", f"**{o.label}**", A.GREY)
        if o.pays:
            c.add("Your prize", f"{fmt.short_amounts(o.amounts)}\nThe alliance bank will pay it to **{who(member)}** once staff approve the payout. "
                                f"Prize #{res.prize_id}: follow it with `/games history`.")
        c.add("Your score", f"{stats['score']:,} points" + (f" · rank #{stats['rank']}" if stats["rank"] else ""), True)
        c.add("Spins left today", str(res.spins_left), True)
        if res.spins_left == 0:
            c.add("Next spin", f"in {G.until(utcnow(), G.next_reset(utcnow(), st['hour']))}", True)
        if st["odds"]:
            c.footer = f"Chance of this result: {res.chance_pct:.1f}%"
        return c

    async def announce(interaction, res: G.PlayResult, member, st: dict):
        o = res.outcome
        mode = st["public"]
        if mode == "none" or (mode == "wins" and not o.pays) or (mode == "jackpots" and not (o.jackpot and o.pays)):
            return None
        if o.jackpot and o.pays:
            c = A.Card("💰 JACKPOT!", f"🎉 <@{interaction.user.id}> hit the jackpot!\n\n🏆 **{o.label}**", GOLD)
        elif o.pays:
            c = A.Card(f"{DEF.emoji} Spin result", f"<@{interaction.user.id}> won\n\n🏆 **{o.label}**", A.GREEN)
        else:
            c = A.Card(f"{DEF.emoji} Spin result", f"<@{interaction.user.id}> spun: **{o.label}**", A.GREY)
        target = getattr(interaction, "channel", None)
        client = getattr(interaction, "client", None)
        if st["announce"].isdigit() and client is not None:
            try:
                target = client.get_channel(int(st["announce"])) or await client.fetch_channel(int(st["announce"]))
            except (discord.HTTPException, AttributeError):
                target = getattr(interaction, "channel", None)
        try:
            await target.send(embed=A.to_embed(c))
        except (discord.HTTPException, AttributeError):
            log.info("could not post the public spin result (missing channel permission?)")
            return None
        return target

    # ---- keeping the panel at the bottom of its channel
    PANEL_DELAY = 3.0                       # wait for a burst of messages to settle before moving it
    watch: dict = {}                        # per alliance: what channel the panel is in (cached, so ordinary messages cost nothing)
    movers: dict = {}                       # per alliance: the background mover

    async def put_panel(client, target, actor: str):
        """Post a fresh panel in `target`, then remove the previous one and remember the new one. None if it could not be posted."""
        from . import tenantctx
        with svc.db.read() as conn:
            saved, old_msg = cfg_get(conn, "spin_panel_channel_id"), cfg_get(conn, "spin_panel_message_id")
        old = None
        if old_msg.strip().isdigit() and saved.strip().isdigit() and client is not None:
            try:
                old_ch = client.get_channel(int(saved)) or await client.fetch_channel(int(saved))
                old = await old_ch.fetch_message(int(old_msg))
            except (discord.HTTPException, AttributeError):
                old = None
        try:
            msg = await target.send(embed=A.to_embed(panel_card()), view=SpinPanelView())      # post first: the channel is never left without a panel
        except (discord.HTTPException, AttributeError):
            return None
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

        def keep():
            with svc.db.tx() as conn:
                cfg_set(conn, "spin_panel_channel_id", str(target.id), actor)
                cfg_set(conn, "spin_panel_message_id", str(msg.id), actor)
        await asyncio.to_thread(keep)
        watch.pop(tenantctx.current_key(), None)                           # re-read the panel's channel on the next message
        return msg

    def watched() -> dict:
        """The panel's channel and settings for the current alliance, re-read at most once a minute."""
        import time
        from . import tenantctx
        key = tenantctx.current_key()
        w = watch.get(key)
        if w is None or time.monotonic() - w["at"] > 60:
            with svc.db.read() as conn:
                chan = cfg_get(conn, "spin_panel_channel_id").strip()
                w = {"at": time.monotonic(), "channel": int(chan) if chan.isdigit() else None,
                     "stay": cfg_int(conn, "spin_panel_stay_last") == 1, "gap": max(cfg_int(conn, "spin_panel_min_seconds"), 3)}
            watch[key] = w
        return w

    async def touch(message, client=None):
        """Call for EVERY message posted in a server this alliance uses (the bot's on_message does). If it landed in the panel's channel
        the panel is moved back to the bottom: after a short pause so a burst of messages causes one move, and never more often than
        `spin_panel_min_seconds`. The panel's own message never triggers a move. Returns the background task (tests wait on it)."""
        from . import tenantctx
        w = watched()
        channel = getattr(message, "channel", None)
        if not w["stay"] or w["channel"] is None or getattr(channel, "id", None) != w["channel"]:
            return None
        me = getattr(client, "user", None)
        if getattr(getattr(message, "author", None), "id", None) == getattr(me, "id", object()) and getattr(message, "components", None):
            return None                                                   # the panel itself (a bot message carrying buttons)
        key = tenantctx.current_key()
        mv = movers.setdefault(key, {"dirty": False, "task": None, "lock": asyncio.Lock()})
        mv["dirty"] = True

        async def worker():
            while mv["dirty"]:
                await asyncio.sleep(PANEL_DELAY)
                mv["dirty"] = False
                async with mv["lock"]:
                    try:
                        await put_panel(client, channel, "system:panel")
                    except Exception:  # noqa: BLE001 - a panel that fails to move must never matter to anyone else
                        log.exception("could not move the spin panel to the bottom")
                await asyncio.sleep(w["gap"])
        if mv["task"] is None or mv["task"].done():
            mv["task"] = asyncio.create_task(worker())
        return mv["task"]

    svc.game_panel_touch = touch

    # ------------------------------------------------------------------------------------------------ player views
    def prizes_card() -> A.Card:
        with svc.db.read() as conn:
            table = G.odds(G.load_outcomes(conn, GAME))
            show_odds = cfg_int(conn, "games_show_odds") == 1
        c = A.Card(f"{DEF.emoji} {DEF.name}: prizes", "What you can win.", A.BLUE)
        wins = [(o, pct) for o, pct in table if o.pays]
        if not wins:
            c.description = "No prizes are set up yet."
            return c
        if show_odds:
            c.description = "What the wheel can give and how often. These are the real odds the bot uses."
            lines = [f"{'💰' if o.jackpot else '•'} **{o.label}** — {pct:.1f}%" for o, pct in table]
        else:
            lines = [f"{'💰' if o.jackpot else '•'} **{o.label}**" for o, _ in wins]
        c.add("Prizes", "\n".join(lines)[:1000])
        return c

    async def stats_for(interaction, user):
        target = user or interaction.user
        st = settings()
        with svc.db.read() as conn:
            member = L.member_by_discord(conn, target.id)
            if not member:
                return await reply(interaction, "That person has not linked a nation, so there are no spin stats." if user else
                                   "Link your nation first with `/nation link`.")
            s = G.player_stats(conn, GAME, member["nation_id"], st["season"])
            life = G.player_stats(conn, GAME, member["nation_id"], "")
            pend = conn.execute("SELECT COUNT(*) FROM game_prizes p WHERE p.nation_id=? AND p.status='PENDING' AND p.batch_id IS NULL", (member["nation_id"],)).fetchone()[0]
            day = G.day_key(utcnow(), st["hour"])
            today = conn.execute("SELECT COUNT(*) FROM game_plays WHERE game=? AND nation_id=? AND day_key=?", (GAME, member["nation_id"], day)).fetchone()[0]
        c = A.Card(f"📊 {who(member)}: spin stats", "", A.BLUE)
        c.add("This season" if st["season"] else "All time", f"**{s['score']:,}** points · {s['plays']} spins" + (f" · rank #{s['rank']}" if s["rank"] else ""))
        if st["season"]:
            c.add("All time", f"{life['score']:,} points · {life['plays']} spins", True)
        c.add("Prizes won", f"{life['wins']} · worth {fmt.dollars(life['value_cents'])}", True)
        if pend:
            c.add("Waiting to be paid", f"{pend} prize(s)", True)
        left = max(st["per_day"] - today, 0)
        c.add("Today", f"{left} spin(s) left" if left else f"Used. Next spin in {G.until(utcnow(), G.next_reset(utcnow(), st['hour']))}", True)
        await reply(interaction, card=c)

    @games.command(name="spin", description="Spin the wheel for your daily prize")
    async def spin(interaction: discord.Interaction):
        await do_spin(interaction)

    @games.command(name="prizes", description="See the prizes and the odds of Spin of Fortune")
    async def prizes(interaction: discord.Interaction):
        await reply(interaction, card=prizes_card())

    @games.command(name="stats", description="Your spin score, rank and prizes (or another member's)")
    @app_commands.describe(user="Whose stats (default: yours)")
    async def stats(interaction: discord.Interaction, user: Optional[discord.User] = None):
        await stats_for(interaction, user)

    async def show_board(interaction, period: str):
        st = settings()
        since = st["season"] if period == "season" else ""
        with svc.db.read() as conn:
            rows = G.leaderboard(conn, GAME, since, st["board"])
        c = A.Card("🏆 Spin leaderboard", ("Since the season started" if since else "All time"), A.ORANGE)
        if not rows:
            c.description += "\n\nNo scores yet. Be the first to spin!"
        else:
            c.add("Top players", "\n".join(f"{MEDALS.get(i, f'**#{i}**')} {identity.label(r['nation_id'], r['nname'])} — **{r['score']:,}** pts · {r['plays']} spins"
                                           for i, r in enumerate(rows, 1))[:1000])
        await reply(interaction, card=c, ephemeral=False)

    @games.command(name="leaderboard", description="The top spinners")
    @app_commands.choices(period=PERIODS)
    async def leaderboard(interaction: discord.Interaction, period: Optional[app_commands.Choice[str]] = None):
        await show_board(interaction, period.value if period else "season")

    @games.command(name="history", description="Your recent spins and whether their prizes were paid")
    @app_commands.describe(user="Whose history (default: yours)")
    async def history(interaction: discord.Interaction, user: Optional[discord.User] = None):
        target = user or interaction.user
        with svc.db.read() as conn:
            member = L.member_by_discord(conn, target.id)
            rows = G.history(conn, GAME, member["nation_id"], 15) if member else []
        if not member:
            return await reply(interaction, "No linked nation, so no spin history.")
        c = A.Card(f"📜 {who(member)}: recent spins", "", A.BLUE)
        if not rows:
            c.description = "No spins yet."
        else:
            c.add("Latest first", "\n".join(f"`{r['played_at'][:10]}` {'💰' if r['jackpot'] else '•'} **{r['outcome_label']}**" +
                                            (f" — {G.STATE_TEXT.get(r['state'], r['state'])}" if r["state"] else "") for r in rows)[:1000])
        await reply(interaction, card=c)

    # ------------------------------------------------------------------------------------------------ the panel
    class SpinPanelView(TenantView):
        """The permanent panel: one-click spin, your stats, the leaderboard. Works after restarts."""

        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="SPIN", emoji="🎰", style=discord.ButtonStyle.success, custom_id="tunbank:spin:play")
        async def play(self, interaction: discord.Interaction, button):
            await do_spin(interaction)

        @discord.ui.button(label="My stats", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="tunbank:spin:stats")
        async def mine(self, interaction: discord.Interaction, button):
            await stats_for(interaction, None)

        @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="tunbank:spin:board")
        async def leaders(self, interaction: discord.Interaction, button):
            await show_board(interaction, "season")

        @discord.ui.button(label="Prizes", emoji="🎁", style=discord.ButtonStyle.secondary, custom_id="tunbank:spin:prizes")
        async def odds(self, interaction: discord.Interaction, button):
            await reply(interaction, card=prizes_card())

    svc.game_panel_view = SpinPanelView

    def panel_card() -> A.Card:
        with svc.db.read() as conn:
            title, text = cfg_get(conn, "spin_panel_title"), cfg_get(conn, "spin_panel_text")
            table = G.odds(G.load_outcomes(conn, GAME))
        c = A.Card(title or f"{DEF.emoji} {DEF.name}", text, A.BLUE, kind="GAME_PANEL")
        top = [o for o, _ in table if o.pays]
        if top:
            c.add("Prizes", " · ".join(o.label for o in top)[:1000])
        return c

    # ------------------------------------------------------------------------------------------------ staff: overview and settings
    @gameset.command(name="show", description="BANKER: the state of the games, the prize table and what it costs")
    async def show(interaction: discord.Interaction):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        st = settings()
        try:
            snap = await svc.prices.get()
        except Exception:  # noqa: BLE001
            snap = None

        def read():
            with svc.db.tx() as conn:
                G.refresh_payments(conn)
            with svc.db.read() as conn:
                outs = G.load_outcomes(conn, GAME, only_enabled=False)
                waiting = G.payable_prizes(conn, GAME)
                today = G.plays_today(conn, GAME, G.day_key(utcnow(), st["hour"]))
            return outs, waiting, today
        outs, waiting, today = await asyncio.to_thread(read)
        live = [o for o in outs if o.enabled and o.weight > 0]
        c = A.Card(f"{DEF.emoji} {DEF.name}: staff overview", "", A.GREEN if st["on"] else A.ORANGE)
        c.add("Status", ("✅ open" if st["on"] else "⏸️ closed") + f" · {st['per_day']} spin(s) per day · resets {st['hour']:02d}:00 UTC", True)
        c.add("Who may play", f"<@&{st['role']}>" if st["role"].isdigit() else "Every member with a linked nation", True)
        c.add("Today", f"{today['plays']} spins by {today['players']} players · prizes worth {fmt.dollars(today['value_cents'])}", True)
        if outs:
            lines = []
            total = sum(o.weight for o in live)
            for o in outs:
                chance = f"{100 * o.weight / total:.1f}%" if o.enabled and o.weight and total else "off"
                lines.append(f"`#{o.id}` {'💰' if o.jackpot else '•'} **{o.label}** · weight {o.weight} ({chance}) · {fmt.short_amounts(o.amounts) if o.amounts else 'no prize'} · {o.score} pts")
            c.add("Prize table", "\n".join(lines)[:1000])
            ev = G.expected_value(outs, snap)
            if ev["complete"]:
                c.add("Cost to the alliance", f"About **{fmt.dollars(ev['average_cents'])}** per spin on average · at most {fmt.dollars(ev['max_cents'])} for one spin. "
                      f"With {st['per_day']} spin/day each, 100 players cost about {fmt.dollars(ev['average_cents'] * 100 * st['per_day'])} a day.")
            else:
                c.add("Cost to the alliance", "Can't be worked out yet: some prize has no price (or live prices are down).")
        else:
            c.add("Prize table", "**Empty.** Use `/gameset defaults` to load the classic prizes, or `/gameset prize` to add your own.")
        c.add("Panel stays last", "✅ yes: moved below any new message in its channel" if st["stay"] else "⬜ no", True)
        c.add("Prize budget", f"{fmt.dollars(st['cap'])} per day" if st["cap"] else "No daily cap (set one with `/gameset configure`)", True)
        if waiting:
            c.add("Waiting to be paid", f"**{len(waiting)}** prize(s) worth {fmt.amounts_with_value(_sum(waiting), None)}. Pay them with `/gameset pay`.")
        await reply(interaction, card=c)

    def _sum(rows) -> dict:
        t: dict = {}
        for r in rows:
            t = M.add(t, json.loads(r["amounts_json"]))
        return t

    @gameset.command(name="configure", description="MINISTER: open/close the games and change the spin settings")
    @app_commands.describe(open_games="Open or close all games", spins_per_day="Spins per member per day", reset_hour="UTC hour the day resets (0-23)",
                           role="Only this role may play (use clear_role to allow everyone)", clear_role="Allow every linked member again",
                           results="Who sees results in the channel", results_channel="Channel for public results",
                           animation="Spinning animation", panel_stays_last="Keep the panel as the last message of its channel", prize_budget_usd="Most prize value per day in dollars (0 = no cap)")
    @app_commands.choices(results=PUBLIC)
    async def configure(interaction: discord.Interaction, open_games: Optional[bool] = None, spins_per_day: Optional[app_commands.Range[int, 0, 20]] = None,
                        reset_hour: Optional[app_commands.Range[int, 0, 23]] = None, role: Optional[discord.Role] = None, clear_role: bool = False,
                        results: Optional[app_commands.Choice[str]] = None, results_channel: Optional[discord.TextChannel] = None,
                        animation: Optional[bool] = None, panel_stays_last: Optional[bool] = None, prize_budget_usd: Optional[app_commands.Range[int, 0, 10_000_000_000]] = None):
        changes: dict = {}
        flag = lambda b: "1" if b else "0"   # noqa: E731
        if open_games is not None:
            changes["games_enabled"] = flag(open_games)
        if spins_per_day is not None:
            changes["spin_per_day"] = str(spins_per_day)
        if reset_hour is not None:
            changes["games_day_start_hour_utc"] = str(reset_hour)
        if role is not None:
            changes["games_play_role_id"] = str(role.id)
        if clear_role:
            changes["games_play_role_id"] = ""
        if results is not None:
            changes["games_public_results"] = results.value
        if results_channel is not None:
            changes["games_announce_channel_id"] = str(results_channel.id)
        if animation is not None:
            changes["spin_animation"] = flag(animation)
        if panel_stays_last is not None:
            changes["spin_panel_stay_last"] = flag(panel_stays_last)
        if prize_budget_usd is not None:
            changes["spin_daily_prize_cap"] = str(prize_budget_usd)
        if not changes:
            return await reply(interaction, "Nothing to change. Pick at least one option, or see everything with `/gameset show`. "
                                            "Every games setting is also in `/bankset config`.")
        if not await need(svc, interaction, "MINISTER"):
            return
        if changes.get("games_enabled") == "1":
            with svc.db.read() as conn:
                if not G.load_outcomes(conn, GAME):
                    return await reply(interaction, "The prize table is empty, so the games can't open yet. Run `/gameset defaults` or add prizes with `/gameset prize`.")

        def save():
            with svc.db.tx() as conn:
                for k, v in changes.items():
                    old = cfg_get(conn, k)
                    if old != v:
                        cfg_set(conn, k, v, uid(interaction))
                        L.audit(conn, uid(interaction), "CONFIG_CHANGED", k, {"value": v, "previous": old})
        await asyncio.to_thread(save)
        await reply(interaction, f"Saved {len(changes)} setting(s). See the result with `/gameset show`.")

    # ------------------------------------------------------------------------------------------------ staff: the prize table
    @gameset.command(name="prize", description="MINISTER: add a prize, or change one (give its number)")
    @app_commands.describe(prize_id="The prize number from /gameset show (leave empty to add a new prize)", prize_name="What it is called, e.g. 300 steel",
                           weight="How often it comes up: bigger = more often (compare with the others)",
                           amounts="What it pays, e.g. steel=300 or money=5m coal=100. Write none for a no-prize result",
                           score="Leaderboard points", jackpot="Mark as a jackpot (gold announcement)", enabled="Switch it on or off without deleting it")
    async def prize(interaction: discord.Interaction, prize_id: Optional[int] = None, prize_name: Optional[str] = None, weight: Optional[int] = None,
                    amounts: Optional[str] = None, score: Optional[int] = None, jackpot: Optional[bool] = None, enabled: Optional[bool] = None):
        if not await need(svc, interaction, "MINISTER"):
            return
        parsed = None
        if amounts is not None:
            try:
                parsed = {} if amounts.strip().lower() in ("none", "nothing", "no", "-", "") else M.parse_amounts(amounts)
            except M.AmountError as exc:
                return await reply(interaction, f"I couldn't read the amounts: {exc}")

        def save():
            with svc.db.tx() as conn:
                return G.save_outcome(conn, GAME, uid(interaction), outcome_id=prize_id, label=prize_name, weight=weight, amounts=parsed, score=score,
                                      jackpot=jackpot, enabled=enabled)
        try:
            oid = await asyncio.to_thread(save)
        except G.GameError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"Prize `#{oid}` saved. Check the odds and the cost with `/gameset show`.")

    @gameset.command(name="removeprize", description="MINISTER: delete a prize from the table (past results keep their record)")
    async def removeprize(interaction: discord.Interaction, prize_id: int):
        if not await need(svc, interaction, "MINISTER"):
            return

        def go():
            with svc.db.tx() as conn:
                return G.remove_outcome(conn, GAME, uid(interaction), prize_id)
        try:
            label = await asyncio.to_thread(go)
        except G.GameError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"Removed **{label}**. Results already given are not affected.")

    @gameset.command(name="defaults", description="MINISTER: load the classic Spin of Fortune prizes into an empty table")
    async def defaults(interaction: discord.Interaction):
        if not await need(svc, interaction, "MINISTER"):
            return

        def go():
            with svc.db.tx() as conn:
                return G.load_defaults(conn, GAME, uid(interaction))
        try:
            n = await asyncio.to_thread(go)
        except G.GameError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"Loaded {n} classic prizes. **Check the amounts and odds** with `/gameset show` before opening the games: "
                                 "`/gameset configure open_games:True`.")

    @gameset.command(name="panel", description="MINISTER: post (or re-post) the Spin of Fortune panel in a channel")
    @app_commands.describe(channel="Where to post it (default: where the last panel was, else this channel)")
    async def panel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)
        with svc.db.read() as conn:
            saved = cfg_get(conn, "spin_panel_channel_id")
        target = channel
        client = getattr(interaction, "client", None)
        if target is None and saved.strip().isdigit() and client is not None:
            target = client.get_channel(int(saved))
        target = target or getattr(interaction, "channel", None)
        msg = await put_panel(client, target, uid(interaction))
        if msg is None:
            return await reply(interaction, "I couldn't post there. Check that I can send messages and embeds in that channel.")
        await reply(interaction, f"The spin panel is posted in {getattr(target, 'mention', 'this channel')}.")

    @gameset.command(name="newseason", description="MINISTER: start a fresh leaderboard (nothing is deleted; all-time stays available)")
    async def newseason(interaction: discord.Interaction):
        if not await need(svc, interaction, "MINISTER"):
            return
        await thinking(interaction)                          # the confirmation screen is sent as a follow-up, which needs the interaction acknowledged first
        card = A.Card("🏁 Start a new season?", "The leaderboard starts again from zero. **Nothing is deleted**: every spin and prize stays on record "
                      "and `/games leaderboard period:All time` still shows everything.", A.ORANGE)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. The leaderboard was not changed.")

        def go():
            with svc.db.tx() as conn:
                old = cfg_get(conn, "games_season_start")
                cfg_set(conn, "games_season_start", L.now_iso(), uid(interaction))
                L.audit(conn, uid(interaction), "GAME_SEASON_STARTED", GAME, {"previous_start": old})
        await asyncio.to_thread(go)
        await reply(interaction, "A new season has started. The leaderboard is empty.")

    # ------------------------------------------------------------------------------------------------ staff: paying prizes
    def parse_ids(text: str) -> list:
        try:
            return sorted({int(x) for x in text.replace(";", ",").replace(" ", ",").split(",") if x.strip()})
        except ValueError:
            raise G.GameError("Prize numbers must be whole numbers like `12,13,20`.") from None

    @gameset.command(name="payouts", description="BANKER: prizes waiting to be paid, and recent payment results")
    async def payouts(interaction: discord.Interaction):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)

        def read():
            with svc.db.tx() as conn:
                G.refresh_payments(conn)
            with svc.db.read() as conn:
                rows = G.payable_prizes(conn, GAME, limit=40)
                counts = {r["state"]: r["n"] for r in conn.execute(
                    f"SELECT {G.STATE_SQL} AS state, COUNT(*) n FROM game_prizes p WHERE p.game=? GROUP BY state", (GAME,))}
                held = conn.execute("SELECT COUNT(*) FROM game_prizes p JOIN members m ON m.nation_id=p.nation_id WHERE p.status='PENDING' AND m.frozen=1 AND p.batch_id IS NULL").fetchone()[0]
            return rows, counts, held
        rows, counts, held = await asyncio.to_thread(read)
        c = A.Card(f"{DEF.emoji} Spin prizes", "Prizes are only ever paid by `/gameset pay`: alliance-owned funds, your limits, a second approver when "
                   "required, a confirmation, and never sent twice.", A.BLUE)
        c.add("Totals", " · ".join(f"{G.STATE_TEXT.get(k, k)}: **{v}**" for k, v in sorted(counts.items())) or "No prizes yet.")
        if rows:
            c.add(f"Waiting ({len(rows)}{'+' if len(rows) == 40 else ''})", "\n".join(
                f"`#{r['id']}` {identity.label(r['nation_id'])} — {fmt.short_amounts(json.loads(r['amounts_json']))}" + (" ⚠️ retry" if r["state"] == "FAILED" else "")
                for r in rows[:12])[:1000] + (f"\n…and {len(rows) - 12} more" if len(rows) > 12 else ""))
        if held:
            c.add("Held back", f"{held} prize(s) belong to frozen accounts and are skipped until ECON unfreezes them.")
        await reply(interaction, card=c)

    @gameset.command(name="pay", description="BANKER: pay waiting prizes from alliance funds (you see everything first)")
    @app_commands.describe(prize_ids="Only these prize numbers, e.g. 12,13 (default: everything waiting, up to 100)")
    async def pay(interaction: discord.Interaction, prize_ids: str = ""):
        if not await need(svc, interaction, "BANKER"):
            return
        await thinking(interaction)
        try:
            wanted = parse_ids(prize_ids) if prize_ids.strip() else None
        except G.GameError as exc:
            return await reply(interaction, str(exc))

        def load():
            with svc.db.tx() as conn:
                G.refresh_payments(conn)
            with svc.db.read() as conn:
                rows = G.payable_prizes(conn, GAME, ids=wanted, limit=B.MAX_ROWS)
                frozen = {r["nation_id"] for r in conn.execute("SELECT nation_id FROM members WHERE frozen=1")}
            return rows, frozen
        rows, frozen = await asyncio.to_thread(load)
        skipped = [r for r in rows if r["nation_id"] in frozen]
        rows = [r for r in rows if r["nation_id"] not in frozen]
        if not rows:
            return await reply(interaction, "Nothing is waiting to be paid." + (" (Prizes of frozen accounts are held back.)" if skipped else ""))
        parsed = [(r, json.loads(r["amounts_json"])) for r in rows]
        totals: dict = {}
        for _, a in parsed:
            totals = M.add(totals, a)
        try:
            snap, val, alliance_free = await svc.wd.prepare(funding_source="ALLIANCE", amounts=totals)
        except (PnWRejected, PnWUncertain) as exc:
            return await reply(interaction, f"I can't read the live PnW bank, so nothing can be sent: {exc}")
        actor = uid(interaction)
        reveal = has_flag(svc, interaction, "bank_view_alliance_holdings")
        errors: list = []
        with svc.db.read() as conn:
            held = L.holds(conn, None, "ALLIANCE")
            free = M.sub(alliance_free, held)
            for r, need_units in totals.items():
                if free.get(r, 0) < need_units:
                    errors.append(f"Alliance-owned {M.LABELS[r]} is short: {M.fmt_units(r, max(free.get(r, 0), 0))} available, {M.fmt_units(r, need_units)} needed."
                                  if reveal else f"Alliance-owned {M.LABELS[r]} is not sufficient for these prizes.")
            for r, a in parsed:
                try:
                    LIM.check(conn, is_self=False, nation_id=None, actor_id=actor, actor_role_ids=role_ids(interaction), amounts=a, valuation=value_amounts(a, snap))
                except LIM.LimitExceeded as exc:
                    errors.append(f"Prize #{r['id']}: {exc}  (pay the others with `prize_ids`, and let someone with a higher limit pay this one)")
            try:
                LIM.check(conn, is_self=False, nation_id=None, actor_id=actor, actor_role_ids=role_ids(interaction), amounts=totals, valuation=val, skip_per_tx=True)
            except LIM.LimitExceeded as exc:
                errors.append(f"Whole payout: {exc}")
            threshold = cfg_int(conn, "approval_threshold_value") * 100
        if errors:
            c = A.Card(f"{icons.status('bad')} Prize payout blocked", "Nothing was sent.", A.RED)
            c.add(f"Problems ({len(errors)})", "\n".join(errors[:10])[:1000])
            return await reply(interaction, card=c)
        ids = [r["id"] for r, _ in parsed]
        sha = hashlib.sha256(("game-prizes:" + ",".join(map(str, ids))).encode()).hexdigest()
        approval_id = approver = None
        if threshold and (val.total_cents is None or val.total_cents > threshold):
            payload = {"game_prizes": ids, "totals": totals}

            def ask():
                with svc.db.tx() as conn:
                    return L.find_or_request_approval(conn, "ECON_WITHDRAW", actor, payload, f"spin prizes ({len(ids)})")
            approval_id, approver = await asyncio.to_thread(ask)
            if not approver:
                c = A.Card(f"{icons.status('wait')} Prize payout needs a second approver", f"Worth {fmt.dollars(val.total_cents)} for {len(ids)} prize(s).", A.ORANGE, kind="APPROVAL")
                c.add("Request", f"#{approval_id} by <@{actor}>")
                c.add("How to approve", f"A different Minister runs `/bank approve approval_id:{approval_id}`.")
                await svc.alerts.econ(c)
                return await reply(interaction, f"Approval request #{approval_id} was posted. After a different Minister approves it, run `/gameset pay` again.")
        card = A.Card(f"{icons.status('withdraw')} Confirm prize payout", "Nothing is sent until you press Confirm. Paid from **ALLIANCE-OWNED** funds.", A.ORANGE)
        card.add("Prizes", f"{len(ids)} prize(s) for {len({r['nation_id'] for r, _ in parsed})} nation(s)", True)
        card.add("Total", fmt.amounts_with_value(totals, val))
        if reveal:
            card.add("Alliance-owned left afterwards", fmt.amount_lines(M.sub(free, totals)))
        card.add("First prizes", "\n".join(f"`#{r['id']}` **{identity.label(r['nation_id'])}** — {fmt.short_amounts(a)}" for r, a in parsed[:8])
                 + (f"\n…and {len(parsed) - 8} more" if len(parsed) > 8 else ""))
        if skipped:
            card.add("Held back", f"{len(skipped)} prize(s) of frozen accounts are not included.")
        if approver:
            card.add("Second approver", f"<@{approver}>", True)
        if not await confirm(svc, interaction, card):
            return await reply(interaction, "Cancelled. Nothing was sent.")

        def create():
            with svc.db.tx() as conn:
                for r, _ in parsed:                      # claim them: another staff member paying at the same moment gets none of these
                    now = conn.execute(f"SELECT {G.STATE_SQL} AS state FROM game_prizes p WHERE p.id=?", (r["id"],)).fetchone()
                    if not now or now["state"] not in ("PENDING", "FAILED"):
                        return None
                cur = conn.execute(
                    "INSERT INTO bulk_batches(created_at,actor,source_filename,source_sha256,reason,row_count,totals_json,value_cents,price_snapshot_id,status,approval_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'RUNNING',?)",
                    (L.now_iso(), actor, "game-prizes", sha, f"{DEF.name} prizes", len(parsed), json.dumps(totals, sort_keys=True), val.total_cents,
                     snap.id if snap else None, approval_id))
                bid = cur.lastrowid
                for n, (r, a) in enumerate(parsed, 1):
                    conn.execute("INSERT INTO bulk_items(batch_id,row_no,nation_id,amounts_json,note) VALUES(?,?,?,?,?)",
                                 (bid, n, r["nation_id"], json.dumps(a, sort_keys=True), f"{DEF.name} prize #{r['id']}"))
                    conn.execute("UPDATE game_prizes SET batch_id=?, row_no=?, updated_at=? WHERE id=?", (bid, n, L.now_iso(), r["id"]))
                if approval_id:
                    L.finish_approval(conn, approval_id, "EXECUTED", {"batch": bid})
                L.audit(conn, actor, "BULK_BATCH_CREATED", f"bulk:{bid}", {"rows": len(parsed), "totals": totals, "reason": f"{DEF.name} prizes", "prize_ids": ids})
                return bid
        batch_id = await asyncio.to_thread(create)
        if batch_id is None:
            return await reply(interaction, "Someone else just started paying some of these prizes. Nothing was repeated: run `/gameset payouts` to see where things stand.")
        result = await B.run_batch(svc, batch_id, actor, role_ids(interaction), reveal)
        counts = (result.get("summary") or {}).get("counts", {})
        c = A.Card(f"{icons.status('good') if result.get('status') == 'COMPLETED' else icons.status('wait')} Prize payout #{batch_id}: {result.get('status', 'stopped')}",
                   result.get("halted_why") or result.get("error") or "", A.GREEN if result.get("status") == "COMPLETED" else A.ORANGE)
        c.add("Results", " · ".join(f"{k.title()}: {v}" for k, v in counts.items() if v) or "-")
        c.add("More detail", f"`/bulk status batch_id:{batch_id}`. If it stopped early, `/bulk resume` continues without repeating anything.")
        await reply(interaction, card=c)

    @gameset.command(name="cancel", description="MINISTER: cancel a prize that has not been paid (the reason is kept)")
    @app_commands.describe(prize_id="The prize number", reason="Why (kept in the record)")
    async def cancel(interaction: discord.Interaction, prize_id: int, reason: str):
        if not await need(svc, interaction, "MINISTER"):
            return

        def go():
            with svc.db.tx() as conn:
                G.refresh_payments(conn)
                return G.cancel_prize(conn, prize_id, uid(interaction), reason)
        try:
            row = await asyncio.to_thread(go)
        except G.GameError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"Prize #{prize_id} ({fmt.short_amounts(json.loads(row['amounts_json']))} for {identity.label(row['nation_id'])}) is cancelled. It stays in the record.")
