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
        by position vs. the rest of your league (starter-level, ranked)
  - GET /api/leagues/{league_id}/drop-candidates -> bench players worth a
        second look, with long-term HOLD rules (young + injured, dynasty value)
  - GET /api/leagues/{league_id}/trade-eval?give=..&get=.. -> trade balance with
        live-market pick values, consolidation weighting and per-team tax
  - GET /api/leagues/{league_id}/trade-targets -> who to target at your weak
        positions, priced, with a suggested package from your own bench
  - /                                        -> the frontend (static files)
"""
import json
import os
import threading
import time
from pathlib import Path

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
import projections
import valuation
import lineup as lineup_logic
from config import (
    LEAGUES, SLEEPER_USERNAME, POSITION_CAPS, PROTECTED_PICK_ROUNDS, UNTOUCHABLE,
)

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
        "age": p.get("age"),
        "years_exp": p.get("years_exp"),
        "injury_status": injury_status,
        "bad_injury": injury_status in BAD_STATUSES,
        "depth_chart_order": p.get("depth_chart_order"),
    }


# ----------------------------------------------------------------------
# Small TTL memo with a per-key lock. The page fires several endpoints
# at once and most of them need the same expensive roster/pool build;
# the lock makes concurrent requests wait for ONE build instead of each
# repeating it.
# ----------------------------------------------------------------------
_memo_data = {}
_memo_locks = {}
_memo_guard = threading.Lock()
MEMO_TTL_SECONDS = 45


def _memo(key, builder, ttl: float = MEMO_TTL_SECONDS):
    with _memo_guard:
        lock = _memo_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _memo_data.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        value = builder()
        _memo_data[key] = (time.time(), value)
        return value


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
            "dynasty": valuation.is_dynasty_league(info),
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


def _league_fc(league_info: dict, num_teams: int):
    """FantasyCalc board for THIS league: superflex/PPR/size mirror the
    league's real settings, and dynasty vs. redraft values follow the
    league type (it used to be hardcoded to redraft for every league)."""
    scoring_settings = league_info.get("scoring_settings") or {}
    roster_positions_raw = league_info.get("roster_positions") or []
    num_qbs = roster_positions_raw.count("QB") + roster_positions_raw.count("SUPER_FLEX")
    dynasty = valuation.is_dynasty_league(league_info)
    values = fantasycalc.get_values_by_sleeper_id(
        num_qbs=max(num_qbs, 1),
        num_teams=num_teams or 10,
        ppr=scoring_settings.get("rec", 0),
        is_dynasty=dynasty,
    )
    return values, dynasty


def _select_waiver_ids(players, rostered_ids, valid_positions, trend_counts, fc_values, season, current_week, scoring_settings):
    """
    Real free agents, not just Sleeper's trending list. Union of:
      * everyone trending-add on Sleeper right now (and unrostered here),
      * the best unrostered players by recent production under THIS
        league's scoring,
      * the best unrostered players by market value (catches stashes
        and rookies who haven't produced yet).
    """
    pool = []
    for pid, p in players.items():
        if pid in rostered_ids:
            continue
        pos = p.get("position")
        if pos not in valid_positions or pos not in RELEVANT_POSITIONS:
            continue
        if not p.get("team") or p.get("active") is False:
            continue
        pool.append(pid)

    def recent(pid):
        pts = scoring.recent_avg_points(pid, season, current_week, scoring_settings)
        return pts if pts is not None else -1

    by_recent = sorted(pool, key=recent, reverse=True)[:30]
    by_fc = sorted(pool, key=lambda pid: (fc_values.get(pid) or {}).get("value") or 0, reverse=True)[:30]
    pool_set = set(pool)
    trending = [pid for pid in trend_counts if pid in pool_set]
    seen, ordered = set(), []
    for pid in trending + by_recent + by_fc:
        if pid not in seen:
            seen.add(pid)
            ordered.append(pid)
    return ordered


def _build_pool(league_id: str) -> dict:
    """Everything we know about every player on your roster (plus the
    best free agents), enriched with recent production, projections,
    matchup, market value and age - all under THIS league's rules."""
    league_info = sleeper.get_league(league_id)
    scoring_settings = league_info.get("scoring_settings") or {}
    state = sleeper.get_nfl_state()
    season, current_week = state["season"], state["week"]

    my_roster, all_rosters, _ = _find_my_roster(league_id)
    players = sleeper.get_players_cached()

    starter_ids = my_roster.get("starters") or []
    all_ids = my_roster.get("players") or []
    bench_ids = [pid for pid in all_ids if pid not in starter_ids]

    fc_values, dynasty = _league_fc(league_info, len(all_rosters))
    floor = valuation.board_floor(fc_values) if dynasty else None

    # Sleeper's own projection for the week (opponent- and role-aware),
    # scored under THIS league's rules. {} if the endpoint is down.
    proj_by_pid = sleeper.get_projections(season, current_week)

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
        brief["fantasycalc_rank"] = fc["overall_rank"] if fc else None

        team = brief.get("team")
        position = brief.get("position")

        upcoming = []
        for w in upcoming_weeks:
            week_schedule = upcoming_schedules.get(w) or {}
            opponent = week_schedule.get(team) if team else None
            # A team genuinely has no game that week (bye) only if we
            # have a real schedule for that week and the team's just
            # missing from it - not if the schedule fetch itself came
            # back empty (ESPN unreachable, week not published yet).
            on_bye = bool(team) and bool(week_schedule) and team not in week_schedule
            upcoming.append({
                "week": w,
                "opponent": opponent,
                "on_bye": on_bye,
                "matchup_label": _matchup_for(team, position, opponent),
            })
        brief["upcoming_matchups"] = upcoming

        # Keep these top-level for backwards compatibility / this week's view.
        this_week = upcoming[0]
        brief["opponent"] = this_week["opponent"]
        brief["on_bye"] = this_week["on_bye"]
        brief["matchup_label"] = this_week["matchup_label"]
        if this_week["opponent"] and brief["matchup_label"]:
            rank_info = defense_ranks.get(this_week["opponent"], {}).get(position)
            brief["matchup_detail"] = f"{this_week['opponent']} allows the {_ordinal(rank_info['rank'])}-most {position} points in the league"

        proj = projections.projected_points(proj_by_pid.get(pid), scoring_settings)
        brief["projected_points"] = round(proj, 1) if proj is not None else None
        brief["expected_points"], brief["expected_source"] = projections.blend_expected(
            proj, brief["recent_avg_points"], brief["matchup_label"]
        )
        brief["age_outlook"] = valuation.age_outlook(position, brief.get("age"))
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
    trend_counts = {t["player_id"]: t.get("count") for t in trending}
    candidate_ids = _select_waiver_ids(
        players, rostered_ids, valid_positions, trend_counts, fc_values,
        season, current_week, scoring_settings,
    )
    waiver_candidates = []
    for pid in candidate_ids:
        brief = enrich(pid)
        brief["add_count_48h"] = trend_counts.get(pid, 0) or 0
        waiver_candidates.append(brief)
    waiver_candidates.sort(key=lambda w: lineup_logic.add_score(w, dynasty), reverse=True)
    waiver_candidates = waiver_candidates[:40]

    return {
        "league_info": league_info,
        "starters": starters,
        "bench": bench,
        "waiver_candidates": waiver_candidates,
        "dynasty": dynasty,
        "fc_values": fc_values,
        "floor": floor,
        "scoring_settings": scoring_settings,
        "season": season,
        "week": current_week,
        "my_roster": my_roster,
        "all_rosters": all_rosters,
        "players": players,
        "slot_types": slot_types,
    }


def _pool(league_id: str) -> dict:
    return _memo(("pool", league_id), lambda: _build_pool(league_id))


def _build_player_pool(league_id: str):
    """Back-compat 4-tuple for callers that predate the pool dict."""
    pool = _pool(league_id)
    return pool["league_info"], pool["starters"], pool["bench"], pool["waiver_candidates"]


@app.get("/api/leagues/{league_id}/recommendations")
def get_recommendations(league_id: str):
    """
    Start/sit and waiver suggestions, computed entirely in Python:
      - Start/sit compares EXPECTED points for the week: Sleeper's
        projection (scored under this league's own rules) blended with
        trailing production, matchup-adjusted when no projection exists.
        A starter who is Out/Doubtful/IR or on a bye is always flagged;
        Questionable players are discounted rather than benched. The
        best swap is applied each round, so FLEX/SUPER_FLEX chains
        resolve properly (>1.5 pt margin to avoid noise).
      - Waivers are ranked from REAL free agents (trending adds +
        best unrostered by production + best by market value), and the
        waiver plan pairs every add with a specific drop, in claim order.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    pool = _pool(league_id)
    starters, bench = pool["starters"], pool["bench"]
    dynasty = pool["dynasty"]

    swap_suggestions = lineup_logic.optimize_swaps(starters, bench, SLOT_ELIGIBILITY)

    ranked_waivers = sorted(
        pool["waiver_candidates"], key=lambda w: lineup_logic.add_score(w, dynasty), reverse=True
    )[:10]

    drops = _drop_candidates(league_id)["candidates"]
    league_name = pool["league_info"].get("name")
    counts = {}
    for pid in pool["my_roster"].get("players") or []:
        pos = (pool["players"].get(pid) or {}).get("position")
        counts[pos] = counts.get(pos, 0) + 1
    caps = POSITION_CAPS.get(league_name, {})
    needed = lineup_logic.open_slot_positions(swap_suggestions)
    waiver_plan = lineup_logic.build_waiver_plan(
        adds=pool["waiver_candidates"],
        drops=[d for d in drops if d["verdict"] == "Drop candidate"],
        position_counts=counts,
        caps=caps,
        dynasty=dynasty,
        needed_positions=needed,
    )
    # Last-resort list: the safe plan never cuts held players, which can
    # leave it empty. Offer the least valuable market-value holds, clearly
    # flagged, for adds the safe plan didn't already use.
    fallback_plan = lineup_logic.build_fallback_plan(
        adds=pool["waiver_candidates"],
        drops=drops,
        position_counts=counts,
        caps=caps,
        dynasty=dynasty,
        needed_positions=needed,
        exclude_add_ids={r["add"]["id"] for r in waiver_plan},
        exclude_drop_ids={r["drop"]["id"] for r in waiver_plan},
    )
    # An empty lineup slot neither plan filled still needs a body, even if
    # that means cutting someone the plans above won't touch.
    planned = waiver_plan + fallback_plan
    slot_fixes = lineup_logic.build_slot_fixes(
        adds=pool["waiver_candidates"],
        roster=_slot_fix_drop_pool(league_id, pool, swap_suggestions, drops),
        needed_positions=needed - {r["add"]["position"] for r in planned if r.get("fills_slot")},
        roster_full=_roster_is_full(pool),
        dynasty=dynasty,
        exclude_add_ids={r["add"]["id"] for r in planned},
        exclude_drop_ids={r["drop"]["id"] for r in planned},
    )

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "dynasty": dynasty,
        "swap_suggestions": swap_suggestions,
        "top_waivers": ranked_waivers,
        "slot_fixes": slot_fixes,
        "waiver_plan": waiver_plan,
        "fallback_plan": fallback_plan,
        "open_slots": sorted(needed),
    }


def _roster_is_full(pool: dict) -> bool:
    """Every active roster spot taken (IR and taxi players don't count),
    so an add needs a drop."""
    roster = pool["my_roster"]
    stashed = set(roster.get("reserve") or []) | set(roster.get("taxi") or [])
    active = [pid for pid in (roster.get("players") or []) if pid not in stashed]
    return len(active) >= len(pool["league_info"].get("roster_positions") or [])


def _slot_fix_drop_pool(league_id: str, pool: dict, swap_suggestions: list, drops: list) -> list:
    """Everyone you could cut to fill an empty lineup slot: not a healthy
    starter you need this week (or a bench player moving into the
    lineup), not an IR/taxi stash, and not a long-term bet the hold rules
    protect (young + injured, young upside, rising role). Players held
    only for market value stay in, marked `hold` so the drop is flagged."""
    roster = pool["my_roster"]
    stashed = set(roster.get("reserve") or []) | set(roster.get("taxi") or [])
    moving_in = {s["start"] for s in swap_suggestions if s.get("start")}
    role_growing = {d["id"] for d in drops if d["verdict"] == "Hold - role growing"}
    dynasty, floor = pool["dynasty"], pool["floor"]

    out = []
    for p in pool["starters"] + pool["bench"]:
        is_starter = "slot" in p
        if is_starter and not (p.get("bad_injury") or p.get("on_bye")):
            continue
        if p["id"] in stashed or p.get("injury_status") in STASH_STATUSES:
            continue
        if not is_starter and p["name"] in moving_in:
            continue
        hold = valuation.hold_reason(p, dynasty, floor)
        if (hold and hold != lineup_logic.FALLBACK_HOLD_VERDICT) or p["id"] in role_growing:
            continue
        out.append(dict(p, hold=bool(hold), keep_score=valuation.keep_score(p, dynasty)))

    if pool["league_info"].get("name") in KEEPER_RULE_LEAGUE_NAMES and out:
        eligible = keeper_eligibility.get_keeper_eligible_ids(league_id, {p["id"] for p in out})
        for p in out:
            p["keeper_eligible"] = p["id"] in eligible
    return out


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

    pool = _pool(league_id)
    starters, bench = pool["starters"], pool["bench"]

    def trim(p):
        return {
            "name": p["name"],
            "position": p.get("position"),
            "team": p.get("team"),
            "opponent": p.get("opponent"),
            "on_bye": p.get("on_bye"),
            "matchup_label": p.get("matchup_label"),
            "matchup_detail": p.get("matchup_detail"),
            "recent_avg_points": p.get("recent_avg_points"),
            "expected_points": p.get("expected_points"),
            "projected_points": p.get("projected_points"),
            "injury_status": p.get("injury_status"),
            "bad_injury": p.get("bad_injury"),
        }

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "week": pool["week"],
        "players": [trim(p) for p in starters + bench if p.get("position") in defense_rankings.RELEVANT_POSITIONS],
    }


# ----------------------------------------------------------------------
# Drops (with long-term hold rules)
# ----------------------------------------------------------------------

def _league_position_averages(league_id: str) -> dict:
    """Average recent fantasy points at each position, pooled across
    EVERY roster in the league (not just yours) - the real
    replacement-level baseline for that position in this league right
    now, computed fresh each time rather than a hardcoded number per
    position (a QB's replacement level is nothing like a TE's)."""
    pool = _pool(league_id)
    scoring_settings, season, current_week = pool["scoring_settings"], pool["season"], pool["week"]
    players = pool["players"]

    totals = {pos: [] for pos in RELEVANT_POSITIONS}
    for roster in pool["all_rosters"]:
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


def _drop_candidates(league_id: str) -> dict:
    return _memo(("drops", league_id), lambda: _build_drop_candidates(league_id))


def _build_drop_candidates(league_id: str) -> dict:
    pool = _pool(league_id)
    league_info, bench = pool["league_info"], pool["bench"]
    dynasty, floor = pool["dynasty"], pool["floor"]
    season, current_week = pool["season"], pool["week"]
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

        # Long-term value beats last week's box score: a young injured
        # player, or anyone with real market value in a keeper/dynasty
        # league, is a HOLD no matter how quiet recent games were.
        hold = valuation.hold_reason(b, dynasty, floor)
        if hold:
            verdict = hold
        elif is_stash:
            verdict = "Hold - role growing"
        else:
            verdict = "Drop candidate"

        candidates.append({
            "id": b["id"],
            "name": b["name"],
            "position": position,
            "team": b.get("team"),
            "age": b.get("age"),
            "age_outlook": b.get("age_outlook"),
            "years_exp": b.get("years_exp"),
            "recent_avg_points": pts,
            "expected_points": b.get("expected_points"),
            "fantasycalc_value": b.get("fantasycalc_value"),
            "league_avg_at_position": round(league_avg, 2) if league_avg is not None else None,
            "injury_status": b.get("injury_status"),
            "bad_injury": b.get("bad_injury"),
            "opportunity_pct": opportunity_pct,
            "opportunity_trend": opportunity_dir,
            "verdict": verdict,
            "keep_score": round(valuation.keep_score(b, dynasty), 2),
            "keeper_eligible": enforce_keeper_rule and b["id"] in keeper_eligible_ids,
        })

    # Real drop candidates first, least valuable (lowest keep score) at
    # the top - not just lowest recent points.
    candidates.sort(key=lambda c: (c["verdict"] != "Drop candidate", c["keep_score"]))

    return {
        "league_id": league_id,
        "label": _league_lookup[league_id],
        "dynasty": dynasty,
        "enforce_keeper_rule": enforce_keeper_rule,
        "candidates": candidates,
    }


@app.get("/api/leagues/{league_id}/drop-candidates")
def get_drop_candidates(league_id: str):
    """
    Bench players worth reconsidering for the waiver wire. Raw recent
    points alone can't tell "role is gone" apart from "real role,
    hasn't converted yet" - so RB/WR/TE are also checked against their
    share of their own team's targets/carries (and whether that share
    is rising or falling) before being called an actual drop candidate
    vs. a stash. Long-term value wins over a quiet week: young injured
    players and anyone with real dynasty market value are held. In
    DickMelt Fantasies, players who are still keeper-eligible (never
    touched waivers, however they got onto your roster) are flagged
    rather than suggested outright - dropping them costs that
    eligibility for good.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")
    return _drop_candidates(league_id)


# ----------------------------------------------------------------------
# Team needs (starter-level, ranked)
# ----------------------------------------------------------------------

def _starter_counts(roster_positions: list) -> dict:
    """How many players at each position a roster actually STARTS -
    including a share of the FLEX / SUPER_FLEX slots. Team needs compare
    starter-level production; averaging everyone on a roster (bench
    scrubs included) buries the signal."""
    counts = {pos: roster_positions.count(pos) for pos in RELEVANT_POSITIONS}
    if "FLEX" in roster_positions:
        counts["RB"] += 1
        counts["WR"] += 1
    if "SUPER_FLEX" in roster_positions:
        counts["QB"] += 1
    return {pos: max(n, 1) for pos, n in counts.items()}


def _compute_team_needs(league_id: str) -> list:
    pool = _pool(league_id)
    scoring_settings, season, current_week = pool["scoring_settings"], pool["season"], pool["week"]
    players = pool["players"]
    my_id = pool["my_roster"]["roster_id"]
    k_by_pos = _starter_counts(pool["league_info"].get("roster_positions") or [])

    # roster_id -> position -> [recent avg of each player]
    per_roster = {}
    for roster in pool["all_rosters"]:
        by_pos = {pos: [] for pos in RELEVANT_POSITIONS}
        for pid in (roster.get("players") or []):
            position = (players.get(pid) or {}).get("position")
            if position not in RELEVANT_POSITIONS:
                continue
            pts = scoring.recent_avg_points(pid, season, current_week, scoring_settings)
            if pts is not None:
                by_pos[position].append(pts)
        per_roster[roster["roster_id"]] = by_pos

    def starter_avg(vals, pos):
        top = sorted(vals, reverse=True)[: k_by_pos[pos]]
        return sum(top) / len(top) if top else None

    needs = []
    for position in RELEVANT_POSITIONS:
        avgs = {rid: starter_avg(by_pos[position], position) for rid, by_pos in per_roster.items()}
        avgs = {rid: a for rid, a in avgs.items() if a is not None}
        mine = avgs.get(my_id)
        others = [a for rid, a in avgs.items() if rid != my_id]
        if mine is None or not others:
            continue
        league_avg = round(sum(others) / len(others), 2)
        my_avg = round(mine, 2)
        diff = round(my_avg - league_avg, 2)
        rank = 1 + sum(1 for a in avgs.values() if a > mine)
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
            "rank": rank,
            "out_of": len(avgs),
            "label": label,
        })
    return needs


@app.get("/api/leagues/{league_id}/team-needs")
def get_team_needs(league_id: str):
    """
    At each position, how do your STARTERS (top N at the position,
    including your share of FLEX) score against every other roster's
    starters in the league - real recent performance, not generic
    rankings, with your rank out of the league. This is the kind of
    cross-roster analysis Sleeper itself doesn't show you.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")
    return {"league_id": league_id, "needs": _compute_team_needs(league_id)}


# ----------------------------------------------------------------------
# Trades
# ----------------------------------------------------------------------

def _team_name(users_by_id: dict, roster: dict) -> str:
    user = users_by_id.get(roster.get("owner_id")) or {}
    return (user.get("metadata") or {}).get("team_name") or user.get("display_name") or f"Team {roster.get('roster_id')}"


def _normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum() or ch == " ").strip()


def _resolve_player(token: str, players: dict, fc_values: dict):
    """Find a player id from a typed name: exact match first, then a
    unique-ish substring match (ties go to the higher-valued player)."""
    target = _normalize(token)
    if not target:
        return None
    exact, partial = [], []
    for pid, p in players.items():
        if p.get("position") not in RELEVANT_POSITIONS or not p.get("team"):
            continue
        name = _normalize(p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}")
        if name == target:
            exact.append(pid)
        elif target in name:
            partial.append(pid)
    matches = exact or partial
    if not matches:
        return None
    return max(matches, key=lambda pid: (fc_values.get(pid) or {}).get("value") or 0)


def _asset_for_token(token: str, pool: dict):
    """Turn 'Puka Nacua' or '2027 R1' into an asset dict (or None)."""
    pick = valuation.parse_pick(token)
    if pick:
        season, rnd = pick
        label = f"{season or 'Next'} round {rnd} pick"
        return {
            "label": label, "kind": "pick", "round": rnd, "team": None, "id": None,
            "value": valuation.pick_value(rnd, pool["fc_values"]),
        }
    pid = _resolve_player(token, pool["players"], pool["fc_values"])
    if pid is None:
        return None
    p = pool["players"][pid]
    fc = pool["fc_values"].get(pid)
    return {
        "label": f"{_player_name(pool['players'], pid)} ({p.get('position')}, {p.get('team')})",
        "kind": "player", "round": None, "team": p.get("team"), "id": pid,
        "name": _player_name(pool["players"], pid),
        "age": p.get("age"),
        "value": fc["value"] if fc else None,
    }


def _split(csv: str):
    return [t.strip() for t in (csv or "").split(",") if t.strip()]


@app.get("/api/leagues/{league_id}/trade-eval")
def get_trade_eval(league_id: str, give: str = "", get: str = ""):
    """
    Evaluate a trade from names: ?give=Garrett Wilson,2027 R2&get=Puka Nacua
    Values come from the live FantasyCalc board for THIS league (dynasty
    or redraft, superflex-aware); picks are priced off the same board so
    they float with the market. The package math is consolidation-
    weighted, assets you RECEIVE carry the league's team tax (Kansas
    City players cost extra in DickMelt), and sending a protected pick
    or an untouchable player throws a warning.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    pool = _pool(league_id)
    league_name = pool["league_info"].get("name")

    give_assets, get_assets, unresolved = [], [], []
    for token in _split(give):
        a = _asset_for_token(token, pool)
        (give_assets if a else unresolved).append(a or token)
    for token in _split(get):
        a = _asset_for_token(token, pool)
        (get_assets if a else unresolved).append(a or token)

    result = valuation.evaluate_trade(give_assets, get_assets, league_name)

    warnings = []
    protected = PROTECTED_PICK_ROUNDS.get(league_name, [])
    for a in give_assets:
        if a["kind"] == "pick" and a["round"] in protected:
            warnings.append(f"Sending a round {a['round']} pick - you keep rounds {protected[0]}-{protected[-1]} in {league_name}, so this means you're all in.")
        if a["kind"] == "player" and a.get("name") in (UNTOUCHABLE.get(league_name) or []):
            warnings.append(f"{a['name']} is on your untouchable list.")
    for a in result["get"]:
        if a.get("tax", 1.0) > 1.0:
            warnings.append(f"{a['label']} carries a {round((a['tax'] - 1) * 100)}% team tax in {league_name}.")
    for a in give_assets + get_assets:
        if a["kind"] == "player" and a.get("value") is None:
            warnings.append(f"No market value found for {a['label']}; counted as 0.")
    if unresolved:
        warnings.append("Couldn't find: " + ", ".join(str(u) for u in unresolved))

    result.update({
        "league_id": league_id,
        "league_name": league_name,
        "dynasty": pool["dynasty"],
        "warnings": warnings,
    })
    return result


@app.get("/api/leagues/{league_id}/trade-targets")
def get_trade_targets(league_id: str):
    """
    Players on OTHER rosters at the positions where you're weakest,
    priced off the live market (with the league's team tax), with the
    cheapest package from your own tradeable bench that covers the
    price. Untouchables and your starters are never offered.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    pool = _pool(league_id)
    league_name = pool["league_info"].get("name")
    needs = _compute_team_needs(league_id)
    weak = [n for n in needs if n["label"] == "Weakness" and n["position"] != "DEF"]
    if not weak:
        non_def = [n for n in needs if n["position"] != "DEF"]
        weak = sorted(non_def, key=lambda n: n["diff"])[:1]
    weak_positions = [n["position"] for n in weak]

    users = sleeper.get_league_users(league_id)
    users_by_id = {u["user_id"]: u for u in users}
    my_id = pool["my_roster"]["roster_id"]
    players, fc_values = pool["players"], pool["fc_values"]

    untouchable = set(UNTOUCHABLE.get(league_name) or [])
    my_assets = [
        {"label": b["name"], "value": b.get("fantasycalc_value")}
        for b in pool["bench"]
        if b["name"] not in untouchable and b.get("fantasycalc_value")
    ]

    targets = []
    for roster in pool["all_rosters"]:
        if roster["roster_id"] == my_id:
            continue
        for pid in roster.get("players") or []:
            p = players.get(pid) or {}
            if p.get("position") not in weak_positions:
                continue
            fc = fc_values.get(pid)
            if not fc or not fc.get("value"):
                continue
            tax = valuation.team_tax(league_name, p.get("team"))
            price = round(fc["value"] * tax)
            package, package_total = valuation.suggest_package(price, my_assets)
            targets.append({
                "id": pid,
                "name": _player_name(players, pid),
                "position": p.get("position"),
                "team": p.get("team"),
                "age": p.get("age"),
                "age_outlook": valuation.age_outlook(p.get("position"), p.get("age")),
                "owner": _team_name(users_by_id, roster),
                "value": fc["value"],
                "price": price,
                "tax": tax,
                "suggested_package": package,
                "package_value": package_total,
            })

    targets.sort(key=lambda t: t["value"], reverse=True)
    top = []
    per_pos = {}
    for t in targets:
        if per_pos.get(t["position"], 0) >= 6:
            continue
        per_pos[t["position"]] = per_pos.get(t["position"], 0) + 1
        top.append(t)

    return {
        "league_id": league_id,
        "league_name": league_name,
        "dynasty": pool["dynasty"],
        "weak_positions": weak_positions,
        "targets": top,
    }


# Serve the frontend last, so /api/* routes above take priority.
# In Docker main.py sits at /app/ next to /app/frontend; in a repo
# checkout it is backend/main.py with ../frontend beside it.
_here = Path(__file__).resolve().parent


def _find_frontend_dir() -> Path:
    # Path("") is "." (the current directory), so an unset FRONTEND_DIR
    # must be skipped explicitly - otherwise the app serves its own
    # source folder instead of the frontend.
    candidates = []
    override = os.environ.get("FRONTEND_DIR", "").strip()
    if override:
        candidates.append(Path(override))
    candidates += [_here / "frontend", _here.parent / "frontend"]
    for d in candidates:
        if d.is_dir() and (d / "index.html").is_file():
            return d
    return _here / "frontend"


_frontend_dir = _find_frontend_dir()
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
