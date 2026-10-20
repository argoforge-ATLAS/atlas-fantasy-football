"""
Live head-to-head: the thing the app never knew - who you're actually
playing this week, and how the matchup is going right now.

Feeds both the Lineup HQ scoreboard and the TV overlay page
(frontend/overlay.html). The data comes from Sleeper's matchup endpoint
(live points for both sides) plus ESPN game states, with a simple,
transparent win-probability model:

  team_final ~ Normal(points_now + sum(remaining projections),
                      sum(per-player remaining variance))
  P(win) = P(my_final - opp_final > 0)

Each player's remaining projection shrinks as their game progresses;
their variance shrinks with it. A finished game contributes nothing
to either.
"""
import json
import math
import time
import threading
from pathlib import Path

DATA_DIR = Path("/app/data")

# Rough per-player scoring spread for a full game, as a floor and as a
# share of projection. Wide on purpose - fantasy football is noisy.
SD_FLOOR = 4.0
SD_SHARE = 0.55

_history = {}  # (league_id, week) -> [{"t": epoch, "p": win_prob_pct}]
_history_lock = threading.Lock()
_HISTORY_MIN_GAP = 45  # seconds between points unless the value moved
_HISTORY_MAX = 600


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def player_status(team, game_states, on_bye):
    if on_bye:
        return "bye", None
    gs = (game_states or {}).get(team)
    if not gs:
        return "upcoming", None
    return {"pre": "upcoming", "in": "live", "post": "final"}.get(gs["state"], "upcoming"), gs


def side_summary(entry, players, slots, expected_by_pid, game_states, bye_teams):
    """
    entry: one Sleeper matchup record
        {"starters": [...], "starters_points": [...], "players_points": {...}, "points": n}
    slots: slot labels aligned with `starters` (BN removed)
    Returns (summary dict, remaining_mean, remaining_var).
    """
    starters = entry.get("starters") or []
    spts = entry.get("starters_points") or []
    rows = []
    rem_mean = 0.0
    rem_var = 0.0
    for i, pid in enumerate(starters):
        p = players.get(pid) or {}
        team = p.get("team")
        pts = spts[i] if i < len(spts) and spts[i] is not None else (entry.get("players_points") or {}).get(pid, 0.0)
        status, gs = player_status(team, game_states, team in bye_teams)
        expected = expected_by_pid.get(pid)
        if status == "final" or status == "bye":
            remaining = 0.0
        elif status == "live":
            remaining = max((expected or 0.0) * (1 - gs["progress"]), 0.0)
            # A player already above their projection mid-game: assume the
            # rest of the game still has some juice, never negative.
        else:
            remaining = expected or 0.0
        frac_left = 1.0 if status == "upcoming" else (0.0 if status in ("final", "bye") else 1 - gs["progress"])
        sd_full = max(SD_FLOOR, SD_SHARE * (expected or 0.0))
        rem_mean += remaining
        rem_var += (sd_full ** 2) * frac_left

        rows.append({
            "id": pid,
            "slot": slots[i] if i < len(slots) else None,
            "name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid,
            "position": p.get("position"),
            "team": team,
            "points": round(pts or 0.0, 2),
            "expected": round(expected, 1) if expected is not None else None,
            "remaining": round(remaining, 1),
            "status": status,
            "game": gs["detail"] if gs else None,
            "injury_status": p.get("injury_status"),
        })

    total = round(entry.get("points") or sum(r["points"] for r in rows), 2)
    summary = {
        "points": total,
        "projected_final": round(total + rem_mean, 1),
        "players": rows,
        "yet_to_play": sum(1 for r in rows if r["status"] == "upcoming"),
        "playing_now": sum(1 for r in rows if r["status"] == "live"),
        "done": sum(1 for r in rows if r["status"] in ("final", "bye")),
    }
    return summary, rem_mean, rem_var


def win_probability(my_total, my_rem, my_var, opp_total, opp_rem, opp_var) -> float:
    """P(my final > opp final), 0-1."""
    mean_diff = (my_total + my_rem) - (opp_total + opp_rem)
    var = my_var + opp_var
    if var <= 1e-9:
        if mean_diff > 0:
            return 1.0
        if mean_diff < 0:
            return 0.0
        return 0.5
    return _phi(mean_diff / math.sqrt(var))


def need_line(summary, other_summary):
    """'You need X more from Y players' - the sentence a TV overlay
    actually wants. Uses projections for players yet to play."""
    gap = other_summary["projected_final"] - summary["projected_final"]
    left = summary["yet_to_play"] + summary["playing_now"]
    if left == 0:
        return None
    if gap <= 0:
        return f"Projected ahead by {round(-gap, 1)} with {left} still to play"
    return f"Projected behind by {round(gap, 1)} with {left} still to play"


def record_history(league_id, week, win_prob_pct):
    """Append to the in-memory (and best-effort on-disk) win
    probability history that drives the sparkline."""
    key = (league_id, week)
    now = time.time()
    with _history_lock:
        series = _history.get(key)
        if series is None:
            series = _load_history(league_id, week)
            _history[key] = series
        last = series[-1] if series else None
        if last is None or abs(last["p"] - win_prob_pct) >= 0.5 or (now - last["t"]) >= _HISTORY_MIN_GAP:
            series.append({"t": int(now), "p": round(win_prob_pct, 1)})
            if len(series) > _HISTORY_MAX:
                del series[: len(series) - _HISTORY_MAX]
            _save_history(league_id, week, series)
        return list(series)


def _hist_path(league_id, week):
    return DATA_DIR / f"winprob_{league_id}_{week}.json"


def _load_history(league_id, week):
    try:
        with open(_hist_path(league_id, week)) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(league_id, week, series):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_hist_path(league_id, week), "w") as f:
            json.dump(series, f)
    except Exception:
        pass  # history is a nicety; never break the endpoint over it
