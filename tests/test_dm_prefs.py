"""Bank DMs to members: staff switches per kind of DM, the master switch, and the member's own choice."""
import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_discord  # noqa: E402
import test_commands as TC  # noqa: E402

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from tunbank import alerts as A  # noqa: E402
from tunbank import dmprefs as D  # noqa: E402
from tunbank import notify  # noqa: E402
from tunbank.config import cfg_get, cfg_set  # noqa: E402

KINDS = ["WITHDRAWAL", "EXTERNAL_WITHDRAWAL", "CREDIT", "DEBIT", "ADJUSTMENT"]


class FakeUserBot:
    def __init__(self):
        self.dms = []                                           # (discord_id, title)

    def get_user(self, did):
        outer = self

        class U:
            async def send(self, embed=None, **kw):
                outer.dms.append((did, embed.title))
        return U()

    async def fetch_user(self, did):
        return self.get_user(did)


class DMs(unittest.TestCase):
    make_settings = TC.Cmd.make_settings
    run_async = TC.Cmd.run_async
    call = TC.Cmd.call
    tearDown = TC.Cmd.tearDown

    def setUp(self):
        TC.Cmd.setUp(self)
        from tunbank import cmds_dm
        cmds_dm.register(self.nation, self.svc)
        self.run_async(self.svc.scanner.scan())
        self.call(self.nation, "link", self.alice, 1)
        self.call(self.nation, "link", discord.User(112, "bob"), 2)
        self.bot = FakeUserBot()
        self.svc.alerts.bot = self.bot

    def cfg(self, **kw):
        with self.db.tx() as c:
            for k, v in kw.items():
                cfg_set(c, k, str(v), "t")

    def queue(self, nation=1, kinds=KINDS, tag=""):
        with self.db.tx() as c:
            for k in kinds:
                notify.enqueue(c, dedupe_key=f"{tag}{k}:{nation}", nation_id=nation, kind=k, title=k, deltas={"money": -100})

    def flush(self):
        self.bot.dms.clear()
        self.run_async(self.svc.alerts.flush_notifications())
        with self.db.read() as c:
            return {r["kind"]: (r["status"], r["error"]) for r in c.execute("SELECT kind,status,error FROM member_notifications ORDER BY id")}

    def sent(self, result):
        return sorted(k for k, (s, _) in result.items() if s == "SENT")

    # ---- staff switches
    def test_defaults_change_nothing_every_dm_is_sent(self):
        self.queue()
        self.assertEqual(self.sent(self.flush()), sorted(KINDS))

    def test_master_switch_stops_every_dm_with_a_reason(self):
        self.cfg(dm_members=0)
        self.queue()
        r = self.flush()
        self.assertEqual(self.sent(r), [])
        self.assertTrue(all(s == "SKIPPED" and "dm_members" in e for s, e in r.values()))

    def test_each_category_switch_only_affects_its_own_dms(self):
        for n, (key, silent) in enumerate((("dm_withdrawals", {"WITHDRAWAL", "EXTERNAL_WITHDRAWAL"}), ("dm_transfers", {"CREDIT", "DEBIT"}),
                                           ("dm_changes", {"ADJUSTMENT"}))):
            self.cfg(**{key: 0})
            self.queue(nation=1, tag=f"r{n}")
            with self.db.read() as c:
                ids = [r["id"] for r in c.execute("SELECT id FROM member_notifications WHERE dedupe_key LIKE ?", (f"r{n}%",))]
            self.flush()
            with self.db.read() as c:
                got = {r["kind"]: r["status"] for r in c.execute(f"SELECT kind,status FROM member_notifications WHERE id IN ({','.join('?' * len(ids))})", ids)}
            self.assertEqual({k for k, s in got.items() if s == "SKIPPED"}, silent, key)
            self.assertEqual({k for k, s in got.items() if s == "SENT"}, set(KINDS) - silent, key)
            self.cfg(**{key: 1})

    def test_a_skipped_dm_names_the_switch_that_stopped_it(self):
        self.cfg(dm_transfers=0)
        self.queue(kinds=["CREDIT"])
        self.assertIn("dm_transfers", self.flush()["CREDIT"][1])

    def test_unknown_kinds_follow_only_the_master_switch(self):
        self.cfg(dm_withdrawals=0, dm_transfers=0, dm_changes=0, dm_deposits=0)
        self.queue(kinds=["SOMETHING_NEW"])
        self.assertEqual(self.sent(self.flush()), ["SOMETHING_NEW"])           # never silently lost

    def test_direct_dms_follow_their_category(self):
        card = A.Card("hello")
        send = lambda cat: self.run_async(self.svc.alerts.dm("111", card, category=cat))   # noqa: E731
        self.cfg(dm_deposits=0)
        send("deposits")
        self.assertEqual(self.bot.dms, [])
        send("changes")
        self.assertEqual(len(self.bot.dms), 1)
        send(None)
        self.assertEqual(len(self.bot.dms), 2)                                  # no category: only the master switch
        self.cfg(dm_members=0)
        send("changes")
        send(None)
        self.assertEqual(len(self.bot.dms), 2)

    # ---- the member's own choice
    def test_member_choices_are_ignored_unless_staff_allow_them(self):
        with self.db.tx() as c:
            D.set_choice(c, 1, "withdrawals", False)
        self.queue(kinds=["WITHDRAWAL"])
        self.assertEqual(self.sent(self.flush()), ["WITHDRAWAL"])
        out = self.call(self.nation, "alerts", self.alice, withdrawals=False).text()
        self.assertIn("alliance decides", out)

    def test_a_member_can_mute_a_category_for_themselves_only(self):
        self.cfg(dm_member_choice=1)
        self.assertIn("Saved", self.call(self.nation, "alerts", self.alice, withdrawals=False, transfers=True).text())
        self.queue(nation=1, kinds=["WITHDRAWAL", "CREDIT", "ADJUSTMENT"], tag="a")
        self.queue(nation=2, kinds=["WITHDRAWAL"], tag="b")
        r = self.flush()
        with self.db.read() as c:
            rows = {(x["nation_id"], x["kind"]): x["status"] for x in c.execute("SELECT nation_id,kind,status FROM member_notifications")}
        self.assertEqual(rows[(1, "WITHDRAWAL")], "SKIPPED")
        self.assertEqual((rows[(1, "CREDIT")], rows[(1, "ADJUSTMENT")], rows[(2, "WITHDRAWAL")]), ("SENT", "SENT", "SENT"))
        with self.db.read() as c:
            self.assertIn("switched", [x["error"] for x in c.execute("SELECT error FROM member_notifications WHERE status='SKIPPED'")][0])

    def test_a_member_can_never_mute_the_notice_of_money_leaving_outside_the_bank(self):
        self.cfg(dm_member_choice=1)
        self.call(self.nation, "alerts", self.alice, withdrawals=False)
        self.queue(kinds=["WITHDRAWAL", "EXTERNAL_WITHDRAWAL"])
        self.assertEqual(self.sent(self.flush()), ["EXTERNAL_WITHDRAWAL"])
        self.cfg(dm_withdrawals=0)                                              # only staff can switch that notice off
        self.queue(kinds=["EXTERNAL_WITHDRAWAL"], tag="later")
        with self.db.read() as c:
            self.assertFalse(D.decide(c, category="withdrawals", nation_id=1, kind="EXTERNAL_WITHDRAWAL")[0])

    def test_member_alerts_screen_shows_what_applies_and_requires_a_linked_nation(self):
        self.assertIn("Link your nation", self.call(self.nation, "alerts", discord.User(999, "x")).text())
        self.cfg(dm_transfers=0)
        out = self.call(self.nation, "alerts", self.alice).text()
        self.assertIn("Withdrawals", out)
        self.assertIn("off for everyone", out)
        self.cfg(dm_member_choice=1)
        self.call(self.nation, "alerts", self.alice, deposits=False)
        out = self.call(self.nation, "alerts", self.alice).text()
        self.assertIn("⬜ **Deposits**", out)
        self.assertIn("✅ **Withdrawals**", out)

    def test_choices_survive_staff_switching_the_option_off_and_on(self):
        self.cfg(dm_member_choice=1)
        self.call(self.nation, "alerts", self.alice, withdrawals=False)
        self.cfg(dm_member_choice=0)
        with self.db.read() as c:
            self.assertTrue(D.decide(c, category="withdrawals", nation_id=1, kind="WITHDRAWAL")[0])
        self.cfg(dm_member_choice=1)
        with self.db.read() as c:
            self.assertFalse(D.decide(c, category="withdrawals", nation_id=1, kind="WITHDRAWAL")[0])

    # ---- staff command
    def test_staff_command_is_admin_only_audited_and_shows_the_state(self):
        self.assertIn("permission", self.call(self.nation, "dmalerts", self.alice, withdrawals=False).text().lower())
        self.assertIn("permission", self.call(self.nation, "dmalerts", self.alice).text().lower())
        out = self.call(self.nation, "dmalerts", self.admin, withdrawals=False, members_choose=True).text()
        self.assertIn("Withdrawals", out)
        with self.db.read() as c:
            self.assertEqual((cfg_get(c, "dm_withdrawals"), cfg_get(c, "dm_member_choice"), cfg_get(c, "dm_deposits")), ("0", "1", "1"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='CONFIG_CHANGED' AND target IN ('dm_withdrawals','dm_member_choice')").fetchone()[0], 2)
        self.assertNotIn("nothing below", self.call(self.nation, "dmalerts", self.admin).text())
        self.assertIn("nothing below", self.call(self.nation, "dmalerts", self.admin, all_dms=False).text())

    def test_a_real_withdrawal_dm_respects_the_switch_and_the_money_still_moves(self):
        self.run_async(self.svc.scanner.scan())
        self.pnw.recs.append(TC.rec(900, 1, {"money": 5000.0}))
        self.pnw.holdings = {"money": 500000}
        self.run_async(self.svc.scanner.scan())
        self.cfg(dm_withdrawals=0)
        res = self.run_async(self.svc.wd.request(tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1, lock_id=None, dest_nation_id=1,
                                                 amounts={"money": 100000}, actor="111", note="n", reason="r", idempotency_key="w1"))
        self.assertEqual(res.status, "COMPLETED")
        r = self.flush()
        self.assertEqual(r["WITHDRAWAL"][0], "SKIPPED")
        self.assertEqual(self.pnw.withdraw_calls, 1)                            # the DM setting never touches the movement of money


class Structure(unittest.TestCase):
    def test_every_dm_kind_has_a_decision(self):
        import re
        src = "".join(p.read_text() for p in (Path(__file__).resolve().parent.parent / "tunbank").glob("*.py"))
        used = set(re.findall(r'notify\.enqueue\([^)]*?kind="([A-Z_]+)"', src, re.S))
        self.assertGreaterEqual(len(used), 4)                                   # the scan really found the DM kinds
        self.assertTrue(used <= set(D.KIND_CATEGORY), f"a DM kind has no category: {used - set(D.KIND_CATEGORY)}")


if __name__ == "__main__":
    unittest.main()
