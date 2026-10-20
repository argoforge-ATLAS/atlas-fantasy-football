"""
Thin wrapper around ESPN's public (unofficial, unauthenticated)
scoreboard API - used only to know which real NFL team plays which
opponent in a given week. No API key needed, no official rate limit,
so results are cached in memory for a few hours.
"""
import time

import requests

_session = requests.Session()
_cache = {}  # (season, week) -> (fetched_at, {team_abbr: opponent_abbr})
_TTL_SECONDS = 3600 * 6

# Sleeper and ESPN occasionally use different abbreviations for the
# same team. Map ESPN's abbreviation -> Sleeper's, so a player's
# Sleeper `team` field matches what this schedule returns.
ESPN_TO_SLEEPER_ABBR = {
    "WSH": "WAS",
}


def get_week_schedule(season: str, week: int) -> dict:
    """Returns {team_abbr: opponent_abbr} for every game in that week,
    using Sleeper-style team abbreviations. Returns {} if the week
    hasn't been scheduled yet or ESPN is unreachable - callers should
    degrade gracefully, same as the FantasyCalc client."""
    cache_key = (season, week)
    now = time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < _TTL_SECONDS:
        return _cache[cache_key][1]

    url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    params = {"week": week, "seasontype": 2, "year": season}
    try:
        resp = _session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    schedule = {}
    for event in data.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        if len(competitors) != 2:
            continue
        abbrs = []
        for c in competitors:
            abbr = (c.get("team") or {}).get("abbreviation")
            abbr = ESPN_TO_SLEEPER_ABBR.get(abbr, abbr)
            abbrs.append(abbr)
        if len(abbrs) == 2 and all(abbrs):
            schedule[abbrs[0]] = abbrs[1]
            schedule[abbrs[1]] = abbrs[0]

    _cache[cache_key] = (now, schedule)
    return schedule
