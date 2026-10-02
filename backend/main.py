"""
Atlas Fantasy Football - Lineup HQ backend.

Serves:
  - GET /api/leagues                        -> basic info for both leagues
  - GET /api/leagues/{league_id}/lineup     -> your roster, starters vs bench
  - GET /api/leagues/{league_id}/matchups   -> this week's real NFL opponent
        for each of your players + how tough that defense has been at
        their position (from raw stats + a free schedule source)
  - GET /api/leagues/{league_id}/recommendations -> start/sit + waiver suggestions,
        computed in Python from real recent performance under THIS league's
        actual scoring rules. No AI call, no external chat step.
  - GET /api/leagues/{league_id}/team-needs -> your team's strengths/weaknesses
        by position vs. the rest of your league
  - /                                        -> the frontend (static files)
"""
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import sleeper_client as sleeper
import scoring
import fantasycalc_client as fantasycalc
import nfl_schedule
import defense_rankings
import keeper_eligibility
import opportunity
from config import LEAGUES, SLEEPER_USERNAME

# DickMelt Fantasies has a house rule: a player keeps keeper
# eligibility for as long as they've never touched the waiver wire,
# however they got onto a roster (draft or trade). Matched by the
# league's actual Sleeper name, not league order in config.py, so a
# reordering or ID change can't silently flip which league this
# applies to.
KEEPER_RULE_LEAGUE_NAMES = {"DickMelt Fantasies"}

app = FastAPI(title="Atlas Fantasy Football - Lineup HQ")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_store_api_responses(request: Request, call_next):
    """Lineup/roster data changes on Sleeper's side at any moment (e.g.
    you set a new starter from your phone), so API responses should
    never be cached by the browser - always fetch fresh."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

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


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Statuses that mean "don't start this guy," checked everywhere we
# touch injury status - defined once, up top, so every endpoint uses
# the exact same rule.
BAD_STATUSES = {"Out", "Doubtful", "IR", "PUP", "Suspended"}

# Of those, only these are a LONG-TERM roster decision already made
# (stashed on IR, suspended, etc.) - Out/Doubtful/Questionable are
# just this week's game-day uncertainty and shouldn't exempt an
# otherwise-bad player from being flagged as droppable.
STASH_STATUSES = {"IR", "PUP", "Suspended"}

RELEVANT_POSITIONS = ["QB", "RB", "WR", "TE", "DEF"]


def _player_brief(players: dict, player_id: str) -> dict:
    p = players.get(player_id, {})
    injury_status = p.get("injury_status")
    return {
        "name": _player_name(players, player_id),
        "position": p.get("position"),
        "team": p.get("team"),
        "injury_status": injury_status,
        "bad_injury": injury_status in BAD_STATUSES,
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

    # This week's real NFL schedule, and how tough each defense has
    # been against each position recently (both independent of
    # Sleeper/FantasyCalc - computed from raw stats + ESPN's schedule).
    # Look at this week plus the next two - useful for "is this waiver
    # pickup's schedule actually good, or just a one-week mirage?"
    upcoming_weeks = [current_week, current_week + 1, current_week + 2]
    upcoming_schedules = {w: nfl_schedule.get_week_schedule(season, w) for w in upcoming_weeks}
    lookback_weeks = scoring.get_recent_weeks(current_week)
    defense_ranks = defense_rankings.get_defense_rankings(season, lookback_weeks, scoring_settings)

    def _matchup_for(team, position, opponent):
        if not opponent or position not in defense_rankings.RELEVANT_POSITIONS:
            return None
        rank_info = defense_ranks.get(opponent, {}).get(position)
        if not rank_info:
            return None
        return defense_rankings.matchup_label(rank_info["rank"], rank_info["out_of"])

    def enrich(pid):
        brief = _player_brief(players, pid)
        brief["id"] = pid
        brief["recent_avg_points"] = scoring.recent_avg_points(pid, season, current_week, scoring_settings)
        fc = fc_values.get(pid)
        brief["fantasycalc_value"] = fc["value"] if fc else None

        team = brief.get("team")
        position = brief.get("position")

        upcoming = []
        for w in upcoming_weeks:
            opponent = upcoming_schedules.get(w, {}).get(team) if team else None
            upcoming.append({
                "week": w,
                "opponent": opponent,
                "matchup_label": _matchup_for(team, position, opponent),
            })
        brief["upcoming_matchups"] = upcoming

        # Keep these top-level for backwards compatibility / this week's view.
        this_week = upcoming[0]
        brief["opponent"] = this_week["opponent"]
        brief["matchup_label"] = this_week["matchup_label"]
        if this_week["opponent"] and brief["matchup_label"]:
            rank_info = defense_ranks.get(this_week["opponent"], {}).get(position)
            brief["matchup_detail"] = f"{this_week['opponent']} allows the {_ordinal(rank_info['rank'])}-most {position} points in the league"
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
        return p.get("bad_injury", False)

    ordered_starters = sorted(starters, key=lambda s: (not is_bad(s),))

    for starter in ordered_starters:
        eligible_positions = SLOT_ELIGIBILITY.get(starter.get("slot"), {starter.get("position")})
        candidates = [
            b for b in bench
            if b["id"] not in used_bench_ids
            and b.get("position") in eligible_positions
            and not is_bad(b)  # never recommend starting an injured/Out player
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


@app.get("/api/leagues/{league_id}/matchups")
def get_weekly_matchups(league_id: str):
    """
    This week's real-life opponent for every player on your roster,
    plus how tough that opponent's defense has been against that
    position recently - e.g. 'Khalif Raymond vs. LAR - Tough matchup'.
    Computed from raw stats + a free NFL schedule source, independent
    of Sleeper and FantasyCalc.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    league_info, starters, bench, _ = _build_player_pool(league_id)
    state = sleeper.get_nfl_state()

    def trim(p):
        return {
            "name": p["name"],
            "position": p.get("position"),
            "team": p.get("team"),
            "opponent": p.get("opponent"),
            "matchup_label": p.get("matchup_label"),
            "matchup_detail": p.get("matchup_detail"),
            "recent_avg_points": p.get("recent_avg_points"),
            "injury_status": p.get("injury_status"),
            "bad_injury": p.get("bad_injury"),
        }

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "week": state.get("week"),
        "players": [trim(p) for p in starters + bench if p.get("position") in defense_rankings.RELEVANT_POSITIONS],
    }


def _league_position_averages(league_id: str) -> dict:
    """Average recent fantasy points at each position, pooled across
    EVERY roster in the league (not just yours) - the real
    replacement-level baseline for that position in this league right
    now, computed fresh each time rather than a hardcoded number per
    position (a QB's replacement level is nothing like a TE's)."""
    league_info = sleeper.get_league(league_id)
    scoring_settings = league_info.get("scoring_settings") or {}
    state = sleeper.get_nfl_state()
    season, current_week = state["season"], state["week"]
    _, all_rosters, _ = _find_my_roster(league_id)
    players = sleeper.get_players_cached()

    totals = {pos: [] for pos in RELEVANT_POSITIONS}
    for roster in all_rosters:
        for pid in (roster.get("players") or []):
            position = (players.get(pid) or {}).get("position")
            if position not in RELEVANT_POSITIONS:
                continue
            pts = scoring.recent_avg_points(pid, season, current_week, scoring_settings)
            if pts is not None:
                totals[position].append(pts)

    return {pos: sum(vals) / len(vals) for pos, vals in totals.items() if vals}


# Below this fraction of the league's OWN average at that position,
# worth a second look. Dynamic and position-relative, rather than one
# flat number for every position - a QB and a TE don't share a
# replacement level.
DROP_CANDIDATE_RATIO = 0.65

# Only used if we don't have a league average yet (e.g. week 1, no
# stats anywhere) - a safety net, not the real rule.
FALLBACK_DROP_MAX_PTS = 4.0

# A low scorer whose share of their team's own targets/carries is at
# or above this (or climbing) likely hasn't converted opportunity into
# points yet, rather than having no real role - worth holding, not
# cutting. A starting rule of thumb, not a tuned number.
STASH_OPPORTUNITY_PCT = 18.0


@app.get("/api/leagues/{league_id}/drop-candidates")
def get_drop_candidates(league_id: str):
    """
    Bench players worth reconsidering for the waiver wire. Raw recent
    points alone can't tell "role is gone" apart from "real role,
    hasn't converted yet" - so RB/WR/TE are also checked against their
    share of their own team's targets/carries (and whether that share
    is rising or falling) before being called an actual drop candidate
    vs. a stash. In DickMelt Fantasies, players who are still
    keeper-eligible (never touched waivers, however they got onto your
    roster) are flagged rather than suggested outright - dropping them
    costs that eligibility for good.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    league_info, starters, bench, waiver_candidates = _build_player_pool(league_id)
    state = sleeper.get_nfl_state()
    season, current_week = state["season"], state["week"]
    lookback_weeks = scoring.get_recent_weeks(current_week)
    league_avgs = _league_position_averages(league_id)

    enforce_keeper_rule = league_info.get("name") in KEEPER_RULE_LEAGUE_NAMES
    keeper_eligible_ids = set()
    if enforce_keeper_rule:
        bench_ids = {b["id"] for b in bench}
        keeper_eligible_ids = keeper_eligibility.get_keeper_eligible_ids(league_id, bench_ids)

    candidates = []
    for b in bench:
        # A long-term stash (IR/PUP/Suspended) is a decision you've
        # already made, not a "should I drop this guy" question. But
        # Out/Doubtful/Questionable is just this week's status - it
        # shouldn't hide an otherwise-bad player from this list.
        if b.get("injury_status") in STASH_STATUSES:
            continue

        position = b.get("position")
        league_avg = league_avgs.get(position)
        threshold = league_avg * DROP_CANDIDATE_RATIO if league_avg is not None else FALLBACK_DROP_MAX_PTS

        pts = b.get("recent_avg_points")
        if pts is not None and pts >= threshold:
            continue

        opportunity_pct, opportunity_dir = (None, None)
        if position in ("RB", "WR", "TE"):
            opportunity_pct, opportunity_dir = opportunity.opportunity_trend(
                b["id"], b.get("team"), position, season, lookback_weeks
            )

        is_stash = opportunity_dir == "Rising" or (
            opportunity_pct is not None and opportunity_pct >= STASH_OPPORTUNITY_PCT
        )
        verdict = "Hold - role growing" if is_stash else "Drop candidate"

        candidates.append({
            "name": b["name"],
            "position": position,
            "recent_avg_points": pts,
            "league_avg_at_position": round(league_avg, 2) if league_avg is not None else None,
            "injury_status": b.get("injury_status"),
            "bad_injury": b.get("bad_injury"),
            "opportunity_pct": opportunity_pct,
            "opportunity_trend": opportunity_dir,
            "verdict": verdict,
            "keeper_eligible": enforce_keeper_rule and b["id"] in keeper_eligible_ids,
        })

    candidates.sort(key=lambda c: (
        c["verdict"] != "Drop candidate",
        c["recent_avg_points"] if c["recent_avg_points"] is not None else -1,
    ))

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "enforce_keeper_rule": enforce_keeper_rule,
        "candidates": candidates,
    }


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
