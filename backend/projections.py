"""
Expected points for the week, under THIS league's scoring rules.

Start/sit used to be "who scored more over the last 3 weeks" - purely
backward looking, and blind to the opponent. This module blends:

  * Sleeper's weekly projection for the player (already opponent- and
    role-aware), scored with the league's own rules, and
  * trailing production, adjusted by the defense-vs-position matchup
    when no projection exists (rookies, fresh signings, projection
    endpoint down).

Pure functions over dicts - no network calls here.
"""
import scoring

# Matchup multipliers applied ONLY to trailing production (a
# projection already accounts for the opponent).
MATCHUP_MULT = {
    "Tough matchup": 0.93,
    "Average matchup": 1.0,
    "Good matchup": 1.07,
}

# Weight on the projection when both exist; the remainder goes to
# trailing production. Projections win, but real recent usage
# keeps them honest.
PROJECTION_WEIGHT = 0.65

# Injury designations that shave expected output (still playable).
INJURY_DISCOUNT = {"Questionable": 0.90}


def projected_points(projection_stats: dict, scoring_settings: dict):
    """Score a projected stat line under the league's rules. None if
    there is no projection for the player."""
    if not projection_stats:
        return None
    pts = scoring.score_stat_line(projection_stats, scoring_settings)
    return pts if pts and pts > 0 else None


def blend_expected(projection, recent_avg, matchup_label=None):
    """
    Returns (expected_points, source).
    source is "projection", "blend", "recent" or None.
    """
    mult = MATCHUP_MULT.get(matchup_label, 1.0)
    recent_adj = round(recent_avg * mult, 2) if recent_avg is not None else None

    if projection is not None and recent_adj is not None:
        return round(PROJECTION_WEIGHT * projection + (1 - PROJECTION_WEIGHT) * recent_adj, 2), "blend"
    if projection is not None:
        return round(projection, 2), "projection"
    if recent_adj is not None:
        return recent_adj, "recent"
    return None, None


def effective_points(player: dict):
    """What a player is worth to THIS week's lineup decision: expected
    points, zeroed for Out/Doubtful/IR/bye, discounted for Questionable.
    None means unknown (don't make comparisons on it)."""
    if player.get("bad_injury") or player.get("on_bye"):
        return 0.0
    ex = player.get("expected_points")
    if ex is None:
        return None
    return round(ex * INJURY_DISCOUNT.get(player.get("injury_status"), 1.0), 2)
