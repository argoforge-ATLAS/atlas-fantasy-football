"""
Atlas Fantasy Football - Lineup HQ backend.

Serves:
  - GET /api/leagues                        -> basic info for both leagues
  - GET /api/leagues/{league_id}/lineup     -> your roster, starters vs bench
  - GET /api/leagues/{league_id}/matchup    -> this week: you vs your opponent
  - GET /api/leagues/{league_id}/recommendations -> start/sit + waiver suggestions,
        computed in Python from real recent performance under THIS league's
        actual scoring rules. No AI call, no external chat step.
  - /                                        -> the frontend (static files)
"""
import json

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import sleeper_client as sleeper
import scoring
import fantasycalc_client as fantasycalc
from config import LEAGUES, SLEEPER_USERNAME

app = FastAPI(title="Atlas Fantasy Football - Lineup HQ")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_league_lookup = {l["id"]: l["label"] for l in LEAGUES}


def _find_my_roster(league_id: str):
    my_user_id = sleeper.get_user_id(SLEEPER_USERNAME)
    rosters = sleeper.get_rosters(league_id)
    users = sleeper.get_league_users(league_id)
    users_by_id = {u["user_id"]: u for u in users}

    my_roster = next((r for r in rosters if r["owner_id"] == my_user_id), None)
    if my_roster is None:
        raise HTTPException(404, f"Could not find your roster in league {league_id}")

    return my_roster, rosters, users_by_id


def _player_name(players: dict, player_id: str) -> str:
    p = players.get(player_id)
    if not p:
        return player_id
    return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


def _player_brief(players: dict, player_id: str) -> dict:
    p = players.get(player_id, {})
    return {
        "name": _player_name(players, player_id),
        "position": p.get("position"),
        "team": p.get("team"),
        "injury_status": p.get("injury_status"),
        "depth_chart_order": p.get("depth_chart_order"),
    }


@app.get("/api/leagues")
def list_leagues():
    out = []
    for league in LEAGUES:
        info = sleeper.get_league(league["id"])
        out.append({
            "id": league["id"],
            "label": league["label"],
            "name": info.get("name"),
            "season": info.get("season"),
        })
    return out


@app.get("/api/leagues/{league_id}/lineup")
def get_lineup(league_id: str):
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    my_roster, _, _ = _find_my_roster(league_id)
    players = sleeper.get_players_cached()

    starters = my_roster.get("starters") or []
    all_players = my_roster.get("players") or []
    bench = [p for p in all_players if p not in starters]

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "starters": [_player_brief(players, pid) for pid in starters],
        "bench": [_player_brief(players, pid) for pid in bench],
    }


@app.get("/api/leagues/{league_id}/matchup")
def get_matchup(league_id: str):
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    state = sleeper.get_nfl_state()
    week = state["week"]

    my_roster, all_rosters, users_by_id = _find_my_roster(league_id)
    matchups = sleeper.get_matchups(league_id, week)

    roster_id_to_owner = {r["roster_id"]: r["owner_id"] for r in all_rosters}

    my_matchup = next((m for m in matchups if m["roster_id"] == my_roster["roster_id"]), None)
    if my_matchup is None:
        return {"league_id": league_id, "label": _league_lookup[league_id], "week": week, "message": "No matchup found (bye week or season not started)"}

    opponent = next(
        (m for m in matchups
         if m["matchup_id"] == my_matchup["matchup_id"] and m["roster_id"] != my_roster["roster_id"]),
        None,
    )

    def team_name(roster_id):
        owner_id = roster_id_to_owner.get(roster_id)
        user = users_by_id.get(owner_id, {})
        return user.get("metadata", {}).get("team_name") or user.get("display_name") or "Unknown"

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "week": week,
        "me": {"team_name": team_name(my_roster["roster_id"]), "points": my_matchup.get("points", 0)},
        "opponent": {
            "team_name": team_name(opponent["roster_id"]) if opponent else "TBD",
            "points": opponent.get("points", 0) if opponent else 0,
        },
    }


# Which real positions can fill each roster slot type. Standard
# Sleeper convention - tweak here if your league's flex rules differ.
SLOT_ELIGIBILITY = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "DEF": {"DEF"},
}

BAD_STATUSES = {"Out", "Doubtful", "IR", "PUP", "Suspended"}


def _build_player_pool(league_id: str):
    """Everything we know about every player on your roster, enriched
    with recent average fantasy points scored under THIS league's own
    scoring rules."""
    league_info = sleeper.get_league(league_id)
    scoring_settings = league_info.get("scoring_settings") or {}
    state = sleeper.get_nfl_state()
    season, current_week = state["season"], state["week"]

    my_roster, all_rosters, _ = _find_my_roster(league_id)
    players = sleeper.get_players_cached()

    starter_ids = my_roster.get("starters") or []
    all_ids = my_roster.get("players") or []
    bench_ids = [pid for pid in all_ids if pid not in starter_ids]

    # A second, independent source: FantasyCalc's own player valuations.
    # Parameters mirror this league's actual settings so the values are
    # relevant (superflex vs. single-QB, PPR level, league size).
    roster_positions_raw = league_info.get("roster_positions") or []
    num_qbs = roster_positions_raw.count("QB") + roster_positions_raw.count("SUPER_FLEX")
    ppr = scoring_settings.get("rec", 0)
    num_teams = len(all_rosters) or 10
    fc_values = fantasycalc.get_values_by_sleeper_id(
        num_qbs=max(num_qbs, 1), num_teams=num_teams, ppr=ppr, is_dynasty=False
    )

    def enrich(pid):
        brief = _player_brief(players, pid)
        brief["id"] = pid
        brief["recent_avg_points"] = scoring.recent_avg_points(pid, season, current_week, scoring_settings)
        fc = fc_values.get(pid)
        brief["fantasycalc_value"] = fc["value"] if fc else None
        return brief

    starters = [enrich(pid) for pid in starter_ids]
    bench = [enrich(pid) for pid in bench_ids]

    # Roster slot type for each starter, assuming Sleeper's convention
    # that `starters` is ordered the same as `roster_positions` with
    # the bench (BN) entries removed.
    slot_types = [p for p in (league_info.get("roster_positions") or []) if p != "BN"]
    for starter, slot in zip(starters, slot_types):
        starter["slot"] = slot

    rostered_ids = set()
    for r in all_rosters:
        rostered_ids.update(r.get("players") or [])

    # Only consider waiver candidates at positions your league actually
    # starts (e.g. no kickers if neither league has a K slot).
    valid_positions = set()
    for slot in slot_types:
        valid_positions |= SLOT_ELIGIBILITY.get(slot, {slot})

    trending = sleeper.get_trending_adds(lookback_hours=48, limit=150)
    waiver_candidates = []
    for t in trending:
        pid = t["player_id"]
        if pid in rostered_ids:
            continue
        brief = enrich(pid)
        if brief.get("position") not in valid_positions:
            continue
        # depth_chart_order of 3+ means deep backup, unlikely to play -
        # still shown, but the frontend can de-emphasize these.
        brief["add_count_48h"] = t.get("count")
        waiver_candidates.append(brief)
        if len(waiver_candidates) >= 20:
            break

    return league_info, starters, bench, waiver_candidates


@app.get("/api/leagues/{league_id}/recommendations")
def get_recommendations(league_id: str):
    """
    Start/sit and waiver suggestions, computed entirely in Python:
      - A starter with an Out/Doubtful/IR/etc. status is always
        flagged, regardless of points.
      - Otherwise, a bench player is suggested over a starter only if
        they're eligible for that roster slot AND their recent average
        (last 3 completed weeks, scored under this league's real
        rules) is clearly higher (>1.5 pt margin, to avoid noise).
      - Waiver candidates are ranked by recent performance when we
        have it, falling back to how many teams are adding them
        league-wide when we don't (e.g. a rookie who just won a job).
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    league_info, starters, bench, waiver_candidates = _build_player_pool(league_id)

    swap_suggestions = []
    used_bench_ids = set()

    # Handle must-sit (injured/etc.) starters first, then point-based upgrades.
    def is_bad(p):
        return p.get("injury_status") in BAD_STATUSES

    ordered_starters = sorted(starters, key=lambda s: (not is_bad(s),))

    for starter in ordered_starters:
        eligible_positions = SLOT_ELIGIBILITY.get(starter.get("slot"), {starter.get("position")})
        candidates = [
            b for b in bench
            if b["id"] not in used_bench_ids and b.get("position") in eligible_positions
        ]
        if not candidates:
            continue

        def sort_key(b):
            pts = b.get("recent_avg_points")
            return pts if pts is not None else -1

        best_bench = max(candidates, key=sort_key)
        starter_pts = starter.get("recent_avg_points")
        bench_pts = best_bench.get("recent_avg_points")

        reason = None
        if is_bad(starter):
            reason = f"{starter['name']} is {starter['injury_status']}"
        elif starter_pts is not None and bench_pts is not None and bench_pts - starter_pts > 1.5:
            reason = f"{best_bench['name']} has outscored {starter['name']} recently ({bench_pts} vs {starter_pts} pts/gm)"

        if reason:
            swap_suggestions.append({
                "slot": starter["slot"],
                "sit": starter["name"],
                "start": best_bench["name"],
                "reason": reason,
            })
            used_bench_ids.add(best_bench["id"])

    def waiver_sort_key(w):
        pts = w.get("recent_avg_points")
        fc_value = w.get("fantasycalc_value")
        # Rank first by real recent production (Sleeper stats), then by
        # FantasyCalc's independent market value, then by how many
        # teams are adding them right now - so a freshly-signed player
        # with no stat history yet can still surface via the other two.
        return (
            pts is not None, pts if pts is not None else 0,
            fc_value is not None, fc_value if fc_value is not None else 0,
            w.get("add_count_48h", 0),
        )

    ranked_waivers = sorted(waiver_candidates, key=waiver_sort_key, reverse=True)[:10]

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "swap_suggestions": swap_suggestions,
        "top_waivers": ranked_waivers,
    }


RELEVANT_POSITIONS = ["QB", "RB", "WR", "TE", "DEF"]


@app.get("/api/leagues/{league_id}/team-needs")
def get_team_needs(league_id: str):
    """
    At each position, is your team scoring above or below the REST OF
    YOUR LEAGUE's average - using real recent performance, not generic
    rankings. This is the kind of cross-roster analysis Sleeper itself
    doesn't show you.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    league_info = sleeper.get_league(league_id)
    scoring_settings = league_info.get("scoring_settings") or {}
    state = sleeper.get_nfl_state()
    season, current_week = state["season"], state["week"]

    my_roster, all_rosters, _ = _find_my_roster(league_id)
    players = sleeper.get_players_cached()

    league_points_by_position = {pos: [] for pos in RELEVANT_POSITIONS}
    my_points_by_position = {pos: [] for pos in RELEVANT_POSITIONS}

    for roster in all_rosters:
        is_me = roster["roster_id"] == my_roster["roster_id"]
        for pid in (roster.get("players") or []):
            position = (players.get(pid) or {}).get("position")
            if position not in RELEVANT_POSITIONS:
                continue
            pts = scoring.recent_avg_points(pid, season, current_week, scoring_settings)
            if pts is None:
                continue
            league_points_by_position[position].append(pts)
            if is_me:
                my_points_by_position[position].append(pts)

    needs = []
    for position in RELEVANT_POSITIONS:
        league_vals = league_points_by_position[position]
        my_vals = my_points_by_position[position]
        if not league_vals or not my_vals:
            continue
        league_avg = round(sum(league_vals) / len(league_vals), 2)
        my_avg = round(sum(my_vals) / len(my_vals), 2)
        diff = round(my_avg - league_avg, 2)
        if diff > 1.5:
            label = "Strength"
        elif diff < -1.5:
            label = "Weakness"
        else:
            label = "Average"
        needs.append({
            "position": position,
            "my_avg": my_avg,
            "league_avg": league_avg,
            "diff": diff,
            "label": label,
        })

    return {"league_id": league_id, "needs": needs}


# Serve the frontend last, so /api/* routes above take priority.
app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
