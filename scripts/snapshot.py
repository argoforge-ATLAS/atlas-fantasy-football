#!/usr/bin/env python3
"""
Weekly snapshot for the Tuesday fantasy review.

Runs on Atlas (where api.sleeper.app is reachable), gathers everything the
review needs for BOTH leagues into one JSON file, and optionally commits it
to a private data repo so Claude's scheduled run can read it.

Usage:
    python3 snapshot.py                 # writes snapshots/latest.json
    DATA_REPO_DIR=/home/drew/fantasy-data python3 snapshot.py   # also commit + push

No API keys. Only the public Sleeper API (and, optionally, the local
Lineup HQ app on localhost:8095).
"""
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

API = "https://api.sleeper.app/v1"
USERNAME = "dpaters1"
LEAGUES = {
    "1382428809231347712": {"name": "DickMelt Fantasies", "stat": "pts_half_ppr"},
    "1388712035025424384": {"name": "Chiefs By 90", "stat": "pts_ppr"},
}
LINEUP_HQ = os.environ.get("LINEUP_HQ_URL", "http://localhost:8095")
# Work files live outside the Lineup HQ repo so git status there stays clean.
CACHE_DIR = Path(os.environ.get("SNAPSHOT_CACHE_DIR", Path.home() / ".cache" / "fantasy-snapshot"))
OUT_DIR = Path(os.environ.get("SNAPSHOT_DIR", CACHE_DIR / "snapshots"))
PLAYERS_CACHE = Path(os.environ.get("PLAYERS_CACHE", CACHE_DIR / "players_cache.json"))
POSITIONS = ["QB", "RB", "WR", "TE", "DEF"]

s = requests.Session()


def get(path, base=API, timeout=30):
    r = s.get(base + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def players_dict():
    """Full player list; Sleeper asks for at most one pull per day."""
    if PLAYERS_CACHE.exists() and time.time() - PLAYERS_CACHE.stat().st_mtime < 20 * 3600:
        return json.loads(PLAYERS_CACHE.read_text())
    data = get("/players/nfl", timeout=120)
    PLAYERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PLAYERS_CACHE.write_text(json.dumps(data))
    return data


def brief(P, pid, stats=None, stat_key=None):
    p = P.get(pid) or {}
    out = {
        "id": pid,
        "name": p.get("full_name") or pid,
        "pos": p.get("position"),
        "team": p.get("team"),
        "injury": p.get("injury_status"),
        "age": p.get("age"),
        "years_exp": p.get("years_exp"),
    }
    if stats is not None and pid in stats and stat_key:
        st = stats[pid]
        gp = st.get("gp") or 0
        pts = st.get(stat_key) or 0
        out.update({"season_pts": round(pts, 1), "gp": gp, "ppg": round(pts / gp, 1) if gp else None})
    return out


def app_json(path):
    """Best-effort call to the local Lineup HQ app; never fatal."""
    try:
        return get(path, base=LINEUP_HQ, timeout=60)
    except Exception as e:  # app may be down or the route may change
        return {"error": str(e)}


def build_league(lid, cfg, P, season, week, uid, season_stats, trending):
    stat_key = cfg["stat"]
    league = get(f"/league/{lid}")
    users = {u["user_id"]: (u.get("metadata", {}).get("team_name") or u["display_name"]) for u in get(f"/league/{lid}/users")}
    rosters = get(f"/league/{lid}/rosters")
    matchups = get(f"/league/{lid}/matchups/{week}")
    by_rid = {m["roster_id"]: m for m in matchups}
    team_of = {r["roster_id"]: users.get(r["owner_id"], f"Roster {r['roster_id']}") for r in rosters}
    me = next(r for r in rosters if r["owner_id"] == uid)

    standings = sorted(
        (
            {
                "roster_id": r["roster_id"],
                "team": team_of[r["roster_id"]],
                "wins": r["settings"]["wins"],
                "losses": r["settings"]["losses"],
                # Sleeper keeps the decimal part in a separate field
                "pf": round((r["settings"].get("fpts") or 0) + (r["settings"].get("fpts_decimal") or 0) / 100, 2),
                "waiver_position": r["settings"].get("waiver_position"),
            }
            for r in rosters
        ),
        key=lambda x: (-x["wins"], -(x["pf"] or 0)),
    )

    all_rosters = {}
    rostered = set()
    for r in rosters:
        m = by_rid.get(r["roster_id"], {})
        starters = set(m.get("starters") or [])
        ids = m.get("players") or r.get("players") or []
        rostered.update(ids)
        all_rosters[team_of[r["roster_id"]]] = {
            "is_me": r["roster_id"] == me["roster_id"],
            "players": [dict(brief(P, i, season_stats, stat_key), starting=i in starters) for i in ids],
        }

    my_m = by_rid.get(me["roster_id"], {})
    opp_m = next((m for m in matchups if m["matchup_id"] == my_m.get("matchup_id") and m["roster_id"] != me["roster_id"]), None)
    opponent = None
    if opp_m:
        opponent = {
            "team": team_of[opp_m["roster_id"]],
            "starters": [brief(P, i, season_stats, stat_key) for i in opp_m.get("starters") or []],
        }

    # top 10 by position with owners (scored the way THIS league scores)
    owner_of = {}
    for r in rosters:
        for i in (by_rid.get(r["roster_id"], {}).get("players") or r.get("players") or []):
            owner_of[i] = team_of[r["roster_id"]]
    top10, free_agents = {}, {}
    for pos in POSITIONS:
        if pos == "DEF":
            continue
        ranked = sorted(
            (i for i, st in season_stats.items() if P.get(i, {}).get("position") == pos and st.get(stat_key) and st.get("gp")),
            key=lambda i: -season_stats[i][stat_key],
        )
        top10[pos] = [dict(brief(P, i, season_stats, stat_key), owner=owner_of.get(i, "FREE AGENT")) for i in ranked[:10]]
        free_agents[pos] = [brief(P, i, season_stats, stat_key) for i in ranked if i not in rostered][:15]
    free_agents["DEF"] = [{"id": i, "team": i} for i, p in P.items() if p.get("position") == "DEF" and i not in rostered and p.get("team")]

    transactions = []
    for w in range(max(1, week - 2), week + 1):
        for t in get(f"/league/{lid}/transactions/{w}"):
            transactions.append(
                {
                    "week": w,
                    "type": t["type"],
                    "status": t["status"],
                    "adds": {P.get(i, {}).get("full_name", i): team_of.get(r) for i, r in (t.get("adds") or {}).items()},
                    "drops": {P.get(i, {}).get("full_name", i): team_of.get(r) for i, r in (t.get("drops") or {}).items()},
                    "draft_picks": t.get("draft_picks"),
                }
            )

    return {
        "name": cfg["name"],
        "league_id": lid,
        "scoring_stat_key": stat_key,
        "roster_positions": league.get("roster_positions"),
        "scoring_settings": league.get("scoring_settings"),
        "my_team": team_of[me["roster_id"]],
        "my_roster_id": me["roster_id"],
        "standings": standings,
        "opponent": opponent,
        "rosters": all_rosters,
        "top10_by_position": top10,
        "free_agents": free_agents,
        "traded_picks": get(f"/league/{lid}/traded_picks"),
        "transactions": transactions,
        "trending_adds": [brief(P, t["player_id"]) | {"adds_24h": t["count"]} for t in trending[:25]],
        "lineup_hq": {
            "lineup": app_json(f"/api/leagues/{lid}/lineup"),
            "recommendations": app_json(f"/api/leagues/{lid}/recommendations"),
            "matchups": app_json(f"/api/leagues/{lid}/matchups"),
            "drop_candidates": app_json(f"/api/leagues/{lid}/drop-candidates"),
            "team_needs": app_json(f"/api/leagues/{lid}/team-needs"),
        },
    }


def stamp(msg):
    print(f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}", flush=True)


def main():
    stamp("snapshot starting")
    state = get("/state/nfl")
    season, week = state["season"], state["week"]
    uid = get(f"/user/{USERNAME}")["user_id"]
    P = players_dict()
    season_stats = get(f"/stats/nfl/regular/{season}")
    trending = get("/players/nfl/trending/add?lookback_hours=48&limit=40")

    snap = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "season": season,
        "week": week,
        "leagues": [],
    }
    for lid, cfg in LEAGUES.items():
        snap["leagues"].append(build_league(lid, cfg, P, season, week, uid, season_stats, trending))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snap, indent=1)
    (OUT_DIR / "latest.json").write_text(text)
    (OUT_DIR / f"{season}-week{week:02d}.json").write_text(text)
    stamp(f"wrote {OUT_DIR / 'latest.json'} ({len(text) // 1024} KB), sleeper week {week}")

    repo = os.environ.get("DATA_REPO_DIR")
    if repo:
        dest = Path(repo) / "snapshots"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "latest.json").write_text(text)
        (dest / f"{season}-week{week:02d}.json").write_text(text)
        run = lambda *a: subprocess.run(a, cwd=repo, check=True)
        run("git", "add", "-A")
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
        if diff.returncode != 0:
            run("git", "commit", "-m", f"snapshot week {week}")
            run("git", "push")
            stamp("pushed to data repo")
        else:
            stamp("no changes to push")


if __name__ == "__main__":
    sys.exit(main())
