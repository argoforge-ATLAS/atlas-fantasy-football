"""
Atlas Fantasy Football - Lineup HQ backend.

Serves:
  - GET /api/leagues                      -> basic info for both leagues
  - GET /api/leagues/{league_id}/lineup   -> your roster, starters vs bench
  - GET /api/leagues/{league_id}/matchup  -> this week: you vs your opponent
  - /                                      -> the frontend (static files)
"""
import json

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import sleeper_client as sleeper
import advisor
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
        "starters": [{"id": pid, "name": _player_name(players, pid)} for pid in starters],
        "bench": [{"id": pid, "name": _player_name(players, pid)} for pid in bench],
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


def _build_context(league_id: str) -> dict:
    league_info = sleeper.get_league(league_id)
    my_roster, all_rosters, _ = _find_my_roster(league_id)
    players = sleeper.get_players_cached()

    starters = [_player_brief(players, pid) for pid in (my_roster.get("starters") or [])]
    all_player_ids = my_roster.get("players") or []
    bench = [_player_brief(players, pid) for pid in all_player_ids if pid not in (my_roster.get("starters") or [])]

    # Waiver candidates: players trending up across Sleeper right now
    # who are NOT already rostered by anyone in this league.
    rostered_ids = set()
    for r in all_rosters:
        rostered_ids.update(r.get("players") or [])

    trending = sleeper.get_trending_adds(lookback_hours=48, limit=75)
    waiver_candidates = []
    for t in trending:
        pid = t["player_id"]
        if pid not in rostered_ids:
            brief = _player_brief(players, pid)
            brief["add_count_48h"] = t.get("count")
            waiver_candidates.append(brief)
        if len(waiver_candidates) >= 15:
            break

    return {
        "league_name": league_info.get("name"),
        "scoring_settings": league_info.get("scoring_settings"),
        "roster_positions": league_info.get("roster_positions"),
        "my_starters": starters,
        "my_bench": bench,
        "top_waiver_candidates": waiver_candidates,
    }


class AdviceRequest(BaseModel):
    notes: str = ""


@app.get("/api/leagues/{league_id}/advice-prompt")
def get_advice_prompt(league_id: str):
    """
    No AI call, no API key needed. Returns a ready-to-paste prompt
    (your real roster/league data) that you can hand to any Claude
    chat to get start/sit and waiver advice - using the Claude
    subscription you already pay for instead of a separate paid API.
    """
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    context = _build_context(league_id)
    prompt = (
        "I need fantasy football start/sit and waiver wire advice for my team, "
        "based on this real data. Be specific and tie it to my league's actual scoring/roster rules "
        "(don't give generic PPR advice if my settings differ).\n\n"
        f"```json\n{json.dumps(context, indent=2)}\n```\n"
    )
    return {"league_id": league_id, "prompt": prompt}


@app.post("/api/leagues/{league_id}/advice")
def get_advice(league_id: str, body: AdviceRequest):
    """Optional automatic version - only works if ANTHROPIC_API_KEY is set."""
    if league_id not in _league_lookup:
        raise HTTPException(404, "Unknown league")

    context = _build_context(league_id)
    advice_text = advisor.get_weekly_advice(context, human_notes=body.notes)
    return {"league_id": league_id, "advice": advice_text}


# Serve the frontend last, so /api/* routes above take priority.
app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
