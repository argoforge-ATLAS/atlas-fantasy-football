"""
Computes real fantasy points from raw Sleeper stat lines, using a
league's OWN scoring settings - not generic PPR assumptions. This is
the core of "a better set of rules": transparent, deterministic,
tunable Python logic, not an AI guess.
"""
import time
from functools import lru_cache

import sleeper_client as sleeper


def score_stat_line(stats: dict, scoring_settings: dict) -> float:
    """Multiply each raw stat category by this league's point value
    for it, and sum. Unknown/irrelevant stat keys are ignored."""
    if not stats:
        return 0.0
    total = 0.0
    for stat_key, amount in stats.items():
        weight = scoring_settings.get(stat_key)
        if weight:
            total += amount * weight
    return round(total, 2)


class RecentStatsCache:
    """
    Caches the last few completed weeks' raw stats in memory for the
    life of the process, so we don't re-fetch all players' stats on
    every request. Refreshes once per hour.
    """
    def __init__(self, ttl_seconds=3600):
        self._ttl = ttl_seconds
        self._cached_week_stats = {}  # week -> {player_id: stats}
        self._fetched_at = {}

    def _get_week(self, season: str, week: int) -> dict:
        now = time.time()
        if week in self._cached_week_stats and (now - self._fetched_at.get(week, 0)) < self._ttl:
            return self._cached_week_stats[week]
        data = sleeper.get_week_stats(season, week)
        self._cached_week_stats[week] = data
        self._fetched_at[week] = now
        return data

    def recent_points_for_player(self, player_id: str, season: str, completed_weeks: list[int], scoring_settings: dict):
        """Average fantasy points over the given completed weeks,
        under this league's scoring rules. Returns None if the player
        has no stats in any of those weeks (e.g. just signed, rookie
        not yet active, or bye weeks throughout)."""
        points = []
        for week in completed_weeks:
            week_stats = self._get_week(season, week)
            player_stats = week_stats.get(player_id)
            if player_stats:
                points.append(score_stat_line(player_stats, scoring_settings))
        if not points:
            return None
        return round(sum(points) / len(points), 2)


_cache = RecentStatsCache()


def get_recent_weeks(current_week: int, lookback: int = 3) -> list[int]:
    """The last `lookback` COMPLETED weeks (never the current/in-progress one)."""
    start = max(1, current_week - lookback)
    return list(range(start, current_week))


def recent_avg_points(player_id: str, season: str, current_week: int, scoring_settings: dict, lookback: int = 3):
    weeks = get_recent_weeks(current_week, lookback)
    if not weeks:
        return None
    return _cache.recent_points_for_player(player_id, season, weeks, scoring_settings)
