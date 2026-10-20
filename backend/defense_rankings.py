"""
How tough or easy each real NFL defense has been against each
offensive position, using the SAME stat lines and the SAME league
scoring rules as the rest of the app - not a generic outside ranking
pulled from some other website.

Method: for each of the last few completed weeks, look at every
player's actual stat line, figure out who their opponent was (via
nfl_schedule), score it with this league's own scoring rules, and
add it to that opponent's total "points allowed" at that position
for that week. A defense's rating is its average weekly total over
the games it actually played. Teams are ranked 1 (fewest points
allowed - toughest matchup) to 32 (most allowed - easiest matchup).
"""
import time

import sleeper_client as sleeper
import scoring
import nfl_schedule

RELEVANT_POSITIONS = ["QB", "RB", "WR", "TE", "DEF"]

_cache = {}  # cache key -> (fetched_at, {team_abbr: {position: {rank, out_of, avg_allowed}}})
_TTL_SECONDS = 3600


def _cache_key(season, weeks, scoring_settings):
    # dicts aren't hashable - use a stable sorted tuple instead
    settings_key = tuple(sorted(scoring_settings.items()))
    return (season, tuple(weeks), settings_key)


def get_defense_rankings(season: str, weeks: list[int], scoring_settings: dict) -> dict:
    if not weeks:
        return {}

    key = _cache_key(season, weeks, scoring_settings)
    now = time.time()
    if key in _cache and (now - _cache[key][0]) < _TTL_SECONDS:
        return _cache[key][1]

    players = sleeper.get_players_cached()
    # (opponent, position, week) -> total points that defense allowed
    # to ALL players at that position that week. A defense's real
    # "points allowed per game" is that weekly total averaged over the
    # weeks it actually played - NOT averaged over individual player
    # stat lines (which rewards defenses that happen to face lots of
    # garbage-time backups and makes them look tougher than they are).
    weekly_totals = {}

    for week in weeks:
        week_stats = sleeper.get_week_stats(season, week)
        schedule = nfl_schedule.get_week_schedule(season, week)
        if not schedule:
            continue
        for player_id, stat_line in week_stats.items():
            p = players.get(player_id)
            if not p:
                continue
            position = p.get("position")
            team = p.get("team")
            if position not in RELEVANT_POSITIONS or not team:
                continue
            opponent = schedule.get(team)
            if not opponent:
                continue
            pts = scoring.score_stat_line(stat_line, scoring_settings)
            wkey = (opponent, position, week)
            weekly_totals[wkey] = weekly_totals.get(wkey, 0) + pts

    totals = {}  # (opponent, position) -> sum of weekly totals
    games = {}   # (opponent, position) -> number of games (weeks) counted
    for (opponent, position, _week), pts in weekly_totals.items():
        tkey = (opponent, position)
        totals[tkey] = totals.get(tkey, 0) + pts
        games[tkey] = games.get(tkey, 0) + 1

    averages = {}
    for tkey, total in totals.items():
        g = games.get(tkey, 0)
        if g:
            averages[tkey] = total / g

    rankings = {}
    for position in RELEVANT_POSITIONS:
        team_avgs = [(team, avg) for (team, pos), avg in averages.items() if pos == position]
        team_avgs.sort(key=lambda x: x[1])  # ascending: fewest points allowed first
        for rank, (team, avg) in enumerate(team_avgs, start=1):
            rankings.setdefault(team, {})[position] = {
                "rank": rank,
                "out_of": len(team_avgs),
                "avg_allowed": round(avg, 2),
            }

    _cache[key] = (now, rankings)
    return rankings


def matchup_label(rank: int, out_of: int) -> str:
    """Bucket a defense's rank against a position into a simple label.
    Low rank = allows fewer points = tough matchup for the offense."""
    if out_of < 6:
        return None  # not enough teams with data yet to call it
    third = out_of / 3
    if rank <= third:
        return "Tough matchup"
    elif rank <= third * 2:
        return "Average matchup"
    else:
        return "Good matchup"
