"""
Unit tests for the pure decision logic (no network, no FastAPI needed):

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import defense_rankings  # noqa: E402
import lineup  # noqa: E402
import projections  # noqa: E402
import sleeper_client as sleeper  # noqa: E402
import nfl_schedule  # noqa: E402
import valuation  # noqa: E402

ELIG = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"},
    "FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "DEF": {"DEF"},
}


def P(name, pos, exp, slot=None, **kw):
    d = {"id": name, "name": name, "position": pos, "expected_points": exp,
         "bad_injury": False, "on_bye": False, "injury_status": None}
    if slot:
        d["slot"] = slot
    d.update(kw)
    return d


class DefenseRankingMath(unittest.TestCase):
    def test_counts_games_not_player_lines(self):
        """Team A's defense faced TWO 10-pt WRs in its one game (20 allowed);
        Team B faced ONE 10-pt WR. Per-player averaging called them equal."""
        players = {
            "w1": {"position": "WR", "team": "X"}, "w2": {"position": "WR", "team": "X"},
            "w3": {"position": "WR", "team": "Y"},
        }
        # X's two WRs play A; Y's one WR plays B.
        schedule = {"X": "A", "A": "X", "Y": "B", "B": "Y"}
        stats = {"w1": {"rec_yd": 100}, "w2": {"rec_yd": 100}, "w3": {"rec_yd": 100}}
        scoring_settings = {"rec_yd": 0.1}

        orig = (sleeper.get_players_cached, sleeper.get_week_stats, nfl_schedule.get_week_schedule)
        sleeper.get_players_cached = lambda: players
        sleeper.get_week_stats = lambda season, week, completed=True: stats
        nfl_schedule.get_week_schedule = lambda season, week: schedule
        defense_rankings._cache.clear()
        try:
            ranks = defense_rankings.get_defense_rankings("2026", [1], scoring_settings)
        finally:
            sleeper.get_players_cached, sleeper.get_week_stats, nfl_schedule.get_week_schedule = orig
        self.assertEqual(ranks["A"]["WR"]["avg_allowed"], 20.0)
        self.assertEqual(ranks["B"]["WR"]["avg_allowed"], 10.0)
        self.assertLess(ranks["B"]["WR"]["rank"], ranks["A"]["WR"]["rank"])  # B is tougher


class StartSit(unittest.TestCase):
    def test_best_swap_goes_to_the_slot_it_helps_most(self):
        # RB3 (14) can replace RB2 (+8) or the WR in FLEX (+9). The FLEX swap
        # is globally better: 12 + 6 + 14 = 32 vs 12 + 14 + 5 = 31.
        starters = [P("RB1", "RB", 12, "RB"), P("RB2", "RB", 6, "RB"), P("WR1", "WR", 5, "FLEX")]
        swaps = lineup.optimize_swaps(starters, [P("RB3", "RB", 14)], ELIG)
        self.assertEqual([(s["start"], s["sit"]) for s in swaps], [("RB3", "WR1")])
        self.assertEqual(swaps[0]["slot"], "FLEX")
        self.assertEqual(swaps[0]["gain"], 9.0)

    def test_two_bench_players_fill_two_slots(self):
        starters = [P("RB1", "RB", 5, "RB"), P("WR1", "WR", 1, "FLEX")]
        swaps = lineup.optimize_swaps(starters, [P("RB3", "RB", 10), P("RB4", "RB", 7)], ELIG)
        self.assertEqual({(s["start"], s["sit"]) for s in swaps}, {("RB3", "WR1"), ("RB4", "RB1")})

    def test_swaps_never_lower_the_lineup_or_start_an_injured_player(self):
        import random
        rng = random.Random(7)
        for _ in range(200):
            starters = [P(f"S{i}", pos, rng.choice([None, *range(0, 20)]), slot,
                          bad_injury=rng.random() < .15, injury_status="Out" if rng.random() < .15 else None)
                        for i, (pos, slot) in enumerate([("QB", "QB"), ("RB", "RB"), ("WR", "WR"), ("RB", "FLEX")])]
            for s in starters:
                s["bad_injury"] = s["injury_status"] == "Out"
            bench = [P(f"B{i}", rng.choice(["QB", "RB", "WR", "TE"]), rng.choice([None, *range(0, 20)]),
                       bad_injury=(i % 3 == 0), injury_status="Out" if i % 3 == 0 else None) for i in range(5)]
            swaps = lineup.optimize_swaps(starters, bench, ELIG)
            injured_bench = {b["name"] for b in bench if b["bad_injury"]}
            for sw in swaps:
                self.assertNotIn(sw["start"], injured_bench)

    def test_out_starter_replaced_by_anyone_healthy(self):
        starters = [P("QB1", "QB", 20, "QB", bad_injury=True, injury_status="Out")]
        bench = [P("QB2", "QB", None)]  # no projection at all
        swaps = lineup.optimize_swaps(starters, bench, ELIG)
        self.assertEqual(swaps[0]["start"], "QB2")
        self.assertIn("Out", swaps[0]["reason"])

    def test_unreplaceable_must_sit_is_still_reported(self):
        starters = [P("TE1", "TE", 9, "TE", on_bye=True)]
        swaps = lineup.optimize_swaps(starters, [P("WR9", "WR", 8)], ELIG)
        self.assertIsNone(swaps[0]["start"])
        self.assertIn("bye", swaps[0]["reason"])

    def test_questionable_is_discounted_not_benched(self):
        starters = [P("WR1", "WR", 10, "WR", injury_status="Questionable")]  # 9.0 effective
        bench = [P("WR2", "WR", 9.5)]  # +0.5 -> no swap
        self.assertEqual(lineup.optimize_swaps(starters, bench, ELIG), [])
        bench = [P("WR2", "WR", 11)]  # 11 vs 9.0 = +2.0 -> swap
        self.assertEqual(lineup.optimize_swaps(starters, bench, ELIG)[0]["start"], "WR2")

    def test_injured_bench_player_never_recommended(self):
        starters = [P("WR1", "WR", 5, "WR")]
        bench = [P("WR2", "WR", 20, bad_injury=True, injury_status="Out")]
        self.assertEqual(lineup.optimize_swaps(starters, bench, ELIG), [])

    def test_unknown_starter_not_swapped_on_a_guess(self):
        self.assertEqual(lineup.optimize_swaps([P("R", "WR", None, "WR")], [P("B", "WR", 9)], ELIG), [])


class Projections(unittest.TestCase):
    def test_blend_and_fallbacks(self):
        self.assertEqual(projections.blend_expected(10, 6)[1], "blend")
        self.assertEqual(projections.blend_expected(10, None), (10.0, "projection"))
        self.assertEqual(projections.blend_expected(None, 10, "Tough matchup"), (9.3, "recent"))
        self.assertEqual(projections.blend_expected(None, None), (None, None))

    def test_effective_points(self):
        self.assertEqual(projections.effective_points({"expected_points": 10, "bad_injury": True}), 0.0)
        self.assertEqual(projections.effective_points({"expected_points": 10, "injury_status": "Questionable"}), 9.0)
        self.assertIsNone(projections.effective_points({}))


class HoldRules(unittest.TestCase):
    def test_young_and_injured_is_held(self):
        r = valuation.hold_reason({"age": 22, "injury_status": "Out", "position": "WR"}, dynasty=False, floor=None)
        self.assertIn("hold", r.lower())

    def test_old_and_injured_is_not_automatically_held(self):
        self.assertIsNone(valuation.hold_reason({"age": 30, "injury_status": "Out"}, dynasty=False, floor=None))

    def test_dynasty_market_value_is_held(self):
        r = valuation.hold_reason({"age": 28, "fantasycalc_value": 2500}, dynasty=True, floor=2000)
        self.assertEqual(r, "Dynasty value - hold")
        self.assertIsNone(valuation.hold_reason({"age": 28, "fantasycalc_value": 2500}, dynasty=False, floor=None))

    def test_young_rookie_upside(self):
        r = valuation.hold_reason({"age": 22, "years_exp": 0, "fantasycalc_value": 10}, dynasty=True, floor=2000)
        self.assertEqual(r, "Young upside - hold")

    def test_dynasty_inference(self):
        self.assertFalse(valuation.is_dynasty_league({"name": "x", "settings": {"type": 0}}))
        self.assertTrue(valuation.is_dynasty_league({"name": "x", "settings": {"type": 1}}))
        self.assertTrue(valuation.is_dynasty_league({"name": "x", "settings": {"type": 2}}))

    def test_keep_score_prefers_young_valuable(self):
        young = {"position": "WR", "age": 22, "fantasycalc_value": 3000, "expected_points": 3}
        old = {"position": "WR", "age": 31, "fantasycalc_value": 3000, "expected_points": 3}
        self.assertGreater(valuation.keep_score(young, True), valuation.keep_score(old, True))


class WaiverPlan(unittest.TestCase):
    def drops(self):
        return [
            P("DropWR", "WR", 2.0, id="d1", keep_score=1.0),
            P("DropQB", "QB", 3.0, id="d2", keep_score=2.0),
            P("KeeperRB", "RB", 1.0, id="d3", keep_score=0.5, keeper_eligible=True),
        ]

    def test_pairs_each_add_with_a_drop_in_claim_order(self):
        adds = [P("AddWR", "WR", 9.0, id="a1", fantasycalc_value=100), P("AddRB", "RB", 8.0, id="a2")]
        rows = lineup.build_waiver_plan(adds, self.drops(), {"WR": 5, "QB": 2, "RB": 4}, {}, dynasty=False)
        self.assertEqual([r["claim_order"] for r in rows], [1, 2])
        self.assertTrue(all(r["add"] and r["drop"] for r in rows))
        self.assertEqual(len({r["drop"]["id"] for r in rows}), len(rows))  # each drop used once

    def test_keeper_eligible_is_last_resort_big_gains_only_and_flagged(self):
        adds = [P("AddRB", "RB", 9.0, id="a1"), P("AddWR", "WR", 9.5, id="a2"), P("AddQB", "QB", 9.0, id="a3")]
        rows = lineup.build_waiver_plan(adds, self.drops(), {}, {}, dynasty=False)
        flagged = [r for r in rows if r["drop"]["keeper_eligible"]]
        self.assertEqual(len(flagged), 1)  # only after the other two drops are spent
        self.assertGreaterEqual(flagged[0]["gain"], lineup.KEEPER_BURN_MIN_GAIN)
        self.assertIn("keeper eligibility", flagged[0]["warning"])
        # a small upgrade is NOT worth burning eligibility
        small = [P("Meh", "RB", 3.0, id="a9")]
        only_keeper = [d for d in self.drops() if d["id"] == "d3"]
        self.assertEqual(lineup.build_waiver_plan(small, only_keeper, {}, {}, dynasty=False), [])

    def test_position_cap_forces_same_position_drop(self):
        adds = [P("AddQB", "QB", 15.0, id="a1")]
        rows = lineup.build_waiver_plan(adds, self.drops(), {"QB": 3}, {"QB": 3}, dynasty=True)
        self.assertEqual(rows[0]["drop"]["position"], "QB")
        self.assertIn("cap", rows[0]["warning"])

    def test_same_position_drop_preferred(self):
        drops = [P("ScrubWR", "WR", 2.0, id="d1", keep_score=0.1), P("ScrubQB", "QB", 2.0, id="d2", keep_score=5.0)]
        rows = lineup.build_waiver_plan([P("AddQB", "QB", 12.0, id="a1")], drops, {}, {}, dynasty=False)
        self.assertEqual(rows[0]["drop"]["id"], "d2")  # QB-for-QB even though the WR is "cheaper"

    def test_marginal_add_is_skipped(self):
        adds = [P("AddWR", "WR", 2.5, id="a1")]  # +0.5 over the drop
        self.assertEqual(lineup.build_waiver_plan(adds, self.drops(), {}, {}, dynasty=False), [])

    def test_injured_and_bye_adds_are_skipped(self):
        adds = [P("Hurt", "WR", 20, id="a1", injury_status="Out"), P("Bye", "WR", 20, id="a2", on_bye=True)]
        self.assertEqual(lineup.build_waiver_plan(adds, self.drops(), {}, {}, dynasty=False), [])

    def test_dynasty_long_term_add_qualifies_without_point_gain(self):
        adds = [P("Stash", "WR", 2.0, id="a1", fantasycalc_value=3000)]
        drops = [P("Vet", "WR", 2.0, id="d1", keep_score=1.0, fantasycalc_value=500)]
        rows = lineup.build_waiver_plan(adds, drops, {}, {}, dynasty=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["long_term"])


class FallbackPlan(unittest.TestCase):
    def held(self):
        return [
            P("OldVet", "TE", 4.0, id="h1", verdict="Dynasty value - hold", age=37, keep_score=3.0, fantasycalc_value=1600),
            P("OlderQB", "QB", 0.0, id="h2", verdict="Dynasty value - hold", age=31, keep_score=5.0, fantasycalc_value=2500),
            P("YoungHurt", "WR", 0.0, id="h3", verdict="Young (22) & Questionable - hold", age=22, keep_score=0.1),
            P("YoungValue", "RB", 5.0, id="h4", verdict="Dynasty value - hold", age=24, keep_score=0.2),
            P("Rising", "WR", 3.0, id="h5", verdict="Hold - role growing", age=27, keep_score=0.3),
            P("NoAge", "WR", 3.0, id="h6", verdict="Dynasty value - hold", age=None, keep_score=0.4),
            P("Cut", "WR", 1.0, id="h7", verdict="Drop candidate", age=28, keep_score=0.1),
        ]

    def test_pool_only_offers_old_market_value_holds_least_valuable_first(self):
        pool = lineup.fallback_drop_pool(self.held())
        # young+injured, young value, role-growing, unknown age and real drop candidates are excluded
        self.assertEqual([d["id"] for d in pool], ["h1", "h2"])

    def test_fallback_rows_are_flagged_and_use_only_the_pool(self):
        adds = [P("AddTE", "TE", 11.0, id="a1")]
        rows = lineup.build_fallback_plan(adds, self.held(), {}, {}, dynasty=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["fallback"])
        self.assertEqual(rows[0]["drop"]["id"], "h1")  # same position, never the young hurt WR
        self.assertIn("held for market value", rows[0]["warning"])
        self.assertIn("trading", rows[0]["warning"])

    def test_fallback_never_reuses_safe_plan_picks(self):
        adds = [P("AddTE", "TE", 11.0, id="a1"), P("AddWR", "WR", 11.0, id="a2")]
        rows = lineup.build_fallback_plan(
            adds, self.held(), {}, {}, dynasty=True,
            exclude_add_ids={"a1"}, exclude_drop_ids={"h2"},
        )
        self.assertTrue(all(r["add"]["id"] != "a1" for r in rows))
        self.assertTrue(all(r["drop"]["id"] != "h2" for r in rows))

    def test_open_slot_add_jumps_the_queue_and_skips_the_gain_bar(self):
        swaps = [{"slot": "DEF", "sit": "BYE DEF", "start": None, "gain": None, "reason": "bye"},
                 {"slot": "FLEX", "sit": "X", "start": None, "gain": None, "reason": "x"}]
        self.assertEqual(lineup.open_slot_positions(swaps), {"DEF"})  # FLEX is not a position
        adds = [P("BigWR", "WR", 14.0, id="a1"), P("StreamDEF", "DEF", 5.0, id="a2")]
        drops = [P("Scrub", "WR", 4.5, id="d1", keep_score=0.1)]  # DEF add is only +0.5
        rows = lineup.build_waiver_plan(adds, drops, {}, {}, dynasty=False, needed_positions={"DEF"})
        self.assertEqual(rows[0]["add"]["name"], "StreamDEF")
        self.assertTrue(rows[0]["fills_slot"])
        self.assertIn("empty DEF slot", rows[0]["warning"])


class SlotFixes(unittest.TestCase):
    def roster(self):
        # What main hands over: no healthy starters, stashes or protected players.
        return [
            P("ByeDEF", "DEF", 7.0, id="r1", slot="DEF", on_bye=True, keep_score=3.5),
            P("BenchWR", "WR", 4.0, id="r2", keep_score=1.0),
            P("OldTE", "TE", 3.0, id="r3", keep_score=6.0, hold=True, fantasycalc_value=1600),
        ]

    def adds(self):
        return [P("StreamDEF", "DEF", 9.0, id="a1"), P("HurtDEF", "DEF", 12.0, id="a2", injury_status="Out"),
                P("FreeWR", "WR", 10.0, id="a3")]

    def test_bye_starter_at_the_same_position_is_the_drop(self):
        rows = lineup.build_slot_fixes(self.adds(), self.roster(), {"DEF"}, roster_full=True, dynasty=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["add"]["id"], "a1")  # never an Out add
        self.assertEqual(rows[0]["drop"]["id"], "r1")  # same position beats a cheaper WR
        self.assertEqual(rows[0]["gain"], 9.0)  # a bye-week player is worth 0 this week
        self.assertTrue(rows[0]["fills_slot"])
        self.assertEqual(rows[0]["claim_order"], 1)

    def test_otherwise_the_least_valuable_player(self):
        roster = [p for p in self.roster() if p["id"] != "r1"]
        rows = lineup.build_slot_fixes(self.adds(), roster, {"DEF"}, roster_full=True, dynasty=True)
        self.assertEqual(rows[0]["drop"]["id"], "r2")

    def test_open_roster_spot_needs_no_drop(self):
        rows = lineup.build_slot_fixes(self.adds(), self.roster(), {"DEF"}, roster_full=False, dynasty=True)
        self.assertIsNone(rows[0]["drop"])
        self.assertIn("no drop is needed", rows[0]["warning"])

    def test_keeper_eligible_last_and_market_holds_flagged(self):
        roster = [P("Keeper", "WR", 1.0, id="k1", keep_score=0.1, keeper_eligible=True),
                  P("OldTE", "TE", 3.0, id="r3", keep_score=6.0, hold=True, fantasycalc_value=1600)]
        rows = lineup.build_slot_fixes(self.adds(), roster, {"DEF"}, roster_full=True, dynasty=True)
        self.assertEqual(rows[0]["drop"]["id"], "r3")
        self.assertIn("held for market value (FantasyCalc 1600)", rows[0]["warning"])
        rows = lineup.build_slot_fixes(self.adds(), roster[:1], {"DEF"}, roster_full=True, dynasty=True)
        self.assertIn("keeper eligibility", rows[0]["warning"])

    def test_skips_used_players_and_positions_with_no_add(self):
        rows = lineup.build_slot_fixes(self.adds(), self.roster(), {"DEF", "TE"}, roster_full=True, dynasty=True,
                                       exclude_drop_ids={"r1"})
        self.assertEqual([r["add"]["position"] for r in rows], ["DEF"])  # no TE free agent
        self.assertNotEqual(rows[0]["drop"]["id"], "r1")
        self.assertEqual(lineup.build_slot_fixes(self.adds(), self.roster(), {"DEF"}, True, True,
                                                 exclude_add_ids={"a1"}), [])
        self.assertEqual(lineup.build_slot_fixes(self.adds(), [], {"DEF"}, True, True), [])


class Trades(unittest.TestCase):
    BOARD = {str(i): {"value": 10000 - i * 50, "overall_rank": i + 1} for i in range(300)}

    def test_pick_values_float_with_board_and_decline_by_round(self):
        v1, v2, v4, v6 = (valuation.pick_value(r, self.BOARD) for r in (1, 2, 4, 6))
        self.assertGreater(v1, v2)
        self.assertGreater(v2, v4)
        self.assertGreater(v4, v6)

    def test_parse_pick(self):
        self.assertEqual(valuation.parse_pick("2027 R1"), (2027, 1))
        self.assertEqual(valuation.parse_pick("2027 2nd"), (2027, 2))
        self.assertEqual(valuation.parse_pick("round 3"), (None, 3))
        self.assertIsNone(valuation.parse_pick("Puka Nacua"))

    def test_two_for_one_favors_the_best_player(self):
        # Two 6000s ("sum 12000") for one 9000: raw sum says you win, consolidation says no.
        give = [{"label": "a", "value": 6000, "kind": "player", "team": "X"},
                {"label": "b", "value": 6000, "kind": "player", "team": "Y"}]
        get = [{"label": "star", "value": 9000, "kind": "player", "team": "Z"}]
        r = valuation.evaluate_trade(give, get, "Any League")
        raw_edge = (9000 - 12000) / 12000 * 100  # -25%: what a plain sum would say
        self.assertGreater(r["edge_pct"], raw_edge)   # depth is discounted, so the overpay shrinks
        self.assertLess(r["give_total"], 12000)
        self.assertEqual(r["verdict"], "You lose this trade")  # but two 6ks still don't buy a 9k

    def test_kc_tax_applies_to_what_you_receive_in_dickmelt(self):
        give = [{"label": "a", "value": 5000, "kind": "player", "team": "X"}]
        get_kc = [{"label": "Rice", "value": 5000, "kind": "player", "team": "KC"}]
        get_other = [{"label": "Other", "value": 5000, "kind": "player", "team": "DAL"}]
        kc = valuation.evaluate_trade(give, get_kc, "DickMelt Fantasies")
        other = valuation.evaluate_trade(give, get_other, "DickMelt Fantasies")
        self.assertEqual(kc["get"][0]["tax"], 1.15)
        # Same market value both ways - the tax is a price, so market edge is unchanged...
        self.assertEqual(kc["edge_pct"], other["edge_pct"])
        # ...but the Chiefs fan wants a premium, so an even swap for a KC player is short.
        self.assertFalse(kc["likely_accepted"])
        self.assertEqual(kc["get_price"], 5750)
        self.assertEqual(kc["ask_more"], 750)
        self.assertTrue(other["likely_accepted"])
        self.assertEqual(valuation.evaluate_trade(give, get_kc, "Chiefs By 90")["get"][0]["tax"], 1.0)

    def test_suggest_package_prefers_fewest_assets_least_overpay(self):
        assets = [{"label": "A", "value": 4000}, {"label": "B", "value": 3000}, {"label": "C", "value": 2500}]
        labels, total = valuation.suggest_package(3900, assets)
        self.assertEqual(labels, ["A"])
        labels, total = valuation.suggest_package(5500, assets)
        self.assertEqual(len(labels), 2)
        self.assertGreaterEqual(total, 5500)
        self.assertEqual(valuation.suggest_package(10**6, assets), (None, None))


class HttpCache(unittest.TestCase):
    def test_stats_download_is_cached(self):
        calls = []

        class Resp:
            def raise_for_status(self): pass
            def json(self): return {"ok": 1}

        class Sess:
            def get(self, url, timeout=None):
                calls.append(url)
                return Resp()

        orig = sleeper._session
        sleeper._session = Sess()
        sleeper._http_cache.clear()
        try:
            for _ in range(5):
                sleeper.get_week_stats("2026", 3)
            self.assertEqual(len(calls), 1)  # was one multi-MB download per call
            sleeper.get_week_stats("2026", 4)
            self.assertEqual(len(calls), 2)
        finally:
            sleeper._session = orig
            sleeper._http_cache.clear()

    def test_projection_shapes_are_normalized(self):
        as_list = [{"player_id": "1", "stats": {"rec": 5}}, {"player_id": "2", "stats": {"rec": 2}}, {"junk": True}]
        as_dict_wrapped = {"1": {"stats": {"rec": 5}}, "2": {"stats": {"rec": 2}}}
        as_dict_flat = {"1": {"rec": 5}, "2": {"rec": 2}}
        for raw in (as_list, as_dict_wrapped, as_dict_flat):
            self.assertEqual(sleeper._normalize_projections(raw), {"1": {"rec": 5}, "2": {"rec": 2}})
        self.assertEqual(sleeper._normalize_projections(None), {})
        self.assertEqual(sleeper._normalize_projections("nope"), {})

    def test_projection_failure_degrades_to_empty(self):
        class Sess:
            def get(self, url, timeout=None):
                raise RuntimeError("down")

        orig = sleeper._session
        sleeper._session = Sess()
        sleeper._http_cache.clear()
        try:
            self.assertEqual(sleeper.get_projections("2026", 6), {})
        finally:
            sleeper._session = orig


if __name__ == "__main__":
    unittest.main()
