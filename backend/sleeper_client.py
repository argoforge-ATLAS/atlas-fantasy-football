"""
Thin wrapper around the public, read-only Sleeper API.
No API key is needed - Sleeper's API is open for read access.
Docs: https://docs.sleeper.com/
"""
import json
import os
import time
from pathlib import Path

import requests

from config import SLEEPER_API_BASE, PLAYER_CACHE_HOURS, PLAYER_CACHE_PATH

_session = requests.Session()


def _get(path: str):
    url = f"{SLEEPER_API_BASE}{path}"
    resp = _session.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_nfl_state():
    """Current NFL season/week info."""
    return _get("/state/nfl")


def get_user_id(username: str) -> str:
    data = _get(f"/user/{username}")
    return data["user_id"]


def get_league(league_id: str):
    return _get(f"/league/{league_id}")


def get_rosters(league_id: str):
    return _get(f"/league/{league_id}/rosters")


def get_league_users(league_id: str):
    return _get(f"/league/{league_id}/users")


def get_matchups(league_id: str, week: int):
    return _get(f"/league/{league_id}/matchups/{week}")


def get_players_cached() -> dict:
    """
    The full NFL player dictionary is a multi-MB file that rarely
    changes. Sleeper asks API users not to pull it more than once a
    day, so we cache it to disk and only refresh when stale.
    """
    cache_path = Path(PLAYER_CACHE_PATH)
    if cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < PLAYER_CACHE_HOURS:
            with open(cache_path) as f:
                return json.load(f)

    players = _get("/players/nfl")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(players, f)
    return players
