"""
Thin wrapper around FantasyCalc's public (unofficial, unauthenticated)
API - an independent, free valuation source, separate from Sleeper.
Used as a second opinion alongside recent on-field performance.

No API key needed. No official rate limit is published, so we cache
results in memory for the life of the process and refresh hourly,
same approach as the Sleeper player list.
"""
import time

import requests

_session = requests.Session()
_cache = {}  # cache key -> (fetched_at, {sleeper_id: {"value": int, "overall_rank": int}})
_TTL_SECONDS = 3600


def get_values_by_sleeper_id(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool = False) -> dict:
    cache_key = (num_qbs, num_teams, ppr, is_dynasty)
    now = time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < _TTL_SECONDS:
        return _cache[cache_key][1]

    url = "https://api.fantasycalc.com/values/current"
    params = {
        "isDynasty": str(is_dynasty).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
    }
    try:
        resp = _session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # FantasyCalc is an unofficial, best-effort source - if it's
        # down or its shape changed, we degrade gracefully rather than
        # breaking the whole recommendations endpoint.
        return {}

    by_sleeper_id = {}
    for entry in data:
        player = entry.get("player", {})
        sleeper_id = player.get("sleeperId")
        if sleeper_id:
            by_sleeper_id[str(sleeper_id)] = {
                "value": entry.get("value"),
                "overall_rank": entry.get("overallRank"),
            }

    _cache[cache_key] = (now, by_sleeper_id)
    return by_sleeper_id
