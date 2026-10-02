"""
Tracks keeper eligibility under DickMelt Fantasies' own house rule:
a player keeps their keeper spot for as long as they've never been
dropped to the waiver wire or picked up as a free agent - it doesn't
matter whether they got onto a roster via the original draft or a
trade. Touch waivers once (as an add OR a drop), and that eligibility
is gone for good, even if the same team re-acquires them later.

That means we can't just look at this season - we have to check the
league's full history, since Sleeper chains a dynasty/keeper league's
past seasons together via `previous_league_id`. Results are cached
for a day: history from a finished week never changes.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sleeper_client as sleeper

_cache = {}  # league_id -> (fetched_at, {player_ids that have touched waivers})
_TTL_SECONDS = 24 * 3600

# Sleeper transactions are grouped by week ("round"), starting at 0
# (preseason/offseason moves) and rarely going past 18. A few extra
# empty weeks cost nothing - the API just returns an empty list.
MAX_ROUNDS_PER_SEASON = 19

# This scan can mean dozens of API calls across a multi-season dynasty
# history, so results are also cached to disk - a 24-hour in-memory
# cache gets wiped every time the container restarts/redeploys, and a
# full scan is slow enough that you shouldn't have to pay for it every
# time we ship an unrelated change.
_CACHE_DIR = Path("/app/data")


def _disk_cache_path(league_id: str) -> Path:
    return _CACHE_DIR / f"keeper_touched_{league_id}.json"


def _load_disk_cache(league_id: str):
    path = _disk_cache_path(league_id)
    if not path.exists():
        return None
    age_seconds = time.time() - path.stat().st_mtime
    if age_seconds >= _TTL_SECONDS:
        return None
    try:
        with open(path) as f:
            return set(json.load(f))
    except Exception:
        return None


def _save_disk_cache(league_id: str, touched: set):
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_disk_cache_path(league_id), "w") as f:
            json.dump(sorted(touched), f)
    except Exception:
        pass  # best-effort - a failed write just means we re-scan next time


def _league_lineage(league_id: str) -> list:
    """This league plus every prior season, oldest first."""
    lineage = []
    current_id = league_id
    seen = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        lineage.append(current_id)
        try:
            info = sleeper.get_league(current_id)
        except Exception:
            break
        current_id = info.get("previous_league_id")
    return list(reversed(lineage))


def _fetch_round(task):
    league_id, week = task
    try:
        return sleeper.get_transactions(league_id, week)
    except Exception:
        return []


def _ever_touched_waivers(league_id: str) -> set:
    lineage = _league_lineage(league_id)
    tasks = [(lid, week) for lid in lineage for week in range(MAX_ROUNDS_PER_SEASON)]

    touched = set()
    # Dozens of independent GET requests - fetching them one at a time
    # is what made this slow. They don't depend on each other, so we
    # fire them in parallel instead of waiting on each round-trip.
    with ThreadPoolExecutor(max_workers=10) as pool:
        for transactions in pool.map(_fetch_round, tasks):
            for tx in transactions or []:
                if tx.get("type") not in ("waiver", "free_agent"):
                    continue
                if tx.get("status") != "complete":
                    continue
                for side in ("adds", "drops"):
                    players = tx.get(side) or {}
                    touched.update(players.keys())
    return touched


def get_keeper_eligible_ids(league_id: str, rostered_ids) -> set:
    """Of the given currently-rostered player ids, which are still
    keeper-eligible (never touched waivers anywhere in this league's
    history, across all seasons)?"""
    now = time.time()
    cached = _cache.get(league_id)
    if cached and (now - cached[0]) < _TTL_SECONDS:
        touched = cached[1]
    else:
        touched = _load_disk_cache(league_id)
        if touched is None:
            touched = _ever_touched_waivers(league_id)
            _save_disk_cache(league_id, touched)
        _cache[league_id] = (now, touched)

    return {pid for pid in rostered_ids if pid not in touched}
