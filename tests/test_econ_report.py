"""The economy report: revenue = tax (+ donations if asked), expenditure = alliance money that left, members' own money never counted."""
import asyncio
import datetime as dt
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_discord  # noqa: E402
import test_commands as TC  # noqa: E402
from test_core import rec  # noqa: E402

from tunbank import econ_report as ER  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank.alerts import to_embed  # noqa: E402
from tunbank.config import cfg_get, cfg_set  # noqa: E402
from tunbank.valuation import Snapshot  # noqa: E402

UTC = dt.timezone.utc
TODAY = dt.datetime.now(UTC)
SNAP = Snapshot(1, "2026-11-27T00:00:00Z", {"coal": 100, "oil": 200})        # 1 coal = $1.00, 1 oil = $2.00 (cents per unit)


def d(y, m, day, h=0, mi=0):
    return dt.datetime(y, m, day, h, mi, tzinfo=UTC)


class Periods(unittest.TestCase):
    NOW = d(2026, 11, 27, 12)            # a Friday

    def test_weeks_run_monday_to_sunday_utc(self):
        p = ER.period_for("last_week", self.NOW)
        self.assertEqual((p.start, p.end), (d(2026, 11, 16), d(2026, 11, 23)))
        self.assertFalse(p.partial)
        t = ER.period_for("this_week", self.NOW)
        self.assertEqual((t.start, t.end, t.partial), (d(2026, 11, 23), d(2026, 11, 30), True))

    def test_months_and_year_end(self):
        self.assertEqual(ER.period_for("last_month", self.NOW).start, d(2026, 10, 1))
        p = ER.period_for("last_month", d(2027, 1, 5))
        self.assertEqual((p.start, p.end), (d(2026, 12, 1), d(2027, 1, 1)))
        self.assertEqual(ER.previous_of(p).start, d(2026, 11, 1))

    def test_rolling_days_include_today(self):
        p = ER.period_for("7d", self.NOW)
        self.assertEqual((p.start, p.end), (d(2026, 11, 21), d(2026, 11, 28)))

    def test_custom_dates_and_their_errors(self):
        p = ER.period_for("custom", self.NOW, "2026-10-01", "2026-10-31")
        self.assertEqual((p.start, p.end), (d(2026, 10, 1), d(2026, 11, 1)))
        self.assertEqual(ER.previous_of(p).start, d(2026, 9, 1))              # a whole calendar month compares with the month before
        for args, text in ((("", ""), "start date"), (("2026-13-01", ""), "year-month-day"), (("2026-10-05", "2026-10-01"), "before"),
                           (("2024-01-01", "2026-11-01"), "400 days")):
            with self.assertRaises(ER.ReportError) as cm:
                ER.period_for("custom", self.NOW, *args)
            self.assertIn(text, str(cm.exception))

    def test_previous_period_has_the_same_length(self):
        p = ER.period_for("custom", self.NOW, "2026-11-10", "2026-11-16")
        q = ER.previous_of(p)
        self.assertEqual(q.end, p.start)
        self.assertEqual(q.end - q.start, p.end - p.start)

    def test_automatic_reports_wait_an_hour_after_the_period_ends(self):
        self.assertEqual(ER.due_periods(d(2026, 11, 23, 0, 30)).get("weekly", (None,))[0].start if "weekly" in ER.due_periods(d(2026, 11, 23, 0, 30)) else None,
                         d(2026, 11, 9))                                         # still the week before: Sunday's last turn is not settled yet
        due = ER.due_periods(d(2026, 11, 23, 1, 5))
        self.assertEqual(due["weekly"][1], "2026-W47")
        self.assertEqual(due["weekly"][0].start, d(2026, 11, 16))
        self.assertEqual(ER.due_periods(d(2026, 12, 1, 1, 5))["monthly"][1], "2026-11")
        self.assertEqual(ER.due_periods(d(2026, 12, 1, 0, 10))["monthly"][1], "2026-10")


class Report(unittest.TestCase):
    make_settings = TC.Cmd.make_settings
    run_async = TC.Cmd.run_async
    call = TC.Cmd.call
    fund_alice = TC.Cmd.fund_alice
    tax_rec = TC.SecurityCmd.tax_rec
    tearDown = TC.Cmd.tearDown

    def setUp(self):
        TC.Cmd.setUp(self)
        self.bot = TC.FakeBotChannels()
        self.econ_log, self.out = TC.FakeChannel(), TC.FakeChannel()
        self.bot.chans = {111000: self.econ_log, 222000: self.out}
        self.svc.alerts.bot = self.bot
        with self.db.tx() as c:
            cfg_set(c, "econ_log_channel_id", "111000", "t")
        self.pnw.taxrecs = []
        self.prices_snapshot = SNAP
        self.svc.prices = types.SimpleNamespace(get=self._snap)

    async def _snap(self):
        return self.prices_snapshot

    def scan(self):
        return self.run_async(self.svc.scanner.scan())

    def day(self, n=0):
        return (TODAY - dt.timedelta(days=n)).strftime("%Y-%m-%d")

    def whole_period(self):
        return ER.Period(TODAY - dt.timedelta(days=3), TODAY + dt.timedelta(days=1), "test")

    def report(self, donations=False, period=None):
        with self.db.read() as c:
            return ER.compute(c, period or self.whole_period(), donations, [])

    def scenario(self):
        """Tax + a member deposit + a donation come in; a member withdraws; the alliance pays a grant. All through the real code."""
        self.scan()                                                           # baseline
        self.pnw.taxrecs = [self.tax_rec(8001, 1, 1000.0, f"{self.day()} 14:00:02", tax_id=3),
                            self.tax_rec(8002, 2, 2000.0, f"{self.day()} 14:00:03", tax_id=4)]
        self.pnw.recs.append(rec(50, 1, {"money": 50000.0, "coal": 5.0}))                              # member 1 deposit: $50,000 + coal
        self.pnw.recs.append(rec(51, 2, {"money": 3000.0}, note="#ignore"))                          # alliance donation $3,000
        self.scan()
        self.pnw.holdings = {"money": 500000000, "coal": 5000000}                                      # plenty in the bank
        self.assertEqual(self.run_async(self.svc.wd.request(
            tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1, lock_id=None, dest_nation_id=1,
            amounts={"money": 1000000}, actor="111", note="n", reason="r", idempotency_key="m1")).status, "COMPLETED")
        self.assertEqual(self.run_async(self.svc.wd.request(
            tx_type="WITHDRAW_ECON", funding_source="ALLIANCE", member_nation_id=None, lock_id=None, dest_nation_id=2,
            amounts={"money": 700000, "coal": 100}, actor="9", note="n", reason="grant: help", idempotency_key="a1")).status, "COMPLETED")
        self.scan()                                                                                   # PnW shows the records

    def test_revenue_is_tax_only_by_default_and_members_money_is_never_counted(self):
        self.scenario()
        r = self.report()
        self.assertEqual(r.tax.amounts, {"money": 300000, "coal": 2000})      # $1,000 + $2,000 cash (in cents) and 10 coal per record (in hundredths)
        self.assertEqual(r.tax.count, 2)
        t = ER.totals(r, SNAP)
        self.assertEqual(t["revenue"], 300000 + 2000 * 100)                   # cash + the coal at the snapshot price; donations are NOT in revenue
        self.assertEqual(r.donations.amounts, {"money": 300000})              # ...but are reported as left out
        self.assertEqual(r.member_deposits.amounts, {"money": 5000000, "coal": 500})
        self.assertEqual(r.member_withdrawals.amounts, {"money": 1000000})
        self.assertEqual(t["expenditure"], 700000 + 100 * 100)                # the grant: $7,000 + 100 coal at $1.00
        self.assertEqual(t["net"], 500000 - 710000)

    def test_donations_are_counted_only_when_asked(self):
        self.scenario()
        t_off, t_on = ER.totals(self.report(False), SNAP), ER.totals(self.report(True), SNAP)
        self.assertEqual(t_on["revenue"] - t_off["revenue"], 300000)
        self.assertEqual(t_on["expenditure"], t_off["expenditure"])

    def test_report_revenue_equals_what_the_turn_summary_adds_up(self):
        self.scenario()
        with self.db.read() as c:
            turn = sum(__import__("json").loads(x["totals_json"]).get("money", 0) for x in c.execute("SELECT totals_json FROM tax_turns"))
        self.assertEqual(self.report().tax.amounts["money"], turn)

    def test_member_withdrawal_changes_neither_revenue_nor_expenditure(self):
        self.scenario()
        before = ER.totals(self.report(), SNAP)
        self.pnw.holdings = {"money": 500000000, "coal": 5000000}
        self.run_async(self.svc.wd.request(
            tx_type="WITHDRAW_SELF", funding_source="MEMBER_AVAILABLE", member_nation_id=1, lock_id=None, dest_nation_id=1,
            amounts={"money": 500000}, actor="111", note="n", reason="r", idempotency_key="m2"))
        self.scan()
        r = self.report()
        self.assertEqual(ER.totals(r, SNAP)["expenditure"], before["expenditure"])
        self.assertEqual(r.member_withdrawals.amounts, {"money": 1500000})

    def test_categories_name_where_the_money_went(self):
        self.scenario()
        self.assertEqual(list(self.report().expenditure), ["Other payments by staff"])
        with self.db.tx() as c:                                               # the same payment recorded as a grant is labelled so
            c.execute("INSERT INTO grants(created_at,recipient_nation_id,amounts_json,purpose,requested_by,status,tx_id) "
                      "SELECT '2026-11-27T00:00:00Z',2,'{}','p','9','PENDING',tx_id FROM pnw_records WHERE classification='OUTGOING_TX' "
                      "AND tx_id IN (SELECT id FROM transactions WHERE funding_source='ALLIANCE')")
        self.assertEqual(list(self.report().expenditure), ["Grants"])

    def test_unreviewed_outflows_are_shown_but_never_counted(self):
        self.scenario()
        self.pnw.recs.append(rec(60, 0, {"money": 9000.0}, receiver=77, receiver_type=1, sender_type=2))
        self.pnw.recs[-1]["sender_id"] = self.settings.alliance_id
        self.scan()
        r = self.report()
        self.assertEqual(r.needs_review.amounts, {"money": 900000})
        self.assertEqual(ER.totals(r, SNAP)["expenditure"], 710000)           # unchanged: not guessed
        card = ER.build_card(r, SNAP)
        self.assertIn("Needs review", " ".join(f[0] for f in card.fields))
        with self.db.tx() as c:                                               # staff decide it was alliance money (what /bank review does)
            c.execute("UPDATE pnw_records SET classification='OTHER', status='NO_CREDIT' WHERE id=60")
        r2 = self.report()
        self.assertEqual(r2.needs_review.count, 0)
        self.assertEqual(ER.totals(r2, SNAP)["expenditure"], 710000 + 900000)
        self.assertIn("Sent outside TUN Bank (reviewed as alliance money)", r2.expenditure)

    def test_moves_between_our_own_banks_are_not_expenditure(self):
        self.scenario()
        self.pnw.recs.append(rec(61, 0, {"money": 8000.0}, receiver=999, receiver_type=2, sender_type=2))
        self.pnw.recs[-1]["sender_id"] = self.settings.alliance_id
        self.scan()
        with self.db.read() as c:
            self.assertEqual(ER.compute(c, self.whole_period(), False, [999]).internal_moves.amounts, {"money": 800000})
            self.assertEqual(ER.totals(ER.compute(c, self.whole_period(), False, [999]), SNAP)["expenditure"], 710000)

    def test_only_the_asked_period_is_counted(self):
        self.scan()
        self.pnw.taxrecs = [self.tax_rec(8101, 1, 1000.0, "2026-10-31 23:59:59", tax_id=3), self.tax_rec(8102, 1, 2000.0, "2026-11-01 00:00:00", tax_id=3),
                            self.tax_rec(8103, 1, 4000.0, "2026-12-01 00:00:00", tax_id=3)]
        self.scan()
        nov = ER.period_for("custom", d(2026, 11, 30), "2026-11-01", "2026-11-30")
        with self.db.read() as c:
            self.assertEqual(ER.compute(c, nov, False).tax.amounts, {"money": 200000, "coal": 1000})

    def test_baseline_history_is_not_counted_and_the_card_says_when_data_starts(self):
        self.pnw.recs.append(rec(5, 1, {"money": 123.0}))
        self.scan()                                                           # first scan = baseline
        self.assertEqual(self.report().member_deposits.count, 0)
        p = ER.Period(d(2020, 1, 1), d(2020, 2, 1), "old")
        with self.db.read() as c:
            card = ER.build_card(ER.compute(c, p, False), SNAP)
        self.assertEqual(card.fields[0][0].split(" · ")[0], "💰 Revenue")

    def test_card_fits_discord_and_reads_clearly(self):
        self.scenario()
        r = self.report()
        card = ER.build_card(r, SNAP, self.report(period=ER.previous_of(self.whole_period())), SNAP)
        fake_discord.check_embed(to_embed(card))
        names = [f[0] for f in card.fields]
        self.assertTrue(any(n.startswith("💰 Revenue") for n in names) and any(n.startswith("💸 Expenditure") for n in names))
        self.assertIn("📈 Net result", names)
        self.assertIn("deficit", dict((f[0], f[1]) for f in card.fields)["📈 Net result"])
        self.assertIn("Members' deposits and withdrawals are not counted", card.description)

    def test_detail_file_lists_every_line_with_its_section(self):
        self.scenario()
        text = ER.detail_csv(self.report(), SNAP).decode("utf-8-sig")
        sections = {ln.split(",")[0] for ln in text.splitlines()[1:]}
        self.assertEqual(sections, {"REVENUE", "EXPENDITURE", "NOT COUNTED"})
        self.assertIn("Member deposit", text)
        self.assertIn("Member withdrawal", text)

    # ---------------------------------------------------------------- commands
    def economy(self):
        from discord import app_commands
        from tunbank import cmds_report
        g = app_commands.Group(name="economy", description="x")
        cmds_report.register(g, self.svc)
        return g

    def test_command_is_for_people_who_may_see_alliance_finances(self):
        g = self.economy()
        out = self.call(g, "report", self.alice).text()
        self.assertIn("confidential", out.lower())
        i = self.call(g, "report", self.admin)
        self.assertIn("Economy report", i.text())

    def test_command_report_with_detail_attaches_a_file_and_bad_dates_are_explained(self):
        self.scenario()
        g = self.economy()
        bad = self.call(g, "report", self.admin, types.SimpleNamespace(value="custom"), start="nonsense")
        self.assertIn("year-month-day", bad.text())
        ok = self.call(g, "report", self.admin, types.SimpleNamespace(value="30d"), detail=True,
                       donations=types.SimpleNamespace(value="include"))
        self.assertTrue(ok.sent[-1].get("file"), "the detail file should be attached")

    def test_auto_setting_needs_minister_and_is_audited(self):
        g = self.economy()
        self.assertIn("permission", self.call(g, "auto", self.alice, weekly=types.SimpleNamespace(value="1")).text().lower())
        self.call(g, "auto", self.admin, weekly=types.SimpleNamespace(value="1"), monthly=types.SimpleNamespace(value="1"),
                  donations=types.SimpleNamespace(value="1"))
        with self.db.read() as c:
            self.assertEqual([cfg_get(c, k) for k in ("econ_report_weekly", "econ_report_monthly", "econ_report_donations")], ["1", "1", "1"])
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='CONFIG_CHANGED' AND target LIKE 'econ_report%'").fetchone()[0], 3)

    # ---------------------------------------------------------------- the schedule
    def set(self, **kw):
        with self.db.tx() as c:
            for k, v in kw.items():
                cfg_set(c, k, v, "t")

    def send(self, now):
        return self.run_async(ER.send_due_reports(self.svc, now))

    def test_schedule_never_posts_an_old_report_when_switched_on_and_posts_each_period_once(self):
        self.set(econ_report_weekly="1", econ_report_monthly="1")
        self.assertEqual(self.send(d(2026, 11, 24, 3)), [])                   # switched on: remembers, posts nothing
        self.assertEqual(self.econ_log.sent, [])
        self.assertEqual(self.send(d(2026, 11, 25, 3)), [])
        posted = self.send(d(2026, 11, 30, 1, 5))                             # the week ending Sunday 29 Nov is finished
        self.assertEqual(posted, [("weekly", "2026-W48")])
        self.assertEqual(self.send(d(2026, 11, 30, 1, 20)), [])               # never twice
        self.assertEqual(len(self.econ_log.sent), 1)
        posted = self.send(d(2026, 12, 1, 1, 5))                              # November is finished
        self.assertEqual(posted, [("monthly", "2026-11")])
        self.assertIn("Monthly economy report", self.econ_log.text())
        self.assertIn("November 2026", self.econ_log.text())

    def test_schedule_waits_for_the_last_turn_and_for_a_channel_and_forgets_when_off(self):
        self.set(econ_report_weekly="1")
        self.send(d(2026, 11, 24, 3))
        self.assertEqual(self.send(d(2026, 11, 30, 0, 30)), [])               # only 30 minutes after the week ended
        self.set(econ_log_channel_id="")
        self.assertEqual(self.send(d(2026, 11, 30, 2)), [])                   # no channel: nothing posted, nothing remembered
        self.set(econ_log_channel_id="111000")
        self.assertEqual(self.send(d(2026, 11, 30, 2)), [("weekly", "2026-W48")])
        self.set(econ_report_weekly="0")
        self.send(d(2026, 12, 1, 3))
        with self.db.read() as c:
            self.assertFalse(L.get_state(c, "econ_report_last_weekly"))      # forgotten: switching on again never posts an old week

    def test_a_separate_report_channel_wins_over_the_econ_log(self):
        self.set(econ_report_weekly="1", econ_report_channel_id="222000")
        self.send(d(2026, 11, 24, 3))
        self.send(d(2026, 11, 30, 2))
        self.assertEqual((len(self.out.sent), len(self.econ_log.sent)), (1, 0))


if __name__ == "__main__":
    unittest.main()
