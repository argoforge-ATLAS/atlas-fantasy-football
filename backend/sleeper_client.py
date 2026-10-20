"""
Thin wrapper around the public, read-only Sleeper API.
No API key is needed - Sleeper's API is open for read access.
Docs: https://docs.sleeper.com/
"""
import json
import os
import threading
import time
from pathlib import Path

import requests

from config import SLEEPER_API_BASE, PLAYER_CACHE_HOURS, PLAYER_CACHE_PATH

_session = requests.Session()

# Short-lived in-process cache for read-only API calls. The page fires
# several endpoints at once and each one used to re-fetch the same
# league/roster/stat data; a few seconds of caching collapses that
# into one request per resource without making anything stale enough
# to matter (live scoring uses its own, shorter TTL).
_http_cache = {}  # (path) -> (fetched_at, data)
_http_lock = threading.Lock()


def _get(path: str, ttl: float = 0, base: str = None):
    url = f"{base or SLEEPER_API_BASE}{path}"
    key = url
    now = time.time()
    if ttl:
        with _http_lock:
            hit = _http_cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    resp = _session.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if ttl:
        with _http_lock:
            _http_cache[key] = (now, data)
    return data


def get_nfl_state():
    """Current NFL season/week info."""
    return _get("/state/nfl", ttl=60)


def get_user_id(username: str) -> str:
    data = _get(f"/user/{username}", ttl=24 * 3600)
    return data["user_id"]


def get_league(league_id: str):
    return _get(f"/league/{league_id}", ttl=60)


def get_rosters(league_id: str):
    return _get(f"/league/{league_id}/rosters", ttl=30)


def get_league_users(league_id: str):
    return _get(f"/league/{league_id}/users", ttl=300)


def get_matchups(league_id: str, week: int, ttl: float = 15):
    """Both sides of every head-to-head in the league for one week,
    with live points. Short TTL so a polling overlay stays fresh
    without hammering Sleeper."""
    return _get(f"/league/{league_id}/matchups/{week}", ttl=ttl)


def get_traded_picks(league_id: str):
    """Draft picks that have changed hands in this league."""
    return _get(f"/league/{league_id}/traded_picks", ttl=300)


def _normalize_projections(raw) -> dict:
    """Sleeper has served weekly projections in two shapes: a list of
    {"player_id": ..., "stats": {...}} records, and a dict keyed by
    player_id (stat line directly, or wrapped in "stats"). Accept both
    and return {player_id: raw_stat_line}."""
    out = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("player_id") and isinstance(entry.get("stats"), dict):
                out[str(entry["player_id"])] = entry["stats"]
    elif isinstance(raw, dict):
        for pid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else entry
            out[str(pid)] = stats
    return out


def get_projections(season: str, week: int) -> dict:
    """
    Sleeper's own weekly projections as {player_id: raw stat line}
    (same shape as get_week_stats, so the same league scoring rules
    apply). This endpoint lives on sleeper.com and is not part of the
    documented API, so any failure or surprise shape degrades to {}
    and callers fall back to recent production.
    """
    try:
        raw = _get(
            f"/projections/nfl/{season}/{week}?season_type=regular"
            "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=DEF",
            ttl=1800,
            base="https://api.sleeper.com",
        )
        return _normalize_projections(raw)
    except Exception:
        return {}


def get_trending_adds(lookback_hours: int = 24, limit: int = 50):
    """Players being added across all of Sleeper right now - a free,
    real-time signal for 'who's hot on waivers'."""
    return _get(f"/players/nfl/trending/add?lookback_hours={lookback_hours}&limit={limit}", ttl=600)


def get_transactions(league_id: str, round_week: int):
    """All waiver/free-agent/trade transactions for one week ('round')
    of a league - used to figure out which players have ever touched
    the waiver wire."""
    return _get(f"/league/{league_id}/transactions/{round_week}", ttl=300)


def get_week_stats(season: str, week: int, completed: bool = True) -> dict:
    """
    Raw stat totals for every player for one completed week, e.g.
    {"4046": {"pass_yd": 275, "pass_td": 2, ...}, ...}.
    Used to compute real fantasy points under a league's own scoring
    rules, rather than relying on generic rankings from elsewhere.
    """
    # A completed week's stat lines are the largest repeated download
    # in the app (every player, every stat). Cache for an hour; the
    # in-progress week uses a shorter TTL so live scoring stays fresh.
    return _get(f"/stats/nfl/regular/{season}/{week}", ttl=3600 if completed else 60)


_players_mem = {"loaded_at": 0.0, "data": None}
_players_lock = threading.Lock()


def get_players_cached() -> dict:
    """
    The full NFL player dictionary is a multi-MB file that rarely
    changes. Sleeper asks API users not to pull it more than once a
    day, so we cache it to disk and only refresh when stale.
    """
    with _players_lock:
        # Parsing a multi-MB JSON file on every request was the other
        # big repeated cost; keep the parsed dict in memory too.
        if _players_mem["data"] is not None and (time.time() - _players_mem["loaded_at"]) < 3600:
            return _players_mem["data"]

        cache_path = Path(PLAYER_CACHE_PATH)
        players = None
        if cache_path.exists():
            age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
            if age_hours < PLAYER_CACHE_HOURS:
                with open(cache_path) as f:
                    players = json.load(f)

        if players is None:
            players = _get("/players/nfl")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(players, f)

        _players_mem["data"] = players
        _players_mem["loaded_at"] = time.time()
        return players
