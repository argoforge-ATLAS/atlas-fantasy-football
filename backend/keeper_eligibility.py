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
import time

import sleeper_client as sleeper

_cache = {}  # league_id -> (fetched_at, {player_ids that have touched waivers})
_TTL_SECONDS = 24 * 3600

# Sleeper transactions are grouped by week ("round"), starting at 0
# (preseason/offseason moves) and rarely going past 18. A few extra
# empty weeks cost nothing - the API just returns an empty list.
MAX_ROUNDS_PER_SEASON = 19


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


def _ever_touched_waivers(league_id: str) -> set:
    touched = set()
    for season_league_id in _league_lineage(league_id):
        for week in range(0, MAX_ROUNDS_PER_SEASON):
            try:
                transactions = sleeper.get_transactions(season_league_id, week)
            except Exception:
                continue
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
        touched = _ever_touched_waivers(league_id)
        _cache[league_id] = (now, touched)

    return {pid for pid in rostered_ids if pid not in touched}
