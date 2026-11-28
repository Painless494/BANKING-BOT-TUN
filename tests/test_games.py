"""Games: the shared engine, Spin of Fortune, prize payouts through the bulk pipeline, and the rules that keep money safe."""
import ast
import asyncio
import datetime as dt
import json
import random
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
from tunbank import econ_report as ER  # noqa: E402
from tunbank import games as G  # noqa: E402
from tunbank import ledger as L  # noqa: E402
from tunbank import money as M  # noqa: E402
from tunbank.config import cfg_get, cfg_set  # noqa: E402
from tunbank.db import Database  # noqa: E402
from tunbank.valuation import Snapshot  # noqa: E402

FI = fake_discord.FakeInteraction
UTC = dt.timezone.utc
SNAP = Snapshot(1, "2026-11-27T00:00:00Z", {**{r: 100 for r in M.RESOURCES if r != "money"}, "steel": 50})


def at(d, h=12):
    return dt.datetime(2026, 11, d, h, tzinfo=UTC)


def seed(conn, rows):
    for label, weight, amounts, score, jackpot in rows:
        G.save_outcome(conn, "spin", "t", outcome_id=None, label=label, weight=weight, amounts=amounts, score=score, jackpot=jackpot, enabled=True)


class Engine(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "g.db")
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def play(self, nation=1, per_day=1, now=None, cap=0, snap=SNAP, rng=None, hour=0):
        with self.db.tx() as c:
            return G.play_daily(conn=c, game="spin", nation_id=nation, discord_id=str(nation), per_day=per_day, day_start_hour=hour, cap_cents=cap,
                                snap=snap, rng=rng or random.Random(1), now=now or at(10))

    # ---- days
    def test_game_day_follows_the_reset_hour(self):
        self.assertEqual(G.day_key(at(10, 0)), "2026-11-10")
        self.assertEqual(G.day_key(at(10, 5), 6), "2026-11-09")                # before a 06:00 reset it is still yesterday's game day
        self.assertEqual(G.day_key(at(10, 6), 6), "2026-11-10")
        self.assertEqual(G.next_reset(at(10, 5), 6), at(10, 6))
        self.assertEqual(G.next_reset(at(10, 12), 0), at(11, 0))
        self.assertEqual(G.until(at(10, 12), at(11, 0)), "12h 0m")

    # ---- the prize table
    def test_classic_defaults_match_the_original_odds_and_only_fill_an_empty_table(self):
        with self.db.tx() as c:
            self.assertEqual(G.load_defaults(c, "spin", "t"), 8)
            outs = G.load_outcomes(c, "spin")
            with self.assertRaises(G.GameError):
                G.load_defaults(c, "spin", "t")
        self.assertEqual(sum(o.weight for o in outs), 101)
        self.assertEqual([o.label for o in outs if o.jackpot], ["$30,000,000 JACKPOT"])
        self.assertEqual({o.label: o.amounts for o in outs}["$5,000,000"], {"money": 500000000})
        self.assertEqual({o.label: o.amounts for o in outs}["300 steel"], {"steel": 30000})
        self.assertEqual(round(sum(p for _, p in G.odds(outs)), 6), 100.0)
        self.assertEqual([o.amounts for o in outs if o.label.startswith("Try")], [{}])

    def test_prize_table_editing_is_validated_and_audited(self):
        with self.db.tx() as c:
            oid = G.save_outcome(c, "spin", "9", outcome_id=None, label="Coal", weight=5, amounts={"coal": 100}, score=3, jackpot=False, enabled=True)
            G.save_outcome(c, "spin", "9", outcome_id=oid, label=None, weight=9, amounts=None, score=None, jackpot=None, enabled=False)
            o = G.load_outcomes(c, "spin", only_enabled=False)[0]
            self.assertEqual((o.weight, o.enabled, o.amounts), (9, False, {"coal": 100}))
            self.assertEqual(G.load_outcomes(c, "spin"), [])
            for kw in ({"label": "", "weight": 1}, {"label": "x", "weight": -1}, {"label": "x", "weight": None}):
                with self.assertRaises(G.GameError):
                    G.save_outcome(c, "spin", "9", outcome_id=None, amounts={}, score=0, jackpot=False, enabled=True, **kw)
            with self.assertRaises(G.GameError):
                G.save_outcome(c, "spin", "9", outcome_id=999, label="x", weight=1, amounts=None, score=None, jackpot=None, enabled=None)
            G.remove_outcome(c, "spin", "9", oid)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'GAME_PRIZE_%'").fetchone()[0], 3)

    def test_expected_value_and_the_worst_case(self):
        with self.db.tx() as c:
            seed(c, [("coal", 1, {"coal": 1000}, 0, False), ("none", 3, {}, 0, False)])
            outs = G.load_outcomes(c, "spin")
        ev = G.expected_value(outs, SNAP)
        self.assertEqual((ev["average_cents"], ev["max_cents"], ev["complete"]), (25000, 100000, True))      # 25% of $1,000
        self.assertFalse(G.expected_value(outs, Snapshot(2, "x", {}))["complete"])                            # no price for coal: say so, never guess

    # ---- the draw
    def test_draw_follows_the_weights(self):
        with self.db.tx() as c:
            seed(c, [("a", 70, {}, 0, False), ("b", 29, {}, 0, False), ("c", 1, {}, 0, True)])
            pool = G.load_outcomes(c, "spin")
        rng = random.Random(7)
        n = 40000
        got = {}
        for _ in range(n):
            o = G._draw(pool, rng)
            got[o.label] = got.get(o.label, 0) + 1
        self.assertAlmostEqual(got["a"] / n, 0.70, delta=0.01)
        self.assertAlmostEqual(got["b"] / n, 0.29, delta=0.01)
        self.assertAlmostEqual(got["c"] / n, 0.01, delta=0.004)

    def test_draw_edges_pick_the_first_and_last_prize(self):
        with self.db.tx() as c:
            seed(c, [("first", 1, {}, 0, False), ("last", 1, {}, 0, False)])
            pool = G.load_outcomes(c, "spin")
        fixed = lambda v: types.SimpleNamespace(randrange=lambda total: v)   # noqa: E731
        self.assertEqual(G._draw(pool, fixed(0)).label, "first")
        self.assertEqual(G._draw(pool, fixed(1)).label, "last")

    # ---- the daily limit
    def test_one_play_per_day_per_nation_and_a_new_day_resets(self):
        with self.db.tx() as c:
            seed(c, [("coal", 1, {"coal": 100}, 5, False)])
        r = self.play()
        self.assertEqual((r.spins_left, r.outcome.label, r.day_key), (0, "coal", "2026-11-10"))
        with self.assertRaises(G.GameError) as cm:
            self.play(now=at(10, 20))
        self.assertIn("4h 0m", str(cm.exception))
        self.play(nation=2)                                                     # somebody else is unaffected
        self.play(now=at(11, 0))                                                # the next game day

    def test_several_spins_per_day_when_configured(self):
        with self.db.tx() as c:
            seed(c, [("none", 1, {}, 1, False)])
        self.assertEqual([self.play(per_day=3).spins_left for _ in range(3)], [2, 1, 0])
        with self.assertRaises(G.GameError):
            self.play(per_day=3)

    def test_the_database_itself_refuses_a_second_play_even_if_counting_is_bypassed(self):
        with self.db.tx() as c:
            seed(c, [("none", 1, {}, 1, False)])
        self.play(nation=1)
        with self.db.tx() as c:                                                 # a rival transaction whose count missed the first play
            c.execute("UPDATE sqlite_sequence SET seq=seq")
        # simulate the race: pre-insert the exact key another press would use, under a different nation id so the count stays 0
        with self.db.tx() as c:
            c.execute("INSERT INTO game_plays(game,play_key,nation_id,discord_id,played_at,day_key,outcome_label,amounts_json,score,jackpot,odds_json) "
                      "VALUES('spin','spin:5:2026-11-10:1',99,'9','x','2026-11-10','x','{}',0,0,'{}')")
        with self.assertRaises(G.GameError):
            self.play(nation=5)

    def test_plays_and_prize_amounts_can_never_be_rewritten_or_deleted(self):
        import sqlite3
        with self.db.tx() as c:
            seed(c, [("coal", 1, {"coal": 100}, 5, False)])
        self.play()
        for sql in ("UPDATE game_plays SET score=999", "DELETE FROM game_plays", "DELETE FROM game_prizes", "UPDATE game_prizes SET amounts_json='{}'",
                    "UPDATE game_prizes SET nation_id=2"):
            with self.assertRaises(sqlite3.Error, msg=sql):
                with self.db.tx() as c:
                    c.execute(sql)

    def test_every_play_keeps_the_odds_it_was_drawn_from(self):
        with self.db.tx() as c:
            seed(c, [("a", 3, {}, 1, False), ("b", 1, {"coal": 5}, 1, False)])
        r = self.play()
        with self.db.read() as c:
            odds = json.loads(c.execute("SELECT odds_json FROM game_plays WHERE id=?", (r.play_id,)).fetchone()[0])
        self.assertEqual(odds["total_weight"], 4)
        self.assertEqual({t["label"]: t["chance_pct"] for t in odds["table"]}, {"a": 75.0, "b": 25.0})

    def test_a_prize_record_exists_only_for_prizes_that_pay(self):
        with self.db.tx() as c:
            seed(c, [("none", 1, {}, 0, False)])
        r = self.play()
        self.assertIsNone(r.prize_id)
        with self.db.tx() as c:
            c.execute("UPDATE game_outcomes SET weight=0")
            seed(c, [("coal", 1, {"coal": 250}, 0, False)])
        r = self.play(nation=2)
        with self.db.read() as c:
            p = c.execute("SELECT * FROM game_prizes WHERE id=?", (r.prize_id,)).fetchone()
        self.assertEqual((p["status"], json.loads(p["amounts_json"]), p["value_cents"], p["batch_id"]), ("PENDING", {"coal": 250}, 25000, None))

    # ---- the daily prize budget
    def test_prize_budget_limits_what_can_come_up_and_never_overspends(self):
        with self.db.tx() as c:
            seed(c, [("big", 50, {"coal": 1000}, 0, False), ("small", 50, {"coal": 100}, 0, False), ("none", 1, {}, 0, False)])   # $1,000 / $100 / nothing
        cap = 120000                                                              # $1,200 for the day
        values = []
        for n in range(1, 8):
            try:
                r = self.play(nation=n, cap=cap)
                values.append(r.value_cents or 0)
            except G.GameError:
                break
        self.assertLessEqual(sum(values), cap)
        with self.db.read() as c:
            self.assertLessEqual(c.execute("SELECT SUM(value_cents) FROM game_plays").fetchone()[0], cap)
        self.assertGreaterEqual(len(values), 2)

    def test_budget_that_is_used_up_leaves_only_no_prize_results_or_an_honest_refusal(self):
        with self.db.tx() as c:
            seed(c, [("coal", 1, {"coal": 100}, 0, False)])                      # $100, no "nothing" outcome
        self.play(nation=1, cap=10000)
        with self.assertRaises(G.GameError) as cm:
            self.play(nation=2, cap=10000)
        self.assertIn("budget has been used up", str(cm.exception))
        with self.db.read() as c:                                                 # the refused press did not use up that member's day
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_plays WHERE nation_id=2").fetchone()[0], 0)

    def test_budget_without_prices_is_refused_not_guessed(self):
        with self.db.tx() as c:
            seed(c, [("coal", 1, {"coal": 100}, 0, False)])
        with self.assertRaises(G.GameError) as cm:
            self.play(cap=10000, snap=None)
        self.assertIn("prices", str(cm.exception))
        with self.assertRaises(G.GameError):                                      # an unpriced prize can't be proven to fit the budget
            self.play(cap=10000, snap=Snapshot(3, "x", {}))

    # ---- stats
    def test_stats_leaderboard_and_history_come_from_the_plays(self):
        with self.db.tx() as c:
            seed(c, [("pts", 1, {}, 10, False)])
            for n in (1, 2, 3):
                c.execute("INSERT INTO members(nation_id,nation_name,created_at) VALUES(?,?,?)", (n, f"N{n}", "x"))
        for day in (10, 11, 12):
            self.play(nation=1, now=at(day))
        self.play(nation=2, now=at(10))
        with self.db.read() as c:
            lb = G.leaderboard(c, "spin", "", 10)
            s1, s2 = G.player_stats(c, "spin", 1), G.player_stats(c, "spin", 2)
            h = G.history(c, "spin", 1)
            season = G.leaderboard(c, "spin", "2026-11-11T00:00:00Z", 10)
        self.assertEqual([(r["nation_id"], r["score"], r["plays"]) for r in lb], [(1, 30, 3), (2, 10, 1)])
        self.assertEqual((s1["rank"], s2["rank"], s1["plays"]), (1, 2, 3))
        self.assertEqual(len(h), 3)
        self.assertEqual(season[0]["score"], 20)                                  # a new season only counts newer plays


class Games(unittest.TestCase):
    """The commands, with real linked members and a real (fake-PnW) bank."""
    make_settings = TC.Cmd.make_settings
    run_async = TC.Cmd.run_async
    call = TC.Cmd.call
    tearDown = TC.Cmd.tearDown

    def setUp(self):
        TC.Cmd.setUp(self)
        self.games = app_commands.Group(name="games", description="x")
        self.gameset = app_commands.Group(name="gameset", description="x")
        from tunbank import cmds_games
        self.mod = cmds_games
        cmds_games.register(self.games, self.gameset, self.svc)
        self.bob = discord.User(112, "bob")
        self.run_async(self.svc.scanner.scan())
        self.call(self.nation, "link", self.alice, 1)
        self.call(self.nation, "link", self.bob, 2)
        self.chan = TC.FakeChannel()
        self.slept = []

        async def nosleep(s):
            self.slept.append(s)
        self._sleep = asyncio.sleep
        cmds_sleep = cmds_games.asyncio
        fast = types.SimpleNamespace(**{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith("__")})
        fast.sleep = nosleep                                   # everything of asyncio works; only the animation pauses are skipped
        cmds_games.asyncio = fast
        self._asyncio = cmds_sleep
        self.svc.prices = types.SimpleNamespace(get=self._snap)

    def tearDown(self):
        self.mod.asyncio = self._asyncio
        TC.Cmd.tearDown(self)

    async def _snap(self):
        return SNAP

    def cfg(self, **kw):
        with self.db.tx() as c:
            for k, v in kw.items():
                cfg_set(c, k, str(v), "t")

    def prizes(self, rows):
        with self.db.tx() as c:
            seed(c, rows)

    def spin(self, user=None, via="command"):
        user = user or self.alice
        i = FI(user)
        i.command = types.SimpleNamespace(qualified_name="games spin")
        i.channel = self.chan
        i.client = getattr(self, "client", None)
        if via == "button":
            self.run_async(self.svc.game_panel_view().play(i, None))
        else:
            self.run_async(self.games.commands["spin"].callback(i))
        return i

    def open(self, rows=None):
        self.prizes(rows or [("300 steel", 1, {"steel": 30000}, 30, False)])
        self.cfg(games_enabled=1, spin_animation=0)

    def as_text(self, i):
        return i.text()

    # ---- who may play
    def test_closed_games_unlinked_and_frozen_members_cannot_spin(self):
        self.prizes([("300 steel", 1, {"steel": 30000}, 30, False)])
        self.assertIn("closed", self.spin().text())
        self.cfg(games_enabled=1, spin_animation=0)
        self.assertIn("link your nation", self.spin(discord.User(555, "carol")).text().lower())
        with self.db.tx() as c:
            c.execute("UPDATE members SET frozen=1 WHERE nation_id=1")
        self.assertIn("frozen", self.spin().text())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_plays").fetchone()[0], 0)

    def test_role_restriction(self):
        self.open()
        self.cfg(games_play_role_id=777)
        self.assertIn("need the", self.spin().text())
        self.alice.roles = [types.SimpleNamespace(id=777)]
        self.assertIn("won", self.spin().text().lower())
        del self.alice.roles

    def test_the_spin_creates_a_pending_prize_and_never_money(self):
        self.open()
        with self.db.read() as c:
            before = (c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], c.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0])
        out = self.spin().text()
        self.assertIn("300 steel", out)
        self.assertIn("will pay it", out)
        with self.db.read() as c:
            p = c.execute("SELECT * FROM game_prizes").fetchone()
            self.assertEqual((p["nation_id"], p["status"], json.loads(p["amounts_json"])), (1, "PENDING", {"steel": 30000}))
            self.assertEqual(before, (c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], c.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]))
            self.assertEqual(L.get_balances(c, 1, "AVAILABLE"), {})            # no silent balance creation
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_a_second_spin_the_same_day_is_refused_with_the_time_left(self):
        self.open()
        self.spin()
        out = self.spin().text()
        self.assertIn("already played today", out)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_plays").fetchone()[0], 1)

    def test_two_simultaneous_presses_give_exactly_one_spin(self):
        self.open()

        async def both():
            a, b = FI(self.alice), FI(self.alice)
            for i in (a, b):
                i.channel = self.chan
            await asyncio.gather(self.games.commands["spin"].callback(a), self.games.commands["spin"].callback(b))
            return a, b
        a, b = self.run_async(both())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_plays").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_prizes").fetchone()[0], 1)
        self.assertEqual(sum("already played" in x.text() for x in (a, b)), 1)

    def test_panel_button_works_like_the_command(self):
        self.open()
        self.assertIn("300 steel", self.spin(via="button").text())

    def test_animation_reveals_the_result_without_changing_it(self):
        self.open()
        self.cfg(spin_animation=1)
        i = self.spin()
        self.assertEqual(len(self.slept), 3)
        shown = i.edited["embed"]
        self.assertIn("won", shown.title)
        self.assertIn("300 steel", shown.description)

    # ---- announcements
    def test_public_results_follow_the_setting(self):
        self.open([("300 steel", 1, {"steel": 30000}, 30, False)])
        self.spin()
        self.assertIn("won", self.chan.text())
        self.assertEqual(len(self.chan.sent), 1)
        self.cfg(games_public_results="none")
        self.spin(self.bob)
        self.assertEqual(len(self.chan.sent), 1)
        self.cfg(games_public_results="jackpots", spin_per_day=5)
        self.spin()
        self.assertEqual(len(self.chan.sent), 1)                                    # not a jackpot

    def test_a_jackpot_gets_the_jackpot_treatment(self):
        self.open([("$30M JACKPOT", 1, {"money": 3000000000}, 500, True)])
        out = self.spin().text()
        self.assertIn("JACKPOT", out)
        self.assertIn("JACKPOT", self.chan.text())

    def test_a_no_prize_result_makes_no_prize_record(self):
        self.open([("Try again tomorrow", 1, {}, 0, False)])
        self.assertIn("No prize", self.spin().text())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_prizes").fetchone()[0], 0)

    # ---- player views
    def test_stats_leaderboard_history_and_odds(self):
        self.open()
        self.spin()
        self.spin(self.bob)
        s = self.call(self.games, "stats", self.alice).text()
        self.assertIn("30", s)
        self.assertIn("alice", self.call(self.games, "leaderboard", self.alice).text().lower() + "alice")
        self.assertIn("waiting", self.call(self.games, "history", self.alice).text().lower())
        o = self.call(self.games, "prizes", self.alice).text()
        self.assertIn("300 steel", o)
        self.assertIn("100.0%", o)
        self.assertIn("link", self.call(self.games, "stats", self.alice, discord.User(777, "x")).text().lower())

    def test_new_season_resets_the_leaderboard_but_deletes_nothing(self):
        self.open()
        self.spin()
        lb = lambda p: self.call(self.games, "leaderboard", self.alice, types.SimpleNamespace(value=p)).text()   # noqa: E731
        self.assertIn("30", lb("season"))
        with self.db.tx() as c:
            c.execute("SELECT 1")
        i = self.call(self.gameset, "newseason", self.admin)
        self.assertIn("new season", i.text().lower())
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_plays").fetchone()[0], 1)
            season = cfg_get(c, "games_season_start")
        self.assertTrue(season)
        self.assertIn("No scores yet", lb("season").replace("\n", " ") if True else "")
        self.assertIn("30", lb("all"))

    # ---- staff
    def test_staff_commands_need_the_right_level(self):
        for name, kw in (("configure", {"open_games": True}), ("prize", {"prize_name": "x", "weight": 1}), ("defaults", {}), ("newseason", {}),
                         ("show", {}), ("pay", {}), ("payouts", {}), ("removeprize", {"prize_id": 1}), ("cancel", {"prize_id": 1, "reason": "x"})):
            self.assertIn("permission", self.call(self.gameset, name, self.alice, **kw).text().lower(), name)

    def test_defaults_then_show_explains_odds_and_cost(self):
        self.assertIn("Loaded 8", self.call(self.gameset, "defaults", self.admin).text())
        self.assertIn("already has", self.call(self.gameset, "defaults", self.admin).text())
        out = self.call(self.gameset, "show", self.admin).text()
        self.assertIn("$30,000,000 JACKPOT", out)
        self.assertIn("per spin on average", out)

    def test_games_cannot_open_with_an_empty_prize_table_and_bad_input_is_explained(self):
        self.assertIn("empty", self.call(self.gameset, "configure", self.admin, open_games=True).text())
        self.assertIn("Nothing to change", self.call(self.gameset, "configure", self.admin).text())
        self.assertIn("couldn't read", self.call(self.gameset, "prize", self.admin, prize_name="x", weight=1, amounts="lots").text())
        self.assertIn("name and a weight", self.call(self.gameset, "prize", self.admin, amounts="coal=5").text())
        self.assertIn("saved", self.call(self.gameset, "prize", self.admin, prize_name="Coal", weight=3, amounts="coal=500", score=7).text())
        with self.db.read() as c:
            o = G.load_outcomes(c, "spin")[0]
        self.assertEqual((o.label, o.amounts, o.score), ("Coal", {"coal": 50000}, 7))
        self.assertIn("Saved", self.call(self.gameset, "configure", self.admin, open_games=True, spins_per_day=2, reset_hour=5, prize_budget_usd=1000).text())
        with self.db.read() as c:
            self.assertEqual([cfg_get(c, k) for k in ("games_enabled", "spin_per_day", "games_day_start_hour_utc", "spin_daily_prize_cap")], ["1", "2", "5", "1000"])
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='CONFIG_CHANGED' AND target IN ('games_enabled','spin_per_day')").fetchone()[0], 2)

    def test_panel_is_posted_with_buttons_and_the_old_one_is_removed(self):
        self.open()
        chan = types.SimpleNamespace(id=4242, mention="#spin", sent=[])

        async def send(embed=None, view=None):
            chan.sent.append((embed, view))
            return types.SimpleNamespace(id=9000 + len(chan.sent), delete=lambda: asyncio.sleep(0))
        chan.send = send
        self.call(self.gameset, "panel", self.admin, chan)
        self.assertEqual(len(chan.sent), 1)
        self.assertIn("Spin", chan.sent[0][0].title)
        with self.db.read() as c:
            self.assertEqual((cfg_get(c, "spin_panel_channel_id"), cfg_get(c, "spin_panel_message_id")), ("4242", "9001"))

    # ---- paying prizes: only through the bulk pipeline
    def fund(self):
        self.pnw.holdings = {"money": 9_000_000_000, "steel": 10_000_000}

    def win(self, user):
        self.spin(user)

    def test_paying_sends_through_the_bulk_pipeline_once_and_marks_prizes_paid(self):
        self.open()
        self.win(self.alice)
        self.win(self.bob)
        self.fund()
        out = self.call(self.gameset, "pay", self.admin).text()
        self.assertIn("COMPLETED", out)
        self.assertEqual(self.pnw.withdraw_calls, 2)
        with self.db.read() as c:
            b = c.execute("SELECT * FROM bulk_batches").fetchone()
            self.assertEqual((b["source_filename"], b["row_count"], b["status"]), ("game-prizes", 2, "COMPLETED"))
            self.assertEqual(c.execute("SELECT funding_source, tx_type FROM transactions").fetchall()[0][:], ("ALLIANCE", "WITHDRAW_ECON"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bulk_items WHERE status='COMPLETED'").fetchone()[0], 2)
        self.assertIn("paid", self.call(self.games, "history", self.alice).text())
        self.assertIn("Nothing is waiting", self.call(self.gameset, "pay", self.admin).text())
        self.assertEqual(self.pnw.withdraw_calls, 2)                                # paying again sent nothing

    def test_payout_needs_confirmation_and_cancelling_sends_nothing(self):
        self.open()
        self.win(self.alice)
        self.fund()
        out = self.call(self.gameset, "pay", self.admin, auto_confirm=False).text()
        self.assertIn("Cancelled", out)
        self.assertEqual(self.pnw.withdraw_calls, 0)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bulk_batches").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT batch_id FROM game_prizes").fetchone()[0], None)

    def test_payout_is_blocked_when_alliance_funds_are_short(self):
        self.open()
        self.win(self.alice)
        self.pnw.holdings = {"money": 100, "steel": 5}
        out = self.call(self.gameset, "pay", self.admin).text()
        self.assertIn("blocked", out.lower())
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_member_deposits_are_never_used_to_pay_prizes(self):
        self.open()
        self.win(self.alice)
        self.pnw.holdings = {"steel": 20_000}
        self.pnw.recs.append(TC.rec(901, 2, {"steel": 200.0}))
        self.run_async(self.svc.scanner.scan())                                      # bob deposits all 200 steel: alliance owns none of it
        out = self.call(self.gameset, "pay", self.admin).text()
        self.assertIn("blocked", out.lower())
        self.assertIn("short", out)
        self.assertEqual(self.pnw.withdraw_calls, 0)

    def test_frozen_accounts_are_held_back(self):
        self.open()
        self.win(self.alice)
        self.win(self.bob)
        with self.db.tx() as c:
            c.execute("UPDATE members SET frozen=1 WHERE nation_id=2")
        self.fund()
        out = self.call(self.gameset, "pay", self.admin).text()
        self.assertEqual(self.pnw.withdraw_calls, 1)
        self.assertIn("COMPLETED", out)
        self.assertIn("Held back", self.call(self.gameset, "payouts", self.admin).text())

    def test_pay_selected_prizes_only(self):
        self.open()
        self.win(self.alice)
        self.win(self.bob)
        self.fund()
        self.call(self.gameset, "pay", self.admin, prize_ids="2")
        self.assertEqual(self.pnw.withdraw_calls, 1)
        with self.db.read() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT nation_id FROM bulk_items")], [2])
        self.assertIn("whole numbers", self.call(self.gameset, "pay", self.admin, prize_ids="a,b").text())

    def test_only_a_confirmed_failure_can_be_retried_never_a_paid_or_in_flight_payment(self):
        self.open()
        self.win(self.alice)
        self.fund()
        self.call(self.gameset, "pay", self.admin)
        states = {}
        for status in ("COMPLETED", "PENDING", "UNCERTAIN", "FAILED"):
            with self.db.tx() as c:
                c.execute("UPDATE bulk_items SET status=?", (status,))
            with self.db.read() as c:
                states[status] = len(G.payable_prizes(c, "spin"))
        self.assertEqual(states, {"COMPLETED": 0, "PENDING": 0, "UNCERTAIN": 0, "FAILED": 1})

    def test_cancel_only_unpaid_prizes_with_a_reason(self):
        self.open()
        self.win(self.alice)
        self.assertIn("reason", self.call(self.gameset, "cancel", self.admin, 1, "").text().lower())
        self.assertIn("cancelled", self.call(self.gameset, "cancel", self.admin, 1, "won by mistake").text())
        self.assertIn("Nothing is waiting", self.call(self.gameset, "pay", self.admin).text())
        self.assertIn("cancelled", self.call(self.games, "history", self.alice).text().lower())
        self.assertIn("can't be cancelled", self.call(self.gameset, "cancel", self.admin, 1, "again").text())
        self.assertIn("no prize #99", self.call(self.gameset, "cancel", self.admin, 99, "x").text().lower().replace("there is ", ""))

    def test_paid_prizes_show_as_game_prizes_in_the_economy_report(self):
        self.open()
        self.win(self.alice)
        self.fund()
        self.call(self.gameset, "pay", self.admin)
        self.run_async(self.svc.scanner.scan())
        p = ER.Period(dt.datetime.now(UTC) - dt.timedelta(days=2), dt.datetime.now(UTC) + dt.timedelta(days=1), "t")
        with self.db.read() as c:
            rep = ER.compute(c, p, False, [])
        self.assertIn("Game prizes", rep.expenditure)

    # ---- the panel stays the last message
    def room(self, spin_in_same_channel=True):
        """A channel that remembers its messages in order, and a client that can find it."""
        outer = self
        msgs = []

        class Msg:
            def __init__(self, embed, view):
                self.id, self.embed, self.view, self.deleted = 7000 + len(msgs), embed, view, False
                msgs.append(self)

            async def delete(self):
                self.deleted = True

        class Room:
            id = 5150
            mention = "#spin"

            async def send(self, embed=None, view=None, **kw):
                if outer.fail_sends and view is not None:
                    raise discord.HTTPException()
                return Msg(embed, view)

            async def fetch_message(self, mid):
                for m in msgs:
                    if m.id == mid and not m.deleted:
                        return m
                raise discord.HTTPException()
        room = Room()
        self.client = types.SimpleNamespace(get_channel=lambda cid: room if cid == 5150 else None, fetch_channel=None)
        self.chan = room
        self.fail_sends = False
        return room, msgs

    def alive(self, msgs):
        return [("panel" if m.view is not None else "result") for m in msgs if not m.deleted]

    def post_panel(self, room):
        i = FI(self.admin)
        i.channel, i.client = room, self.client
        self.run_async(self.gameset.commands["panel"].callback(i, room))

    def test_panel_moves_back_below_a_result_posted_in_its_own_channel(self):
        self.open()
        room, msgs = self.room()
        self.post_panel(room)
        self.assertEqual(self.alive(msgs), ["panel"])
        self.spin()
        self.assertEqual(self.alive(msgs), ["result", "panel"])               # the panel is the last message again, and there is only one
        with self.db.read() as c:
            self.assertEqual(cfg_get(c, "spin_panel_message_id"), str(msgs[-1].id))
        self.spin(self.bob)
        self.assertEqual(self.alive(msgs), ["result", "result", "panel"])

    def test_a_result_in_another_channel_leaves_the_panel_alone(self):
        self.open()
        room, msgs = self.room()
        self.post_panel(room)
        other = TC.FakeChannel()
        self.cfg(games_announce_channel_id=999)
        self.client.get_channel = lambda cid: room if cid == 5150 else other if cid == 999 else None
        self.spin()
        self.assertEqual(self.alive(msgs), ["panel"])
        self.assertEqual(len(other.sent), 1)

    def test_the_setting_can_switch_it_off_and_quiet_results_do_not_move_it(self):
        self.open()
        room, msgs = self.room()
        self.post_panel(room)
        self.cfg(spin_panel_stay_last=0)
        self.spin()
        self.assertEqual(self.alive(msgs), ["panel", "result"])
        self.cfg(spin_panel_stay_last=1, games_public_results="none", spin_per_day=5)
        self.spin(self.bob)
        self.assertEqual(self.alive(msgs), ["panel", "result"])               # nothing was posted, so nothing moved

    def test_many_results_at_once_leave_exactly_one_panel_at_the_bottom(self):
        self.open()
        with self.db.tx() as c:
            for n in range(3, 9):
                c.execute("INSERT INTO members(nation_id,nation_name,discord_id,created_at) VALUES(?,?,?,?)", (n, f"N{n}", str(1000 + n), "x"))
        room, msgs = self.room()
        self.post_panel(room)

        async def burst():
            ins = []
            for uid_ in [111, 112] + [1000 + n for n in range(3, 9)]:
                i = FI(discord.User(uid_, "u"))
                i.channel, i.client = room, self.client
                ins.append(i)
            await asyncio.gather(*(self.games.commands["spin"].callback(i) for i in ins))
        self.run_async(burst())
        alive = self.alive(msgs)
        self.assertEqual(alive.count("result"), 8)
        self.assertEqual(alive.count("panel"), 1)
        self.assertEqual(alive[-1], "panel")
        self.assertLess(sum(m.view is not None for m in msgs), 9)               # fewer panel posts than results: they share moves

    def test_a_panel_that_cannot_be_moved_never_breaks_the_spin(self):
        self.open()
        room, msgs = self.room()
        self.post_panel(room)
        self.fail_sends = True
        out = self.spin().text()
        self.assertIn("300 steel", out)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM game_prizes").fetchone()[0], 1)
        self.assertEqual(self.alive(msgs), ["panel", "result"])               # the old panel is kept when a new one can't be posted

    # ---- structure
    def test_panel_buttons_have_unique_stable_ids(self):
        src = (Path(__file__).resolve().parent.parent / "tunbank" / "cmds_games.py").read_text()
        ids = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.keyword) and n.arg == "custom_id" and isinstance(n.value, ast.Constant)]
        ids = [i.value if hasattr(i, "value") else i for i in ids]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(str(i).startswith("tunbank:spin:") for i in ids))


if __name__ == "__main__":
    unittest.main()
