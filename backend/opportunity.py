"""
Opportunity signals - target share (WR/TE) and carry share (RB) -
computed from the same weekly stat lines we already pull for scoring.
Not a new data source, just new math on data we already have.

Why this matters: raw trailing fantasy points can't tell "this guy's
role evaporated" apart from "this guy's getting real chances but
hasn't converted yet." A rookie WR with rising target share but a few
quiet games is a stash, not a cut - the opposite of someone scoring
the same points on a shrinking role. Share of the TEAM's own targets/
carries is the standard way fantasy analysts separate those two
cases, and we can compute it ourselves from Sleeper's own stat lines.
"""
import time

import sleeper_client as sleeper

_cache = {}  # (season, week) -> (fetched_at, {team: {"targets": n, "carries": n}})
_TTL_SECONDS = 3600


def _team_totals_for_week(season: str, week: int) -> dict:
    cache_key = (season, week)
    now = time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < _TTL_SECONDS:
        return _cache[cache_key][1]

    players = sleeper.get_players_cached()
    week_stats = sleeper.get_week_stats(season, week)

    totals = {}
    for pid, stats in (week_stats or {}).items():
        p = players.get(pid)
        team = p.get("team") if p else None
        if not team:
            continue
        bucket = totals.setdefault(team, {"targets": 0, "carries": 0})
        bucket["targets"] += stats.get("rec_tgt", 0) or 0
        bucket["carries"] += stats.get("rush_att", 0) or 0

    _cache[cache_key] = (now, totals)
    return totals


def _player_week_shares(player_id: str, team: str, season: str, week: int):
    """(target_share, carry_share) for one player in one week, as
    fractions of their team's total that week. None where we don't
    have enough to compute it (bye week, team had zero attempts, etc.)."""
    if not team:
        return None, None
    week_stats = sleeper.get_week_stats(season, week)
    stats = (week_stats or {}).get(player_id)
    if not stats:
        return None, None

    team_totals = _team_totals_for_week(season, week).get(team, {"targets": 0, "carries": 0})

    target_share = None
    if team_totals["targets"] > 0:
        target_share = (stats.get("rec_tgt", 0) or 0) / team_totals["targets"]

    carry_share = None
    if team_totals["carries"] > 0:
        carry_share = (stats.get("rush_att", 0) or 0) / team_totals["carries"]

    return target_share, carry_share


def opportunity_trend(player_id: str, team: str, position: str, season: str, weeks: list):
    """
    Looks at target share (WR/TE) or carry share (RB) over the given
    weeks (oldest to newest) and returns (latest_share_pct, trend),
    where trend is "Rising" / "Steady" / "Falling", or (None, None) if
    there isn't enough data to call it. Not meaningful for QB/DEF -
    callers should only ask for RB/WR/TE.
    """
    if not weeks or not team:
        return None, None

    points = []
    for week in weeks:
        target_share, carry_share = _player_week_shares(player_id, team, season, week)
        if position == "RB":
            share = carry_share if carry_share is not None else target_share
        elif position in ("WR", "TE"):
            share = target_share
        else:
            share = None
        if share is not None:
            points.append(share)

    if not points:
        return None, None

    latest_pct = round(points[-1] * 100, 1)
    if len(points) < 2:
        return latest_pct, None

    diff = points[-1] - points[0]
    if diff >= 0.05:
        trend = "Rising"
    elif diff <= -0.05:
        trend = "Falling"
    else:
        trend = "Steady"
    return latest_pct, trend
