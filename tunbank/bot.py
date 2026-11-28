"""The Discord bot: wires everything together and runs the background jobs."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import cmds_admin, cmds_audit, cmds_backup, cmds_bulk, cmds_chart, cmds_convert, cmds_deposit, cmds_disburse, cmds_grant, cmds_loan, cmds_panel, cmds_report, cmds_shared_offshore, cmds_trade, cmds_offshore, cmds_econ, cmds_help, cmds_market, cmds_member
from . import ledger as L
from .alerts import AlertService
from .credentials import Crypto
from .memberdeposit import MemberDepositService
from .offshore import OffshoreService
from .config import cfg_int
from .db import Database, prune_backups
from .pnw import PnWClient
from .scanner import Scanner
from .ui import Services, guild_allowed, post_outcomes
from .util import now_iso, seconds_since
from .valuation import PriceService
from .withdrawals import WithdrawalService

log = logging.getLogger("tunbank.bot")


def build_services(bot, settings, db: Database) -> Services:
    """Everything one alliance needs, built from its settings and its own database. The bot holds one of these per alliance."""
    pnw = PnWClient(settings)
    prices = PriceService(db, pnw)
    svc = Services(settings=settings, db=db, pnw=pnw, prices=prices,
                   scanner=Scanner(db, pnw, prices, settings),
                   wd=WithdrawalService(db, pnw, prices, settings),
                   alerts=AlertService(db))
    svc.alerts.bot = bot
    svc.offshore = OffshoreService(db, pnw, prices, settings)
    svc.crypto = Crypto(settings.credential_key)
    svc.deposits = MemberDepositService(db, pnw, prices, settings, svc.crypto, svc.scanner)
    from .trademon import TradeMonitor
    svc.trade_monitor = TradeMonitor(db, pnw, prices, svc.alerts)
    return svc


class TunBankBot(commands.Bot):
    def __init__(self, settings, db: Database, platform=None):
        """`platform` (a tenancy.Platform) switches on several alliances in one bot. Without it the bot is the single-alliance
        bot it always was: same groups, same commands, nothing else registered."""
        intents = discord.Intents.default()
        if settings.members_intent:          # only when the owner switched it on in BOTH the portal and .env
            intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.db = db
        self.platform = platform
        self.svc = build_services(self, settings, db)               # the home alliance's services (the only ones in single mode)
        from . import identity as _identity
        _identity.init(db)                                            # names are available from the first second
        self.tenants = None
        self.reg = self.svc                                           # what the command modules hold as `svc`
        if platform is not None:
            from . import tenancy
            home = tenancy.Tenant(tenancy.HOME, settings.alliance_id, settings.guild_id, "Home alliance", settings, db, settings.owner_ids)
            home.svc = self.svc
            self.tenants = tenancy.Tenants(platform, home, lambda st, d: build_services(self, st, d), settings.data_dir)
            self.reg = tenancy.ServicesProxy(lambda: self.tenants)
            self.reg.platform = platform
        self._ready_once = False

    def alliances(self) -> list:
        """The alliances background work runs for: just the home alliance in single mode."""
        if self.tenants is None:
            return [None]
        return self.tenants.ready()

    async def for_each_alliance(self, fn, what: str):
        """Run `await fn(svc, db, settings)` once per ready alliance with that alliance in context.
        A failure in one alliance is logged and never stops the others."""
        for t in self.alliances():
            try:
                if t is None:
                    await fn(self.svc, self.db, self.settings)
                else:
                    with t.activate():
                        await fn(t.svc, t.db, t.settings)
            except Exception:  # noqa: BLE001
                log.exception("%s failed for alliance %s", what, "home" if t is None else t.alliance_id)

    async def setup_hook(self):
        from . import icons
        with self.db.read() as conn:
            icons.load_from_db(conn)
        if self.tenants is not None:
            from . import tenantctx
            from . import tenancy
            tenancy.import_home_game_data(self.platform, self.db)        # the existing rules and catalogue move to the platform, once
            self.tenants.load_all()
            tenantctx.set_resolver(self.tenants.resolve_interaction)      # buttons and modals pick their alliance from their server
        svc = self.reg
        bank = app_commands.Group(name="bank", description="TUN Bank")
        bankset = app_commands.Group(name="bankset", description="TUN Bank settings, limits, roles and backups (Admin)")
        nation = app_commands.Group(name="nation", description="Your Politics & War nation")
        tax = app_commands.Group(name="tax", description="Alliance tax records (ECON)")
        audit = app_commands.Group(name="audit", description="Audit tools (ECON)")
        ledger = app_commands.Group(name="ledger", description="Ledger integrity (ECON)")
        chart = app_commands.Group(name="chart", description="Charts from your real bank data")
        bulk = app_commands.Group(name="bulk", description="Pay many nations at once from alliance funds (ECON)")
        grant = app_commands.Group(name="grant", description="Alliance-approved grants (ECON)")
        deposit = app_commands.Group(name="deposit", description="Deposit reset and restoration (Admin)")
        loan = app_commands.Group(name="loan", description="Loans: money members owe the alliance")
        trade = app_commands.Group(name="trade", description="Trade monitoring: alerts and rules (ECON)")
        offshore_grp = app_commands.Group(name="offshore", description="Shared offshore: who owns what in it")
        transfer = app_commands.Group(name="transfer", description="Resource transfers calculated from each nation's data (ECON)")
        grantset = app_commands.Group(name="grantset", description="Grant and transfer settings (ECON)")
        economy = app_commands.Group(name="economy", description="Alliance revenue and expenditure reports (ECON)")
        cmds_member.register(bank, nation, svc)
        cmds_econ.register(bank, svc)                 # must come first: defines svc.do_reconcile
        cmds_admin.register(bank, bankset, ledger, svc)
        cmds_backup.register(bankset, svc)
        cmds_chart.register(chart, svc)
        cmds_bulk.register(bulk, svc)
        cmds_grant.register(grant, svc)
        cmds_disburse.register(grant, transfer, grantset, svc)
        cmds_offshore.register(bank, svc)
        cmds_help.register(self.tree, svc)
        cmds_market.register(self.tree, svc)
        cmds_audit.register_tax(tax, svc)
        cmds_audit.register_audit(audit, ledger, svc)
        cmds_deposit.register(deposit, svc)
        cmds_convert.register(bankset, svc)
        cmds_loan.register(loan, svc)
        cmds_panel.register(bankset, svc)
        cmds_trade.register(trade, svc)
        cmds_shared_offshore.register(offshore_grp, svc)
        cmds_report.register(economy, svc)
        self.add_view(svc.panel_view())                                # the banking panel keeps working after restarts
        self.add_view(cmds_convert.ConversionPanelView(svc))          # the panel button keeps working after restarts
        self.tree.on_error = self.on_tree_error

        for g in (bank, bankset, nation, tax, audit, ledger, chart, bulk, grant, deposit, loan, trade, offshore_grp, transfer, grantset, economy):
            self.tree.add_command(g)

        if self.tenants is None:
            async def guild_lock(interaction: discord.Interaction) -> bool:
                if guild_allowed(self.settings, interaction):
                    return True
                log.warning("refused a command from a server that is not this bot's home server (guild %s)", interaction.guild_id)
                try:
                    await interaction.response.send_message("This bot is only set up for its own alliance's Discord server.", ephemeral=True)
                except discord.HTTPException:
                    pass
                return False
            self.tree.interaction_check = guild_lock
            if self.settings.guild_id:
                guild = discord.Object(id=self.settings.guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
            return

        from . import cmds_platform
        platform_grp = app_commands.Group(name="platform", description="Platform owner: approve and manage alliances")
        setup_grp = app_commands.Group(name="setup", description="Set up your alliance's bank (alliance admins)")
        cmds_platform.register(platform_grp, setup_grp, self)
        self.tree.add_command(setup_grp)
        self.tree.add_command(platform_grp, guild=discord.Object(id=self.settings.guild_id))   # the home server only
        self.tree.interaction_check = self.alliance_gate
        await self.sync_alliance_servers()

    async def sync_alliance_servers(self, only_guild_id=None):
        """Register the commands in every alliance's server (the platform group only in the home server)."""
        for t in self.tenants.all():
            if only_guild_id and t.guild_id != only_guild_id:
                continue
            try:
                g = discord.Object(id=t.guild_id)
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            except Exception:  # noqa: BLE001 - e.g. the bot has not been invited to that server yet
                log.exception("could not sync commands to the server of alliance %s", t.alliance_id)

    async def alliance_gate(self, interaction: discord.Interaction) -> bool:
        """Every slash command and autocomplete passes here first: map the server to its alliance, or refuse."""
        from . import tenantctx
        from .tenancy import Refusal
        try:
            cmd = getattr(interaction, "command", None)
            tenant = self.tenants.route(interaction.guild_id, getattr(cmd, "qualified_name", "") or "")
        except Refusal as exc:
            kind = getattr(discord, "InteractionType", None)
            if kind is not None and getattr(interaction, "type", None) == getattr(kind, "autocomplete", object()):
                return False                                   # autocomplete must not be answered with a message
            log.warning("refused a command (guild %s): %s", interaction.guild_id, exc)
            try:
                await interaction.response.send_message(str(exc), ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        tenantctx.set_current(tenant.key)
        return True

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("command error", exc_info=error)
        msg = "Something went wrong and nothing was changed unless a confirmation said otherwise. ECON can check `/ledger dashboard`."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _check_icon_pack(self, db=None):
        """If a configured PnW icon was deleted from the bot's emojis, fall back to the default so no broken emoji text is shown."""
        from . import icons as _icons
        from .config import cfg_get, cfg_set
        db = db or self.db
        try:
            emojis = await self.fetch_application_emojis()
        except Exception as exc:  # noqa: BLE001 - offline / older library: keep whatever is configured
            log.info("icon pack check skipped: %s", exc)
            return
        with db.read() as conn:
            configured = {r: cfg_get(conn, f"icon_{r}") for r in _icons.DEFAULT_RESOURCE_ICONS}
        stale = _icons.stale_pack_icons(configured, emojis)
        if stale:
            with db.tx() as conn:
                for r in stale:
                    cfg_set(conn, f"icon_{r}", "", "system:icon-check")
                _icons.load_from_db(conn)
            log.warning("PnW icon(s) no longer exist on the bot, using the default icons for: %s", ", ".join(stale))

    async def _startup_one(self, svc, db, settings):
        """Start-up recovery and checks for ONE alliance (run inside that alliance's context)."""
        try:
            notes = await svc.wd.recover()
            if svc.offshore.enabled:
                notes += await svc.offshore.recover()
            for n in notes:
                log.warning("startup recovery: %s", n)
            await svc.alerts.flush_events()
            await svc.alerts.flush_notifications()
            await self._check_icon_pack(db)
            with db.read() as conn:
                stuck = [r["id"] for r in conn.execute("SELECT id FROM bulk_batches WHERE status='RUNNING'")]
            if stuck:
                from .alerts import Card, ORANGE
                await svc.alerts.econ(Card("⏸️ Bulk transfer interrupted", "The bot restarted while bulk transfer(s) "
                    + ", ".join(f"#{i}" for i in stuck) + " were running. Check `/bulk status`, then `/bulk resume`. "
                    "Rows already sent are never repeated.", ORANGE))
        except Exception:  # noqa: BLE001
            log.exception("startup recovery failed")
        try:
            from . import configaudit as CA
            with db.tx() as conn:
                changed = CA.check_env_changes(conn, settings)
            if changed:
                log.warning("%s startup setting(s) changed since the last start; recorded in the audit log", changed)
            await svc.alerts.flush_config_audit()
        except Exception:  # noqa: BLE001
            log.exception("startup configuration check failed")
        try:
            from .records import backfill_tax_turns
            with db.tx() as conn:
                n = backfill_tax_turns(conn)
            if n:
                log.info("Grouped existing tax records into %s turns (already announced)", n)
        except Exception:  # noqa: BLE001
            log.exception("tax-turn backfill failed")

    async def on_ready(self):
        if self._ready_once:
            return
        self._ready_once = True
        log.info("Logged in as %s", self.user)
        await self.for_each_alliance(self._startup_one, "start-up")
        self.scan_loop.start()
        self.recon_loop.start()
        self.backup_loop.start()
        self.audit_loop.start()
        self.trade_loop.start()
        self.identity_loop.start()
        self.report_loop.start()

    async def close(self):
        for t in (self.scan_loop, self.recon_loop, self.backup_loop, self.audit_loop, self.trade_loop, self.identity_loop, self.report_loop):
            t.cancel()
        await self.svc.pnw.close()
        self.db.close()
        if self.tenants is not None:
            for t in self.tenants.all():
                if t.is_home:
                    continue
                try:
                    if t.svc is not None:
                        await t.svc.pnw.close()
                    t.db.close()
                except Exception:  # noqa: BLE001
                    log.exception("could not close alliance %s cleanly", t.alliance_id)
            self.platform.db.close()
        await super().close()

    # ------------------------------------------------------------- loops (each runs once per alliance, one after the other)
    async def _scan_one(self, svc, db, settings):
        with db.read() as conn:
            interval = cfg_int(conn, "scan_interval_seconds")
            last = L.get_state(conn, "last_scan_attempt")
        if last and (seconds_since(last) or 9999) < interval:
            return
        with db.tx() as conn:
            L.set_state(conn, "last_scan_attempt", now_iso())
        res = await svc.scanner.scan()
        if res.ok:
            await post_outcomes(svc, res.outcomes)
        else:
            log.warning("scan failed: %s", res.error)
        await svc.alerts.flush_events()
        await svc.alerts.flush_notifications()

    @tasks.loop(seconds=30)
    async def scan_loop(self):
        """Runs every 30s but only scans when the configured interval has passed."""
        await self.for_each_alliance(self._scan_one, "scan loop")

    async def _identity_one(self, svc, db, settings):
        from . import identity as _identity
        await _identity.refresh_some(db, svc.pnw, 40)

    @tasks.loop(seconds=180)
    async def identity_loop(self):
        """Fill in / refresh nation names (also for OLD records), a small batch at a time."""
        await self.for_each_alliance(self._identity_one, "nation name refresh")

    async def _trade_one(self, svc, db, settings):
        await svc.trade_monitor.poll_if_due()

    @tasks.loop(seconds=30)
    async def trade_loop(self):
        """Watches completed trades; only does anything when the configured interval has passed."""
        await self.for_each_alliance(self._trade_one, "trade monitor")

    async def _recon_one(self, svc, db, settings):
        try:
            with db.read() as conn:
                interval = cfg_int(conn, "reconcile_interval_seconds")
                last = L.get_state(conn, "last_recon_attempt")
            if not (last and (seconds_since(last) or 9999) < interval):
                with db.tx() as conn:
                    L.set_state(conn, "last_recon_attempt", now_iso())
                await self.reg.do_reconcile("system:scheduled")
        except Exception:  # noqa: BLE001
            log.exception("reconcile loop error")
        try:
            await svc.announce_overdue_loans()
        except Exception:  # noqa: BLE001
            log.exception("overdue loan check failed")

    @tasks.loop(seconds=60)
    async def recon_loop(self):
        await self.for_each_alliance(self._recon_one, "reconcile loop")

    async def _audit_one(self, svc, db, settings):
        await svc.alerts.flush_config_audit()

    @tasks.loop(seconds=20)
    async def audit_loop(self):
        """Nothing stays unannounced: any recorded change not yet in the audit channel is posted here."""
        await self.for_each_alliance(self._audit_one, "config audit flush")

    async def _report_one(self, svc, db, settings):
        from . import econ_report
        await econ_report.send_due_reports(svc)

    @tasks.loop(minutes=10)
    async def report_loop(self):
        """Posts the weekly / monthly economy report once per finished period, for every alliance that switched it on."""
        await self.for_each_alliance(self._report_one, "economy report")

    async def _backup_one(self, svc, db, settings):
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        target = settings.backup_dir / f"daily-{today}.db"
        if target.exists():
            return
        await asyncio.to_thread(db.backup_to, target)
        with db.read() as conn:
            keep = cfg_int(conn, "backup_keep_daily")
        prune_backups(settings.backup_dir, keep_daily=max(7, keep))
        log.info("Daily backup written: %s", target)

    @tasks.loop(minutes=30)
    async def backup_loop(self):
        """One consistent backup per UTC day for every alliance (and the platform registry), kept inside the persistent volume."""
        await self.for_each_alliance(self._backup_one, "backup")
        if self.platform is not None:
            try:
                today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                bdir = self.settings.data_dir / "platform-backups"
                target = bdir / f"daily-{today}.db"
                if not target.exists():
                    await asyncio.to_thread(self.platform.db.backup_to, target)
                    prune_backups(bdir, keep_daily=30)
            except Exception:  # noqa: BLE001
                log.exception("platform backup failed")

    @scan_loop.before_loop
    @recon_loop.before_loop
    @backup_loop.before_loop
    @trade_loop.before_loop
    @identity_loop.before_loop
    @audit_loop.before_loop
    @report_loop.before_loop
    async def _wait(self):
        await self.wait_until_ready()
