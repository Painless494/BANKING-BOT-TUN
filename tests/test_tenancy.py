"""Multi-alliance support: database isolation, authorization boundaries, the command gate, encrypted keys and migrations."""
import asyncio
import ast
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fake_discord  # noqa: E402

fake_discord.install()

import discord  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from test_core import ALLIANCE, FakePnW  # noqa: E402
from tunbank import bot as botmod  # noqa: E402
from tunbank import cmds_platform  # noqa: E402
from tunbank import db as DBMOD  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import tenancy, tenantctx  # noqa: E402
from tunbank.config import Settings, cfg_get, cfg_set  # noqa: E402
from tunbank.db import Database  # noqa: E402
from tunbank.tenancy import NoCurrentAlliance, Platform, TenancyError  # noqa: E402

FI = fake_discord.FakeInteraction
HOME_GUILD, GUILD_A, GUILD_B = 1000000000000000001, 1000000000000000002, 1000000000000000003
OWNER, ADMIN_A, ADMIN_B, RANDOM = 900001, 2000000000000000001, 2000000000000000002, 3000000000000000001
ALLI_A, ALLI_B = 5001, 5002
GOOD_KEYS = ("abcdefghij1234567890", "klmnopqrst1234567890", "uvwxyzabcd1234567890")


def run(coro):
    return asyncio.run(coro)


class Env(unittest.TestCase):
    """A real TunBankBot in multi-alliance mode (fake discord, fake PnW), with two approved alliances."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.master = Fernet.generate_key().decode()
        self.settings = Settings("t", "homeapikey1234", "homebotkey1234", "homeapikey1234", ALLIANCE, self.dir, {OWNER},
                                 guild_id=HOME_GUILD, credential_key=self.master)
        self.db = Database(self.settings.db_path)
        self.db.migrate()
        self.platform = Platform.open(self.settings)
        self._pnw = botmod.PnWClient
        botmod.PnWClient = lambda settings: FakePnW()
        self.bot = botmod.TunBankBot(self.settings, self.db, self.platform)
        run(self.bot.setup_hook())
        self._mk = cmds_platform.make_pnw_client
        self.members = {1: "Alice"}
        fake = lambda settings: types.SimpleNamespace(fetch_alliance_members=self._members, close=self._noop)   # noqa: E731
        cmds_platform.make_pnw_client = fake

    async def _members(self):
        return self.members

    async def _noop(self):
        return None

    def tearDown(self):
        botmod.PnWClient = self._pnw
        cmds_platform.make_pnw_client = self._mk
        tenantctx.set_resolver(None)
        for t in self.bot.tenants.all():
            t.db.close()
        self.platform.db.close()
        self.tmp.cleanup()

    # ---- helpers
    def group(self, name):
        tree = self.bot.tree
        return tree.cmds.get(name) or tree.guild_cmds[HOME_GUILD][name]

    def cmd(self, user, guild, path, *a, **kw):
        """Run a slash command the way Discord would: the gate first, then the handler, in its own task."""
        g, sub = path.split(" ")

        async def go():
            i = FI(discord.User(user, "u"))
            i.guild_id = guild
            i.command = types.SimpleNamespace(qualified_name=path)
            if not await self.bot.alliance_gate(i):
                i.gated = True
                return i
            i.gated = False
            await self.group(g).commands[sub].callback(i, *a, **kw)
            return i
        return run(go())

    def add(self, aid, guild, admin, name=None):
        i = self.cmd(OWNER, HOME_GUILD, "platform add", aid, str(guild), name or f"Alliance {aid}", str(admin))
        self.assertIn("Approved", i.text(), i.text())

    def give_keys(self, admin, guild):
        async def go():
            i = FI(discord.User(admin, "u"))
            i.guild_id = guild
            i.command = types.SimpleNamespace(qualified_name="setup keys")
            assert await self.bot.alliance_gate(i)
            await self.group("setup").commands["keys"].callback(i)
            m = i.modal
            for c, v in zip(m.children, GOOD_KEYS):
                c.value = v
            i2 = FI(discord.User(admin, "u"))
            i2.guild_id = guild
            await m.on_submit(i2)
            return i2
        return run(go())

    def two_alliances(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        self.add(ALLI_B, GUILD_B, ADMIN_B)
        self.assertIn("Keys accepted", self.give_keys(ADMIN_A, GUILD_A).text())
        self.assertIn("Keys accepted", self.give_keys(ADMIN_B, GUILD_B).text())
        return self.bot.tenants.for_alliance(ALLI_A), self.bot.tenants.for_alliance(ALLI_B)


class TestContext(unittest.TestCase):
    def test_default_is_home_and_activation_is_scoped(self):
        self.assertEqual(tenantctx.current_key(), "home")
        with tenantctx.activate("a1"):
            self.assertEqual(tenantctx.current_key(), "a1")
            with tenantctx.activate("a2"):
                self.assertEqual(tenantctx.current_key(), "a2")
            self.assertEqual(tenantctx.current_key(), "a1")
        self.assertEqual(tenantctx.current_key(), "home")

    def test_concurrent_tasks_never_mix_alliances(self):
        seen = {}

        async def worker(key, n):
            with tenantctx.activate(key):
                for _ in range(n):
                    await asyncio.sleep(0)
                    seen.setdefault(key, set()).add(tenantctx.current_key())

        async def main():
            await asyncio.gather(*(worker(f"a{i}", 5 + i) for i in range(6)))
        run(main())
        self.assertEqual(seen, {f"a{i}": {f"a{i}"} for i in range(6)})

    def test_per_alliance_name_cache_and_icons_do_not_leak(self):
        from tunbank import icons, identity
        with tenantctx.activate("a1"):
            identity.remember(77, "SecretNation")
            icons.load({"money": "💎"})
            self.assertEqual(icons.resource("money"), "💎")
        with tenantctx.activate("a2"):
            self.assertNotIn(77, identity._st().cache)
            self.assertNotEqual(icons.resource("money"), "💎")
        self.assertNotIn(77, identity._st().cache)               # home as well


class TestRegistry(Env):
    def test_owner_adds_an_alliance_and_it_gets_its_own_database_file(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        self.assertTrue((self.dir / "tenants" / str(ALLI_A) / "tunbank.db").exists())
        self.assertNotEqual(self.bot.tenants.for_alliance(ALLI_A).db.path, self.db.path)
        with self.platform.db.read() as c:
            self.assertEqual(c.execute("SELECT status, keys_enc FROM tenants").fetchone()[:], ("ACTIVE", None))
            self.assertTrue(L.verify_chain(c, "audit_log")["ok"])

    def test_registry_rules(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        for args, text in (((ALLI_A, str(GUILD_B), "x", str(ADMIN_B)), "already registered"),
                           ((ALLI_B, str(GUILD_A), "x", str(ADMIN_B)), "already registered"),
                           ((ALLIANCE, str(GUILD_B), "x", str(ADMIN_B)), "home alliance"),
                           ((ALLI_B, str(HOME_GUILD), "x", str(ADMIN_B)), "home server"),
                           ((ALLI_B, str(GUILD_B), "x", "notanid"), "not a Discord user ID"),
                           ((ALLI_B, "abc", "x", str(ADMIN_B)), "must be a number")):
            self.assertIn(text, self.cmd(OWNER, HOME_GUILD, "platform add", *args).text())

    def test_registry_rows_cannot_be_deleted_and_alliance_id_is_frozen(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        with self.assertRaises(sqlite3.Error):
            with self.platform.db.tx() as c:
                c.execute("DELETE FROM tenants")
        with self.assertRaises(sqlite3.Error):
            with self.platform.db.tx() as c:
                c.execute("UPDATE tenants SET alliance_id=1")

    def test_keys_are_encrypted_at_rest_and_only_open_with_the_right_alliance_key(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        self.give_keys(ADMIN_A, GUILD_A)
        raw = (self.dir / "platform.db")
        self.platform.db.conn.execute("PRAGMA wal_checkpoint(FULL)")
        blob = raw.read_bytes() + (self.dir / "platform.db-wal").read_bytes() if (self.dir / "platform.db-wal").exists() else raw.read_bytes()
        for k in GOOD_KEYS:
            self.assertNotIn(k.encode(), blob)
        with self.platform.db.read() as c:
            enc = c.execute("SELECT keys_enc FROM tenants").fetchone()[0]
        self.assertTrue(enc.startswith("gAAAA"))
        self.assertEqual(self.platform.load_keys(ALLI_A)["api_key"], GOOD_KEYS[0])
        from tunbank.credentials import Crypto
        with self.assertRaises(Exception):
            Crypto(tenancy.keys_key_for(self.master, ALLI_B)).decrypt(enc.encode())

    def test_member_key_encryption_is_per_alliance_and_home_keeps_the_raw_key(self):
        ta, tb = self.two_alliances()
        self.assertNotEqual(ta.settings.credential_key, tb.settings.credential_key)
        self.assertNotEqual(ta.settings.credential_key, self.master)
        self.assertEqual(self.bot.svc.settings.credential_key, self.master)
        blob = ta.svc.crypto.encrypt("memberkey123456")
        with self.assertRaises(Exception):
            tb.svc.crypto.decrypt(blob)
        self.assertEqual(ta.svc.crypto.decrypt(blob), "memberkey123456")

    def test_keys_cannot_be_stored_without_the_master_encryption_key(self):
        p = Platform(self.platform.db, self.settings, "")
        with self.assertRaises(Exception):
            p.save_keys(1, {"api_key": "x"}, "t")


class TestIsolation(Env):
    def test_two_alliances_have_separate_files_settings_and_services(self):
        ta, tb = self.two_alliances()
        self.assertEqual(len({str(self.db.path), str(ta.db.path), str(tb.db.path)}), 3)
        self.assertEqual((ta.settings.alliance_id, tb.settings.alliance_id), (ALLI_A, ALLI_B))
        self.assertEqual(ta.settings.owner_ids, {ADMIN_A})
        self.assertNotIn(OWNER, ta.settings.owner_ids)                      # the platform owner is not automatically an admin there
        self.assertIsNone(ta.settings.offshore)
        self.assertEqual(ta.settings.pnw_api_key, GOOD_KEYS[0])
        self.assertEqual(self.settings.pnw_api_key, "homeapikey1234")        # home untouched
        self.assertIsNot(ta.svc.db, tb.svc.db)
        self.assertEqual(ta.svc.alerts.only_guild_id, GUILD_A)

    def test_a_setting_or_banker_in_one_alliance_is_invisible_to_the_others(self):
        ta, tb = self.two_alliances()
        i = self.cmd(ADMIN_A, GUILD_A, "bankset addbanker", discord.User(4242, "banker"))
        self.assertFalse(i.gated)
        with ta.db.read() as c:
            self.assertEqual(c.execute("SELECT discord_id FROM bankers").fetchall()[0][0], "4242")
        for other in (tb.db, self.db):
            with other.read() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM bankers").fetchone()[0], 0)
        with ta.db.tx() as c:
            cfg_set(c, "dm_members", "0", "t")
        with tb.db.read() as c:
            self.assertEqual(cfg_get(c, "dm_members"), "1")

    def test_services_proxy_follows_context_and_never_falls_back_to_home(self):
        ta, tb = self.two_alliances()
        proxy = self.bot.reg
        with self.assertRaises(NoCurrentAlliance):
            proxy.db
        with self.assertRaises(NoCurrentAlliance):
            getattr(proxy, "settings", None)                                   # not hidden by a default
        with ta.activate():
            self.assertIs(proxy.db, ta.db)
        with tb.activate():
            self.assertIs(proxy.db, tb.db)
        with tenantctx.activate("home"):
            self.assertIs(proxy.db, self.db)
        with tenantctx.activate("a999"):
            with self.assertRaises(NoCurrentAlliance):
                proxy.db

    def test_shared_proxy_attributes_are_the_same_for_every_alliance(self):
        ta, tb = self.two_alliances()
        proxy = self.bot.reg
        self.assertTrue(callable(proxy.panel_view) and callable(proxy.do_reconcile))
        self.assertIs(proxy.platform, self.platform)
        with ta.activate():
            a = proxy.actions
        with tb.activate():
            self.assertIs(a, proxy.actions)

    def test_identity_names_and_icons_are_per_alliance_in_the_running_bot(self):
        from tunbank import identity
        ta, tb = self.two_alliances()
        with ta.activate():
            identity.remember(1234, "OnlyInA")
        with tb.activate():
            self.assertNotIn(1234, identity._st().cache)

    def test_background_work_runs_once_per_ready_alliance_and_one_failure_does_not_stop_others(self):
        ta, tb = self.two_alliances()
        seen = []

        async def work(svc, db, settings):
            seen.append((tenantctx.current_key(), settings.alliance_id))
            if settings.alliance_id == ALLI_A:
                raise RuntimeError("boom")
        run(self.bot.for_each_alliance(work, "test"))
        self.assertEqual(sorted(seen), sorted([("home", ALLIANCE), (f"a{ALLI_A}", ALLI_A), (f"a{ALLI_B}", ALLI_B)]))
        self.platform.set_status(ALLI_B, "SUSPENDED", "t")
        self.bot.tenants.load_row(self.platform.by_alliance(ALLI_B))
        seen.clear()
        run(self.bot.for_each_alliance(work, "test"))
        self.assertNotIn((f"a{ALLI_B}", ALLI_B), seen)

    def test_each_alliances_loops_use_its_own_database(self):
        ta, tb = self.two_alliances()
        run(self.bot.for_each_alliance(self.bot._scan_one, "scan"))
        for db in (self.db, ta.db, tb.db):
            with db.read() as c:
                self.assertIsNotNone(L.get_state(c, "last_scan_attempt"))
        with ta.db.tx() as c:
            L.set_state(c, "emergency_lock", "ON")
        with tb.db.read() as c:
            self.assertNotEqual(L.get_state(c, "emergency_lock"), "ON")

    def test_alerts_only_post_inside_the_alliances_own_server(self):
        ta, tb = self.two_alliances()
        sent = []

        class Chan:
            def __init__(self, guild):
                self.guild = types.SimpleNamespace(id=guild)

            async def send(self, **kw):
                sent.append(self.guild.id)
        chans = {1: Chan(GUILD_A), 2: Chan(GUILD_B)}
        self.bot.get_channel = lambda i: chans.get(i)
        from tunbank.alerts import Card
        with ta.db.tx() as c:
            cfg_set(c, "econ_log_channel_id", "2", "t")                     # a channel of ANOTHER alliance's server
        run(ta.svc.alerts.econ(Card("hello")))
        self.assertEqual(sent, [])
        with ta.db.tx() as c:
            cfg_set(c, "econ_log_channel_id", "1", "t")
        run(ta.svc.alerts.econ(Card("hello")))
        self.assertEqual(sent, [GUILD_A])


class TestGamesAcrossAlliances(Env):
    def test_prizes_plays_and_settings_of_one_alliance_never_reach_another(self):
        from tunbank import games as G
        ta, tb = self.two_alliances()
        for t in (ta, tb):
            with t.db.tx() as c:
                c.execute("INSERT INTO members(nation_id,nation_name,discord_id,created_at) VALUES(7,'Seven','4242','x')")
        with ta.db.tx() as c:
            cfg_set(c, "games_enabled", "1", "t")
            cfg_set(c, "spin_animation", "0", "t")
            G.save_outcome(c, "spin", "t", outcome_id=None, label="coal", weight=1, amounts={"coal": 100}, score=5, jackpot=False, enabled=True)
        self.assertIn("coal", self.cmd(4242, GUILD_A, "games spin").text())
        self.assertIn("closed", self.cmd(4242, GUILD_B, "games spin").text())           # B never opened its games
        self.assertIn("closed", self.cmd(4242, HOME_GUILD, "games spin").text())
        with ta.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_prizes").fetchone()[0], 1)
        for other in (tb.db, self.db):
            with other.read() as c:
                self.assertEqual((c.execute("SELECT COUNT(*) FROM game_plays").fetchone()[0], c.execute("SELECT COUNT(*) FROM game_prizes").fetchone()[0]), (0, 0))
        self.assertIn("permission", self.cmd(ADMIN_B, GUILD_A, "gameset pay").text().lower())     # another alliance's admin can't pay A's prizes
        self.assertIs(self.bot.reg.game_panel_view, self.bot.reg.game_panel_view)                   # the panel view is shared; it picks its alliance per press


class TestAuthorization(Env):
    def test_platform_commands_are_owner_only_and_home_server_only(self):
        self.assertIn("Only the platform owner", self.cmd(RANDOM, HOME_GUILD, "platform list").text())
        self.assertIn("Only the platform owner", self.cmd(ADMIN_A, HOME_GUILD, "platform add", 1, "5", "x", str(ADMIN_A)).text())
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        for path, args in (("platform list", ()), ("platform suspend", (ALLI_A,)), ("platform resume", (ALLI_A,)),
                           ("platform admins", (ALLI_A, str(RANDOM))), ("platform add", (ALLI_B, str(GUILD_B), "x", str(ADMIN_B)))):
            out = self.cmd(OWNER, GUILD_A, path, *args)                          # the owner, but in an alliance's server
            self.assertIn("home server", out.text(), path)
        with self.platform.db.read() as c:
            self.assertEqual(c.execute("SELECT status, admin_ids FROM tenants").fetchone()[:], ("ACTIVE", str(ADMIN_A)))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tenants").fetchone()[0], 1)

    def test_an_admin_of_one_alliance_is_not_an_admin_in_another(self):
        self.two_alliances()
        self.assertIn("permission", self.cmd(ADMIN_A, GUILD_B, "bankset addbanker", discord.User(4242, "b")).text().lower())
        self.assertIn("permission", self.cmd(OWNER, GUILD_A, "bankset addbanker", discord.User(4242, "b")).text().lower())   # nor the platform owner
        self.assertIn("permission", self.cmd(ADMIN_B, GUILD_A, "bankset addbanker", discord.User(4242, "b")).text().lower())
        for t in self.bot.tenants.all():
            with t.db.read() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM bankers").fetchone()[0], 0)

    def test_setup_keys_is_for_that_alliances_admins_only(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        self.assertIn("Only this alliance's admins", self.cmd(RANDOM, GUILD_A, "setup keys").text())
        self.assertIn("Only this alliance's admins", self.cmd(OWNER, GUILD_A, "setup keys").text())
        self.assertIn("home alliance's keys", self.cmd(OWNER, HOME_GUILD, "setup keys").text())

    def test_setup_modal_rechecks_the_admin_on_submit(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        async def go():
            i = FI(discord.User(ADMIN_A, "u")); i.guild_id = GUILD_A
            i.command = types.SimpleNamespace(qualified_name="setup keys")
            await self.group("setup").commands["keys"].callback(i)
            m = i.modal
            for c, v in zip(m.children, GOOD_KEYS):
                c.value = v
            i2 = FI(discord.User(RANDOM, "u")); i2.guild_id = GUILD_A          # somebody else submits the form
            await m.on_submit(i2)
            return i2
        self.assertIn("not allowed", run(go()).text())
        self.assertIsNone(self.platform.by_alliance(ALLI_A)["keys_enc"])

    def test_bad_or_rejected_keys_are_not_stored(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        self.members = {}
        self.assertIn("returned no members", self.give_keys(ADMIN_A, GUILD_A).text())
        from tunbank.pnw import PnWRejected

        async def rejected():
            raise PnWRejected("bad key")
        self._members = rejected
        self.assertIn("did not accept", self.give_keys(ADMIN_A, GUILD_A).text())
        self.assertIsNone(self.platform.by_alliance(ALLI_A)["keys_enc"])
        self.assertFalse(self.bot.tenants.for_alliance(ALLI_A).has_keys)

    def test_rules_and_catalogue_are_owner_only_with_a_platform(self):
        self.two_alliances()
        for who, guild in ((ADMIN_A, GUILD_A), (OWNER, HOME_GUILD)):
            i = self.cmd(who, guild, "grantset rule", "x", "1")
            if who == OWNER:
                self.assertNotIn("Only the platform owner", i.text())
            else:
                self.assertIn("Only the platform owner", i.text())
        att = types.SimpleNamespace(size=10, filename="p.csv")
        self.assertIn("Only the platform owner", self.cmd(ADMIN_A, GUILD_A, "grantset projectimport", att).text())

    def test_a_rule_correction_lives_in_the_platform_database_for_everyone(self):
        from tunbank import pnw_rules as PR
        ta, tb = self.two_alliances()
        key = sorted(PR.DEFAULT_RULES)[0]
        self.assertIn("→", self.cmd(OWNER, HOME_GUILD, "grantset rule", key, "123").text())
        with self.platform.db.read() as c:
            self.assertIn(key, cfg_get(c, "pnw_rule_overrides"))
        for db in (self.db, ta.db, tb.db):
            with db.read() as c:
                self.assertEqual(cfg_get(c, "pnw_rule_overrides"), "{}")
        PR.load_overrides("{}")


class TestGate(Env):
    def test_unknown_server_dm_suspended_and_no_keys_are_refused(self):
        self.add(ALLI_A, GUILD_A, ADMIN_A)
        self.assertIn("not registered", self.cmd(OWNER, 123456789012345678, "bank balance").text())
        self.assertIn("direct messages", self.cmd(OWNER, None, "bank balance").text())
        self.assertIn("keys are not set up", self.cmd(ADMIN_A, GUILD_A, "bank balance").text())
        self.assertFalse(self.cmd(ADMIN_A, GUILD_A, "setup status").gated)                  # /setup is allowed without keys
        self.give_keys(ADMIN_A, GUILD_A)
        self.assertFalse(self.cmd(ADMIN_A, GUILD_A, "setup status").gated)
        self.cmd(OWNER, HOME_GUILD, "platform suspend", ALLI_A)
        for path in ("bank balance", "setup status", "setup keys"):
            self.assertIn("suspended", self.cmd(ADMIN_A, GUILD_A, path).text(), path)
        self.cmd(OWNER, HOME_GUILD, "platform resume", ALLI_A)
        self.assertFalse(self.cmd(ADMIN_A, GUILD_A, "setup status").gated)

    def test_suspension_does_not_touch_other_alliances_or_delete_anything(self):
        ta, tb = self.two_alliances()
        with ta.db.tx() as c:
            c.execute("INSERT INTO bankers(discord_id,added_by,added_at) VALUES('1','x','x')")
        self.cmd(OWNER, HOME_GUILD, "platform suspend", ALLI_A)
        self.assertFalse(self.cmd(ADMIN_B, GUILD_B, "setup status").gated)
        with ta.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bankers").fetchone()[0], 1)
        self.assertFalse(self.bot.tenants.for_alliance(ALLI_A).ready)

    def test_autocomplete_refusals_send_no_response(self):
        async def go():
            i = FI(discord.User(RANDOM, "u")); i.guild_id = 123456789012345678
            i.type = discord.InteractionType.autocomplete
            i.command = types.SimpleNamespace(qualified_name="bank balance")
            ok = await self.bot.alliance_gate(i)
            return ok, i.sent
        ok, sent = run(go())
        self.assertFalse(ok)
        self.assertEqual(sent, [])

    def test_the_home_alliance_works_exactly_as_before(self):
        i = self.cmd(OWNER, HOME_GUILD, "bankset listbankers")
        self.assertFalse(i.gated)
        self.assertEqual(self.bot.tenants.for_guild(HOME_GUILD).key, "home")
        self.assertIs(self.bot.tenants.home.db, self.db)
        self.assertEqual(self.db.path, self.dir / "tunbank.db")

    def test_buttons_and_modals_pick_their_alliance_from_their_server(self):
        from tunbank.tenantui import TenantModal, TenantView
        ta, tb = self.two_alliances()
        v = TenantView()
        for guild, key in ((GUILD_A, f"a{ALLI_A}"), (GUILD_B, f"a{ALLI_B}"), (HOME_GUILD, "home")):
            async def go():
                i = FI(discord.User(RANDOM, "u")); i.guild_id = guild
                ok = await v.interaction_check(i)
                return ok, tenantctx.current_key()
            self.assertEqual(run(go()), (True, key))

        async def refused():
            i = FI(discord.User(RANDOM, "u")); i.guild_id = 99
            return await TenantModal(title="t").interaction_check(i), i.text()
        ok, text = run(refused())
        self.assertFalse(ok)
        self.assertIn("not registered", text)

    def test_a_button_on_a_suspended_alliance_is_refused(self):
        from tunbank.tenantui import TenantView
        self.two_alliances()
        self.cmd(OWNER, HOME_GUILD, "platform suspend", ALLI_B)

        async def go():
            i = FI(discord.User(RANDOM, "u")); i.guild_id = GUILD_B
            return await TenantView().interaction_check(i), i.text()
        ok, text = run(go())
        self.assertFalse(ok)
        self.assertIn("suspended", text)

    def test_commands_sync_to_every_alliance_server_and_platform_only_to_home(self):
        self.two_alliances()
        tree = self.bot.tree
        self.assertTrue({GUILD_A, GUILD_B, HOME_GUILD} <= set(tree.synced))
        self.assertEqual(set(tree.guild_cmds), {HOME_GUILD})
        self.assertIn("platform", tree.guild_cmds[HOME_GUILD])
        self.assertNotIn("platform", tree.cmds)
        self.assertIn("setup", tree.cmds)

    def test_a_restart_reloads_every_alliance_and_its_keys(self):
        self.two_alliances()
        for t in self.bot.tenants.all():
            if not t.is_home:
                t.db.close()
        bot2 = botmod.TunBankBot(self.settings, self.db, self.platform)
        run(bot2.setup_hook())
        ta = bot2.tenants.for_alliance(ALLI_A)
        self.assertTrue(ta.ready)
        self.assertEqual(ta.settings.pnw_api_key, GOOD_KEYS[0])
        for t in bot2.tenants.all():
            if not t.is_home:
                t.db.close()

    def test_one_broken_alliance_does_not_stop_the_bot_from_loading_the_rest(self):
        self.two_alliances()
        for t in self.bot.tenants.all():
            if not t.is_home:
                t.db.close()
        (self.dir / "tenants" / str(ALLI_A) / "tunbank.db").write_bytes(b"this is not a database at all" * 100)
        bot2 = botmod.TunBankBot(self.settings, self.db, self.platform)
        run(bot2.setup_hook())
        self.assertIsNone(bot2.tenants.for_alliance(ALLI_A))
        self.assertTrue(bot2.tenants.for_alliance(ALLI_B).ready)
        bot2.tenants.for_alliance(ALLI_B).db.close()

    def test_multi_mode_needs_the_home_server_id(self):
        s = Settings("t", "k", "b", "k", ALLIANCE, self.dir, {OWNER})
        with self.assertRaises(TenancyError):
            botmod.TunBankBot(s, self.db, Platform(self.platform.db, s, ""))


class TestHomeDataImport(Env):
    def test_existing_rules_and_catalogue_move_to_the_platform_once(self):
        # the setup_hook already ran the import on an empty home; seed the home and import again after resetting the marker
        from tunbank import projects as PJ
        with self.db.tx() as c:
            cfg_set(c, "pnw_rule_overrides", '{"a": "1"}', "t")
            c.execute("INSERT INTO project_catalogue_versions(imported_at,imported_by,source_name,sha256,project_count,json_text) "
                      "VALUES('2026-01-01T00:00:00Z','9','f.json','abc',1,'{\"projects\": []}')")
        with self.platform.db.tx() as c:
            c.execute("DELETE FROM platform_settings")
        res = tenancy.import_home_game_data(self.platform, self.db)
        self.assertEqual(res, {"rules": True, "catalogue_versions": 1})
        with self.platform.db.read() as c:
            self.assertEqual(cfg_get(c, "pnw_rule_overrides"), '{"a": "1"}')
            self.assertEqual(c.execute("SELECT source_name FROM project_catalogue_versions").fetchone()[0], "f.json")
        self.assertEqual(tenancy.import_home_game_data(self.platform, self.db), {"rules": False, "catalogue_versions": 0})
        with self.db.read() as c:                                               # nothing removed from home
            self.assertEqual(c.execute("SELECT COUNT(*) FROM project_catalogue_versions").fetchone()[0], 1)


class TestMigration018(unittest.TestCase):
    """The platform-only migration, on fresh, 006-level and populated 013-level databases. Shared migrations are untouched."""

    def levels(self, upto, d):
        old = d / f"m{upto}"
        old.mkdir()
        for f in DBMOD.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"):
            if int(f.name[:3]) <= upto:
                (old / f.name).write_bytes(f.read_bytes())
        return old

    def test_fresh_platform_database_gets_018_and_a_home_database_never_does(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            pdb = Database(d / "platform.db", extra_migrations_dir=tenancy.PLATFORM_MIGRATIONS_DIR)
            applied = pdb.migrate()
            self.assertEqual(applied[-2:], ["018_tenants.sql", "019_games.sql"])
            self.assertEqual(len(applied), 19)
            self.assertEqual(pdb.migrate(), [])
            home = Database(d / "tunbank.db")
            applied_home = home.migrate()
            self.assertNotIn("018_tenants.sql", applied_home)
            self.assertIn("019_games.sql", applied_home)
            with home.read() as c:
                self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='tenants'").fetchone())
            pdb.close(); home.close()

    def test_018_on_a_006_level_database(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            real = DBMOD.MIGRATIONS_DIR
            DBMOD.MIGRATIONS_DIR = self.levels(6, d)
            try:
                db = Database(d / "p.db")
                db.migrate()
                with db.tx() as c:
                    L.ensure_member(c, 5)
                    L.audit(c, "9", "T", "x", {})
            finally:
                DBMOD.MIGRATIONS_DIR = real
            db.close()
            plat = Database(d / "p.db", extra_migrations_dir=tenancy.PLATFORM_MIGRATIONS_DIR)
            applied = plat.migrate(backup_dir=d / "bk")
            self.assertEqual(applied[0], "007_negative_balances_reset_restore_loans.sql")
            self.assertEqual(applied[-2:], ["018_tenants.sql", "019_games.sql"])
            self.assertEqual(len(applied), 13)
            with plat.read() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM members").fetchone()[0], 1)
                self.assertTrue(L.verify_chain(c, "audit_log")["ok"])
                self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(c.execute("SELECT COUNT(*) FROM tenants").fetchone()[0], 0)
            plat.close()

    def test_018_on_a_populated_013_level_database_through_the_whole_chain(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            real = DBMOD.MIGRATIONS_DIR
            DBMOD.MIGRATIONS_DIR = self.levels(13, d)
            try:
                db = Database(d / "p.db")
                db.migrate()
                with db.tx() as c:
                    for n in (1, 2):
                        L.ensure_member(c, n)
                    L.audit(c, "9", "T", "x", {"k": 1})
                    cfg_set(c, "dm_members", "0", "t")
            finally:
                DBMOD.MIGRATIONS_DIR = real
            db.close()
            db = Database(d / "p.db", extra_migrations_dir=tenancy.PLATFORM_MIGRATIONS_DIR)
            applied = db.migrate(backup_dir=d / "bk")
            self.assertEqual(applied[0], "014_balance_transfers.sql")
            self.assertEqual(applied[-2:], ["018_tenants.sql", "019_games.sql"])
            self.assertEqual(len(list((d / "bk").glob("pre-migration-*.db"))), 1)
            with db.read() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM members").fetchone()[0], 2)
                self.assertEqual(cfg_get(c, "dm_members"), "0")
                self.assertTrue(L.verify_chain(c, "audit_log")["ok"])
                self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.close()

    def test_old_migrations_are_untouched_and_a_clashing_version_is_refused(self):
        import hashlib
        sums = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in DBMOD.MIGRATIONS_DIR.glob("*.sql")}
        self.assertEqual(len(sums), 18)                                      # 001-017 and 019: 018 belongs to the platform-only directory
        self.assertFalse(any(n.startswith("018") for n in sums))
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "extra").mkdir()
            (d / "extra" / "019_clash.sql").write_text("SELECT 1;")
            with self.assertRaises(DBMOD.DatabaseError):
                Database(d / "x.db", extra_migrations_dir=d / "extra").migrate()


class TestStructure(unittest.TestCase):
    def test_every_view_and_modal_is_alliance_aware(self):
        """A button pressed later runs with no alliance in context: every View/Modal must set it before any callback."""
        allowed_plain = {("cmds_platform.py", "SetupKeysModal")}              # the alliance has no services yet: it checks the server itself
        bad = []
        for f in (Path(__file__).resolve().parent.parent / "tunbank").glob("*.py"):
            if f.name in ("tenantui.py",):
                continue
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.ClassDef):
                    for b in node.bases:
                        src = ast.unparse(b)
                        if src in ("discord.ui.View", "discord.ui.Modal") and (f.name, node.name) not in allowed_plain:
                            bad.append((f.name, node.name))
        self.assertEqual(bad, [])

    def test_help_lists_the_multi_alliance_commands(self):
        from tunbank import cmds_help as H
        self.assertTrue(set(H.MULTI_CATALOG).isdisjoint(H.CATALOG))
        self.assertEqual(H.lookup("setup keys"), ("config", "ADMIN"))


if __name__ == "__main__":
    unittest.main()
