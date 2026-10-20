"""
Long-term value + trade math.

Everything here is pure logic over dicts (no network calls), so it can
be unit tested and reused by the drop, waiver and trade endpoints.

Three ideas, all aimed at "weigh long-term value, not just this week":

  1. Dynasty awareness - keeper/dynasty leagues use FantasyCalc's
     dynasty values (which already bake age and long-term outlook in),
     redraft leagues use redraft values.
  2. A hold test for drops - young + injured, or real dynasty market
     value, means HOLD no matter how bad last week's box score was.
  3. Trade math - live-market pick values, consolidation weighting
     (2-for-1 favors whoever gets the best player), and a per-team
     "tax" (e.g. Kansas City players cost extra in DickMelt).
"""
import re

from config import (
    PACKAGE_WEIGHTS,
    PICK_RANK_BY_ROUND,
    PICK_RANK_LATE,
    TRADE_TAX,
    DYNASTY_OVERRIDES,
)

# ---------------------------------------------------------------- dynasty

def is_dynasty_league(league_info: dict) -> bool:
    """FantasyCalc dynasty vs. redraft values for this league.
    Override in config.DYNASTY_OVERRIDES, else infer from Sleeper's own
    league type: 0 = redraft, 1 = keeper, 2 = dynasty."""
    name = league_info.get("name")
    if name in DYNASTY_OVERRIDES:
        return bool(DYNASTY_OVERRIDES[name])
    league_type = (league_info.get("settings") or {}).get("type")
    return (league_type or 0) >= 1


# ---------------------------------------------------------------- age

# (max_age_inclusive, multiplier) steps, youngest first. These are a
# rough, conventional dynasty age curve per position - a *lens* for
# hold/drop decisions and trade targeting, not a replacement for
# FantasyCalc (whose dynasty values already include age).
_AGE_CURVES = {
    "QB": [(27, 1.0), (33, 0.97), (36, 0.85), (99, 0.65)],
    "RB": [(23, 1.05), (25, 1.0), (26, 0.92), (27, 0.84), (28, 0.72), (99, 0.55)],
    "WR": [(24, 1.05), (27, 1.0), (28, 0.93), (29, 0.84), (30, 0.72), (99, 0.55)],
    "TE": [(25, 1.03), (29, 1.0), (30, 0.9), (31, 0.78), (99, 0.65)],
}

# Five-ish years of useful runway is where a player stops being a
# "young asset" and starts being a "win-now piece".
YOUNG_AGE = 25


def age_factor(position: str, age) -> float:
    curve = _AGE_CURVES.get(position)
    if not curve or not age:
        return 1.0
    for max_age, mult in curve:
        if age <= max_age:
            return mult
    return curve[-1][1]


def age_outlook(position: str, age) -> str:
    """Plain-language label for the table."""
    if not age or position not in _AGE_CURVES:
        return ""
    f = age_factor(position, age)
    if f >= 1.03:
        return "Rising"
    if f >= 0.97:
        return "Prime"
    if f >= 0.84:
        return "Aging"
    return "Declining"


# ---------------------------------------------------------------- hold test

# In dynasty-type leagues, anyone on (or inside) the top N of the live
# FantasyCalc board has real trade/keeper value and shouldn't be cut
# for a marginal waiver upgrade.
DYNASTY_HOLD_RANK = 150


def board_floor(fc_values: dict, rank: int = DYNASTY_HOLD_RANK):
    """FantasyCalc value of the player sitting at `rank` on the live
    board. Anything at or above this is a hold in dynasty leagues."""
    vals = sorted((v["value"] for v in fc_values.values() if v.get("value")), reverse=True)
    if not vals:
        return None
    return vals[min(rank, len(vals)) - 1]


def hold_reason(player: dict, dynasty: bool, floor):
    """Return a short reason string if this player should be HELD
    regardless of recent production, else None.

    The rule you set: don't drop young injured players for short-term
    gains. Anyone young with ANY injury designation is held, as is
    anyone with real dynasty market value."""
    age = player.get("age")
    injured = bool(player.get("injury_status"))
    if age and age <= YOUNG_AGE and injured:
        return f"Young ({age}) & {player['injury_status']} - hold"
    fc = player.get("fantasycalc_value")
    if dynasty and floor is not None and fc is not None and fc >= floor:
        return "Dynasty value - hold"
    if dynasty and age and age <= 23 and (player.get("years_exp") or 0) <= 1:
        return "Young upside - hold"
    return None


def keep_score(player: dict, dynasty: bool) -> float:
    """Higher = more worth keeping. Used to pick WHICH player to cut
    when an add needs a drop. Dynasty leagues lean on market value;
    redraft leagues lean on expected points."""
    expected = player.get("expected_points") or 0.0
    fc = player.get("fantasycalc_value") or 0.0
    youth = age_factor(player.get("position"), player.get("age")) if dynasty else 1.0
    if dynasty:
        # FantasyCalc values run ~0-10000; scale to roughly "points".
        return (fc / 400.0) * youth + expected * 0.5
    return expected + (fc / 800.0)


# ---------------------------------------------------------------- picks

_PICK_RE = re.compile(r"^\s*(?:(\d{4})\s*)?(?:r|rd|round)?\s*(\d)(?:st|nd|rd|th)?\s*(?:round|rd)?\s*(?:pick)?\s*$", re.I)


def parse_pick(text: str):
    """'2027 R1', '2027 1st', 'round 2' -> (season or None, round).
    Returns None if the text isn't a pick."""
    if not text:
        return None
    m = _PICK_RE.match(text.strip())
    if not m:
        return None
    season = int(m.group(1)) if m.group(1) else None
    rnd = int(m.group(2))
    if not 1 <= rnd <= 9:
        return None
    return season, rnd


def pick_value(round_: int, fc_values: dict) -> float:
    """What a pick is worth, read off the LIVE board: a 1st is worth
    about as much as the 18th-most-valuable player right now, etc.
    Because it floats with the market, picks get more valuable when
    the board is top-heavy and cheaper when it's flat."""
    vals = sorted((v["value"] for v in fc_values.values() if v.get("value")), reverse=True)
    if not vals:
        return 0.0
    rank = PICK_RANK_BY_ROUND.get(round_, PICK_RANK_LATE)
    return float(vals[min(rank, len(vals)) - 1])


# ---------------------------------------------------------------- trades

def package_value(values: list) -> float:
    """Consolidation-weighted package total: the best asset counts
    fully, each additional asset counts less (see PACKAGE_WEIGHTS)."""
    ordered = sorted((v for v in values if v), reverse=True)
    total = 0.0
    for i, v in enumerate(ordered):
        w = PACKAGE_WEIGHTS[i] if i < len(PACKAGE_WEIGHTS) else PACKAGE_WEIGHTS[-1]
        total += v * w
    return total


def team_tax(league_name: str, team: str) -> float:
    return (TRADE_TAX.get(league_name) or {}).get(team, 1.0)


def evaluate_trade(give: list, get: list, league_name: str) -> dict:
    """
    give / get: lists of asset dicts, each:
        {"label": str, "value": float|None, "team": str|None, "kind": "player"|"pick",
         "round": int|None}

    Two separate questions, because a team "tax" is a PRICE, not extra
    value:
      * market edge - on live market value, who wins? (edge_pct, verdict)
      * will they take it - the other owner wants a premium for their
        own players (Kansas City players in DickMelt carry 15%), so the
        package you send has to clear the TAXED price of what you get
        (likely_accepted / ask_more).
    """
    give_rows, get_rows = [], []
    for a in give:
        give_rows.append({**a, "adjusted": a.get("value") or 0.0, "tax": 1.0})
    for a in get:
        tax = team_tax(league_name, a.get("team")) if a.get("kind") == "player" else 1.0
        get_rows.append({**a, "adjusted": (a.get("value") or 0.0) * tax, "tax": tax})

    give_total = package_value([r["adjusted"] for r in give_rows])
    get_market_total = package_value([a.get("value") or 0.0 for a in get_rows])
    get_price_total = package_value([r["adjusted"] for r in get_rows])

    if give_total <= 0 and get_market_total <= 0:
        verdict, edge_pct = "No data", 0.0
    else:
        base = max(give_total, get_market_total)
        edge_pct = round((get_market_total - give_total) / base * 100, 1) if base else 0.0
        if edge_pct >= 10:
            verdict = "You win this trade"
        elif edge_pct >= -10:
            verdict = "Roughly fair"
        else:
            verdict = "You lose this trade"

    # 3% slack so a dead-even offer isn't called short over rounding.
    likely_accepted = give_total >= get_price_total * 0.97
    ask_more = max(get_price_total - give_total, 0.0)

    return {
        "give": give_rows,
        "get": get_rows,
        "give_total": round(give_total),
        "get_total": round(get_market_total),
        "get_price": round(get_price_total),
        "edge_pct": edge_pct,
        "verdict": verdict,
        "likely_accepted": bool(likely_accepted),
        "ask_more": round(ask_more),
    }


def suggest_package(target_price: float, assets: list, max_assets: int = 3):
    """
    Cheapest package of YOUR tradeable assets that covers `target_price`
    under consolidation weighting, trying 1 asset, then 2, then 3.
    `assets`: [{"label", "value"}] already filtered for untouchables.
    Returns (labels, package_total) or (None, None).

    Prefers the smallest package that clears the price by the least
    margin, so you overpay as little as possible.
    """
    from itertools import combinations

    pool = sorted((a for a in assets if (a.get("value") or 0) > 0), key=lambda a: a["value"], reverse=True)[:12]
    best = None
    for size in range(1, max_assets + 1):
        for combo in combinations(pool, size):
            total = package_value([a["value"] for a in combo])
            if total >= target_price:
                overpay = total - target_price
                if best is None or overpay < best[0]:
                    best = (overpay, [a["label"] for a in combo], total)
        if best is not None:
            break  # fewest assets that works wins
    if best is None:
        return None, None
    return best[1], round(best[2])
