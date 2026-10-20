"""
End-to-end check of every API route against a fake 10-team league
(no network). Calls the route functions directly, so it needs the
`fastapi` package that the app itself needs (skipped otherwise):

    python -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

try:
    import main  # noqa: E402
    HAVE_APP = True
except Exception as exc:  # fastapi missing
    HAVE_APP = False
    IMPORT_ERROR = exc

if HAVE_APP:
    import fantasycalc_client
    import keeper_eligibility
    import nfl_schedule
    import sleeper_client as sleeper

TEAMS = ["KC", "SF", "DAL", "PHI", "BUF", "MIA", "DET", "BAL", "CIN", "LAR", "SEA", "GB"]
LEAGUE_ID = main.LEAGUES[0]["id"] if HAVE_APP else "L"
OTHER_ID = main.LEAGUES[1]["id"] if HAVE_APP else "L2"


def build_world():
    rng = random.Random(42)
    players, stats = {}, {w: {} for w in range(1, 6)}
    pid = 0

    def add(pos, team, name, age, yrs, inj=None):
        nonlocal pid
        pid += 1
        key = str(pid)
        players[key] = {"position": pos, "team": team, "full_name": name, "age": age, "years_exp": yrs,
                        "injury_status": inj, "active": True, "status": "Active"}
        base = {"QB": 18, "RB": 11, "WR": 10, "TE": 7}.get(pos, 7)
        for w in range(1, 6):
            if pos == "DEF":
                stats[w][key] = {"def_td": rng.choice([0, 0, 1]), "sack": rng.randint(0, 6), "pts_allow_14_20": 1}
            else:
                scale = rng.uniform(0.3, 1.6)
                stats[w][key] = {
                    "pass_yd": int(base * 15 * scale) if pos == "QB" else 0,
                    "rush_yd": int(base * 6 * scale) if pos == "RB" else 0,
                    "rec": rng.randint(0, 8) if pos in ("WR", "TE", "RB") else 0,
                    "rec_yd": int(base * 6 * scale) if pos in ("WR", "TE") else 0,
                    "rec_tgt": rng.randint(0, 11) if pos in ("WR", "TE", "RB") else 0,
                    "rush_att": rng.randint(3, 20) if pos == "RB" else 0,
                }
        return key

    by_team = {}
    for t in TEAMS:
        ids = [add("QB", t, f"{t} Quarterback", 27, 5), add("RB", t, f"{t} Runner One", 24, 3),
               add("RB", t, f"{t} Runner Two", 28, 7), add("WR", t, f"{t} Receiver One", 25, 4),
               add("WR", t, f"{t} Receiver Two", 23, 2), add("WR", t, f"{t} Receiver Three", 30, 9),
               add("TE", t, f"{t} Tight End", 26, 4), add("DEF", t, f"{t} DEF", None, 0)]
        by_team[t] = ids
    return players, stats, by_team


class EndpointSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE_APP:
            raise unittest.SkipTest(f"app not importable here: {IMPORT_ERROR}")
        cls.players, cls.stats, by_team = build_world()
        rosters = []
        # 10 rosters: roster r takes its team's 8 players + 2 of the next team's
        for r in range(10):
            mine = list(by_team[TEAMS[r]]) + by_team[TEAMS[(r + 1) % 12]][:2]
            rosters.append({"roster_id": r + 1, "owner_id": f"u{r + 1}", "players": mine,
                            "starters": [mine[0], mine[1], mine[2], mine[3], mine[4], mine[6], mine[5], mine[8], mine[7]]})
        # Give my bench three scrubs with no production and no market value, so there
        # are real drop candidates to pair with free agents.
        scrubs = [by_team["GB"][2], by_team["GB"][5], by_team["GB"][6]]
        rosters[0]["players"] = rosters[0]["players"] + scrubs
        for sid in scrubs:
            for week_stats in cls.stats.values():
                week_stats.pop(sid, None)
        cls.scrubs = set(scrubs)
        cls.rosters = rosters
        # Make my lineup interesting: starter RB2 is Out, bench has a young injured WR.
        cls.players[rosters[0]["players"][2]]["injury_status"] = "Out"
        young = rosters[0]["players"][9]
        cls.young_hurt = young
        cls.players[young].update({"age": 22, "years_exp": 1, "injury_status": "Questionable", "position": "WR"})

        league = lambda name, typ: {
            "name": name, "season": "2026", "settings": {"type": typ},
            "scoring_settings": {"pass_yd": 0.04, "pass_td": 4, "rush_yd": 0.1, "rec": 0.5, "rec_yd": 0.1, "def_td": 6, "sack": 1},
            "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "DEF", "BN", "BN", "BN", "BN"],
        }
        cls.leagues = {LEAGUE_ID: league("DickMelt Fantasies", 1), OTHER_ID: league("Chiefs By 90", 2)}

        fc = {}
        for i, (pid, p) in enumerate(sorted(cls.players.items(), key=lambda kv: int(kv[0]))):
            if p["position"] != "DEF" and pid not in cls.scrubs:
                fc[pid] = {"value": max(100, 9000 - i * 60), "overall_rank": i + 1}
        cls.fc_calls = []

        cls.orig = {}
        def patch(mod, name, fn):
            cls.orig[(mod, name)] = getattr(mod, name)
            setattr(mod, name, fn)

        patch(sleeper, "get_nfl_state", lambda: {"season": "2026", "week": 6})
        patch(sleeper, "get_user_id", lambda u: "u1")
        patch(sleeper, "get_league", lambda lid: cls.leagues[lid])
        patch(sleeper, "get_rosters", lambda lid: cls.rosters)
        patch(sleeper, "get_league_users", lambda lid: [
            {"user_id": f"u{i}", "display_name": f"owner{i}", "metadata": {"team_name": f"Team {i}"}} for i in range(1, 11)])
        patch(sleeper, "get_players_cached", lambda: cls.players)
        patch(sleeper, "get_week_stats", lambda season, week, completed=True: cls.stats.get(week, {}))
        patch(sleeper, "get_trending_adds", lambda lookback_hours=24, limit=50: [
            {"player_id": by_team["MIA"][3], "count": 900}])
        patch(sleeper, "get_projections", lambda season, week: {
            pid: {"rec_yd": 70, "rec": 6} for pid in by_team["SEA"][3:5]})
        patch(fantasycalc_client, "get_values_by_sleeper_id",
              lambda num_qbs, num_teams, ppr, is_dynasty=False: (cls.fc_calls.append(is_dynasty) or fc))
        sched = {}
        for a, b in zip(TEAMS[0::2], TEAMS[1::2]):
            sched[a], sched[b] = b, a
        patch(nfl_schedule, "get_week_schedule", lambda season, week: sched)
        patch(keeper_eligibility, "get_keeper_eligible_ids", lambda lid, ids: set(list(ids)[:1]))
        main._memo_data.clear()

    @classmethod
    def tearDownClass(cls):
        for (mod, name), fn in cls.orig.items():
            setattr(mod, name, fn)

    def test_leagues_report_dynasty_flag(self):
        out = main.list_leagues()
        self.assertEqual([l["dynasty"] for l in out], [True, True])

    def test_fantasycalc_gets_dynasty_for_keeper_and_dynasty_leagues(self):
        self.fc_calls.clear()
        main._memo_data.clear()
        main.get_recommendations(LEAGUE_ID)
        main.get_recommendations(OTHER_ID)
        self.assertTrue(self.fc_calls and all(self.fc_calls))  # no longer hardcoded redraft

    def test_recommendations_shape_and_start_sit(self):
        r = main.get_recommendations(LEAGUE_ID)
        for key in ("swap_suggestions", "top_waivers", "waiver_plan", "dynasty"):
            self.assertIn(key, r)
        out_starter = self.players[self.rosters[0]["players"][2]]["full_name"]
        sit = [s for s in r["swap_suggestions"] if s["sit"] == out_starter]
        self.assertTrue(sit, "the Out starter must be flagged")
        self.assertTrue(r["waiver_plan"], "scrubs on the bench should be swapped for real free agents")
        self.assertEqual([row["claim_order"] for row in r["waiver_plan"]], list(range(1, len(r["waiver_plan"]) + 1)))
        for row in r["waiver_plan"]:
            self.assertTrue(row["add"] and row["drop"] and row["claim_order"])
            self.assertNotEqual(row["drop"]["id"], self.young_hurt)  # young+injured is never the drop
            self.assertIn(row["drop"]["id"], self.scrubs)
            self.assertGreaterEqual(row["gain"], 1.0)
        self.assertLessEqual(len(r["top_waivers"]), 10)
        # free agents come from the whole pool, not only the trending list
        self.assertGreater(len({w["id"] for w in r["top_waivers"]}), 1)

    def test_drop_candidates_hold_young_injured(self):
        d = main.get_drop_candidates(LEAGUE_ID)
        by_id = {c["id"]: c for c in d["candidates"]}
        if self.young_hurt in by_id:
            self.assertIn("hold", by_id[self.young_hurt]["verdict"].lower())
        for c in d["candidates"]:
            for key in ("id", "age", "keep_score", "fantasycalc_value", "verdict", "keeper_eligible"):
                self.assertIn(key, c)

    def test_team_needs_ranked_and_starter_level(self):
        n = main.get_team_needs(LEAGUE_ID)["needs"]
        self.assertTrue(n)
        for row in n:
            self.assertTrue(1 <= row["rank"] <= row["out_of"])
            self.assertIn(row["label"], ("Strength", "Weakness", "Average"))

    def test_trade_eval_by_name_and_pick(self):
        r = main.get_trade_eval(LEAGUE_ID, give="SF Receiver One,2027 R1", get="KC Receiver One")
        self.assertEqual(len(r["give"]), 2)
        self.assertEqual(r["get"][0]["tax"], 1.15)  # KC tax in DickMelt
        self.assertTrue(any("all in" in w for w in r["warnings"]))  # R1 is protected
        self.assertTrue(any("tax" in w for w in r["warnings"]))
        bad = main.get_trade_eval(LEAGUE_ID, give="Nobody Real", get="")
        self.assertTrue(any("Couldn't find" in w for w in bad["warnings"]))
        # no tax in the other league
        r2 = main.get_trade_eval(OTHER_ID, give="SF Receiver One", get="KC Receiver One")
        self.assertEqual(r2["get"][0]["tax"], 1.0)

    def test_trade_targets(self):
        t = main.get_trade_targets(LEAGUE_ID)
        self.assertTrue(t["weak_positions"])
        for row in t["targets"]:
            self.assertIn(row["position"], t["weak_positions"])
            self.assertNotIn(row["id"], self.rosters[0]["players"])  # never one of my own
            self.assertNotEqual(row["owner"], "Team 1")

    def test_nfl_matchups_and_lineup_still_work(self):
        m = main.get_weekly_matchups(LEAGUE_ID)
        self.assertEqual(m["week"], 6)
        self.assertTrue(m["players"])
        self.assertTrue(main.get_lineup(LEAGUE_ID)["starters"])

    def test_recommendations_include_fallback_fields(self):
        rec = main.get_recommendations(LEAGUE_ID)
        self.assertIn("fallback_plan", rec)
        self.assertIn("open_slots", rec)
        safe_drops = {r["drop"]["id"] for r in rec["waiver_plan"]}
        for row in rec["fallback_plan"]:
            self.assertTrue(row["fallback"])
            self.assertNotIn(row["drop"]["id"], safe_drops)
            self.assertNotEqual(row["drop"]["id"], self.young_hurt)  # young injured is never offered

    def test_frontend_dir_is_never_the_working_directory(self):
        # Regression: Path("") is ".", so an unset FRONTEND_DIR once made the
        # app serve its own source folder (no home page, and /main.py exposed).
        from pathlib import Path
        old_cwd, old_env = os.getcwd(), os.environ.pop("FRONTEND_DIR", None)
        try:
            os.chdir(Path(main.__file__).resolve().parent)  # like WORKDIR /app
            for value in (None, "", "   "):
                if value is None:
                    os.environ.pop("FRONTEND_DIR", None)
                else:
                    os.environ["FRONTEND_DIR"] = value
                d = main._find_frontend_dir()
                self.assertTrue((d / "index.html").is_file())
                self.assertFalse((d / "main.py").exists())
        finally:
            os.chdir(old_cwd)
            if old_env is not None:
                os.environ["FRONTEND_DIR"] = old_env
            else:
                os.environ.pop("FRONTEND_DIR", None)

    def test_unknown_league_404s(self):
        from fastapi import HTTPException
        for fn in (main.get_recommendations, main.get_trade_targets, main.get_team_needs):
            with self.assertRaises(HTTPException):
                fn("nope")


if __name__ == "__main__":
    unittest.main()
