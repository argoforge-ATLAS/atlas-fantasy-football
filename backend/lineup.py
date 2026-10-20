"""
Lineup + waiver decision logic. Pure functions over player dicts - no
network calls - so the rules are easy to read, tune and test.

Player dicts are the enriched ones built in main._build_player_pool:
name, id, position, slot (starters only), expected_points,
bad_injury, on_bye, injury_status, fantasycalc_value, age, ...
"""
from projections import effective_points
from valuation import YOUNG_AGE

# A bench player must beat the starter by at least this many expected
# points to be worth a swap (below that it's noise).
SWAP_MARGIN = 1.5

# A waiver add must beat the player it replaces by at least this much
# expected production... or, in dynasty-type leagues, clearly beat them
# on long-term market value.
ADD_MIN_GAIN = 1.0
ADD_MIN_VALUE_RATIO = 1.25

# Never recommend adding someone with these designations this week.
UNADDABLE_STATUSES = {"Out", "Doubtful", "IR", "PUP", "Suspended"}

MAX_WAIVER_ROWS = 5

# Dropping a keeper-eligible player (DickMelt house rule: never having
# touched waivers) forfeits that eligibility for good. Only pair an add
# with such a drop when the upgrade is large enough to justify it.
KEEPER_BURN_MIN_GAIN = 4.0


# ------------------------------------------------------------ start / sit

def optimize_swaps(starters: list, bench: list, slot_eligibility: dict, max_swaps: int = 8):
    """
    Repeatedly apply the single best bench-for-starter swap until none
    clears the margin. Greedy per-slot matching (the old approach) can
    miss lineups where bumping an RB into FLEX frees a slot; picking
    the globally best swap each round does not.

    Must-sit starters (Out/Doubtful/IR/bye) are valued at 0, so any
    healthy eligible bench player replaces them. A must-sit starter
    nobody can replace is still reported (with start=None) so the
    problem is never silently hidden.
    """
    lineup = [dict(s) for s in starters]
    pool = [dict(b) for b in bench]
    suggestions = []

    for _ in range(max_swaps):
        best = None  # (rank, gain, slot_index, bench_index)
        for si, starter in enumerate(lineup):
            s_val = effective_points(starter)
            eligible = slot_eligibility.get(starter.get("slot"), {starter.get("position")})
            must_sit = starter.get("bad_injury") or starter.get("on_bye")
            for bi, cand in enumerate(pool):
                if cand.get("position") not in eligible:
                    continue
                if cand.get("bad_injury") or cand.get("on_bye"):
                    continue
                c_val = effective_points(cand)
                if must_sit:
                    # Anyone healthy beats an Out/bye starter, even a
                    # player we have no projection for. Must-sits are
                    # handled before point-based upgrades.
                    gain = c_val or 0.0
                    rank = 1000.0 + gain
                else:
                    if c_val is None or s_val is None:
                        continue  # can't compare against an unknown
                    gain = c_val - s_val
                    if gain <= SWAP_MARGIN:
                        continue
                    rank = gain
                if best is None or rank > best[0]:
                    best = (rank, gain, si, bi)
        if best is None:
            break
        _rank, gain, si, bi = best
        starter, cand = lineup[si], pool[bi]
        if starter.get("on_bye"):
            reason = f"{starter['name']} is on a bye this week"
        elif starter.get("bad_injury"):
            reason = f"{starter['name']} is {starter['injury_status']}"
        else:
            reason = (
                f"{cand['name']} projects {effective_points(cand)} vs "
                f"{starter['name']} {effective_points(starter)} (+{round(gain, 1)})"
            )
        suggestions.append({
            "slot": starter.get("slot"),
            "sit": starter["name"],
            "start": cand["name"],
            "gain": round(gain, 1),
            "reason": reason,
        })
        # Apply: candidate takes the slot, displaced starter goes to the bench
        # and may re-enter a different slot on the next pass.
        cand["slot"] = starter.get("slot")
        lineup[si] = cand
        starter = dict(starter)
        starter.pop("slot", None)
        pool[bi] = starter

    # Must-sit starters that no one could replace.
    swapped_slots_names = {s["sit"] for s in suggestions}
    for starter in lineup:
        if (starter.get("bad_injury") or starter.get("on_bye")) and starter["name"] not in swapped_slots_names:
            problem = "on a bye this week" if starter.get("on_bye") else starter.get("injury_status")
            suggestions.append({
                "slot": starter.get("slot"),
                "sit": starter["name"],
                "start": None,
                "gain": None,
                "reason": f"{starter['name']} is {problem}, and no one on your bench can fill the {starter.get('slot')} slot - check waivers",
            })
    return suggestions


# ------------------------------------------------------------ waivers

def add_score(p: dict, dynasty: bool) -> float:
    """Ranking score for a waiver add: expected points, plus a small
    long-term bump in dynasty-type leagues, plus a tiebreak on how many
    teams are adding right now."""
    expected = p.get("expected_points") or 0.0
    fc = p.get("fantasycalc_value") or 0.0
    bump = (fc / 800.0) if dynasty else (fc / 1600.0)
    return expected + bump + min(p.get("add_count_48h", 0), 5000) / 5000.0


def open_slot_positions(swap_suggestions: list) -> set:
    """Positions with a starter slot nobody on the bench can fill (a
    must-sit starter with no healthy replacement). An add at one of these
    positions fixes a hole in this week's lineup, so it jumps the queue."""
    plain = {"QB", "RB", "WR", "TE", "K", "DEF"}
    return {s["slot"] for s in swap_suggestions if s.get("start") is None and s.get("slot") in plain}


def build_waiver_plan(adds: list, drops: list, position_counts: dict, caps: dict, dynasty: bool,
                      needed_positions=frozenset(), exclude_add_ids=frozenset()):
    """
    Claim-ordered list of (add, drop) pairs. Each add is paired with
    the best player to cut for it:
      * never a player held for long-term value (those aren't in
        `drops` - only "Drop candidate" verdicts are passed in),
      * keeper-eligible players only as a last resort, only for a big
        upgrade (KEEPER_BURN_MIN_GAIN), and always flagged,
      * a same-position drop is preferred (roster stays balanced),
      * if the roster is at its position cap (e.g. 3 QBs), the drop
        must be at the SAME position,
      * each drop is used once.
    Rows are ordered by total gain, which is the claim order.
    """
    rows = []
    used_drops = set()
    counts = dict(position_counts)

    ranked_adds = sorted(
        (a for a in adds
         if a.get("injury_status") not in UNADDABLE_STATUSES
         and not a.get("on_bye")
         and a.get("id") not in exclude_add_ids),
        # Adds that fill an empty lineup slot choose their drop first, so a
        # bigger add elsewhere can't use up the only droppable player.
        key=lambda a: (a.get("position") in needed_positions, add_score(a, dynasty)),
        reverse=True,
    )

    for add in ranked_adds:
        pos = add.get("position")
        cap = caps.get(pos)
        at_cap = cap is not None and counts.get(pos, 0) >= cap

        candidates = [d for d in drops if d["id"] not in used_drops]
        if at_cap:
            candidates = [d for d in candidates if d.get("position") == pos]
        if not candidates:
            continue

        # Prefer drops that don't burn keeper eligibility, then a
        # same-position swap (keeps the roster balanced - every candidate
        # here is already a low-value drop), then the least valuable
        # (lowest keep score).
        candidates.sort(key=lambda d: (
            bool(d.get("keeper_eligible")),
            d.get("position") != pos,
            d.get("keep_score", 0.0),
        ))
        drop = candidates[0]

        add_ex = add.get("expected_points") or 0.0
        drop_ex = drop.get("expected_points") or 0.0
        gain = round(add_ex - drop_ex, 1)

        add_fc = add.get("fantasycalc_value") or 0.0
        drop_fc = drop.get("fantasycalc_value") or 0.0
        long_term_win = dynasty and add_fc > 0 and add_fc >= max(drop_fc, 1.0) * ADD_MIN_VALUE_RATIO

        fills_slot = pos in needed_positions
        if gain < ADD_MIN_GAIN and not long_term_win and not fills_slot:
            continue
        if drop.get("keeper_eligible") and gain < KEEPER_BURN_MIN_GAIN and not fills_slot:
            continue  # not worth forfeiting keeper eligibility

        warnings = []
        if fills_slot:
            warnings.append(f"Fills your empty {pos} slot this week")
        if drop.get("keeper_eligible"):
            warnings.append("Dropping costs keeper eligibility for good")
        if at_cap:
            warnings.append(f"Roster is at the {cap}-{pos} cap, so the drop is forced to a {pos}")

        used_drops.add(drop["id"])
        counts[pos] = counts.get(pos, 0) + 1
        counts[drop.get("position")] = max(counts.get(drop.get("position"), 0) - 1, 0)

        rows.append({
            "add": {k: add.get(k) for k in ("id", "name", "position", "team", "injury_status", "expected_points", "fantasycalc_value", "age", "add_count_48h")},
            "drop": {k: drop.get(k) for k in ("id", "name", "position", "team", "expected_points", "fantasycalc_value", "age", "keeper_eligible")},
            "gain": gain,
            "long_term": bool(long_term_win and gain < ADD_MIN_GAIN),
            "fills_slot": bool(fills_slot),
            "warning": "; ".join(warnings) or None,
            "_rank": gain + (0.5 if long_term_win else 0.0) + (100.0 if fills_slot else 0.0),
        })

    rows.sort(key=lambda r: r["_rank"], reverse=True)
    rows = rows[:MAX_WAIVER_ROWS]
    for i, r in enumerate(rows, start=1):
        r["claim_order"] = i
        r.pop("_rank", None)
    return rows


# ------------------------------------------------------------ fallback drops

# Hold verdicts that are about MARKET value only. These players can be
# offered as a last resort (always flagged, never silently). Every other
# hold - young + injured, young upside, role growing - is never offered,
# because those are exactly the long-term bets the hold rules protect.
FALLBACK_HOLD_VERDICT = "Dynasty value - hold"


def fallback_drop_pool(drops: list) -> list:
    """Held players that could be cut if the owner insists: held only for
    market value, and past the 'young asset' age. Least valuable first."""
    pool = [
        d for d in drops
        if d.get("verdict") == FALLBACK_HOLD_VERDICT
        and d.get("age") is not None
        and d["age"] > YOUNG_AGE
    ]
    pool.sort(key=lambda d: d.get("keep_score", 0.0))
    return pool


def build_fallback_plan(adds: list, drops: list, position_counts: dict, caps: dict, dynasty: bool,
                        needed_positions=frozenset(), exclude_add_ids=frozenset(), exclude_drop_ids=frozenset()):
    """
    'If you must drop' list for when the safe plan has nothing (or not
    enough): pairs adds with the least valuable HELD players. Every row
    is flagged as cutting an asset, with its market value and a nudge to
    trade instead, so the owner makes the call knowingly.
    """
    pool = [d for d in fallback_drop_pool(drops) if d["id"] not in exclude_drop_ids]
    rows = build_waiver_plan(
        adds, pool, position_counts, caps, dynasty,
        needed_positions=needed_positions, exclude_add_ids=exclude_add_ids,
    )
    for r in rows:
        r["fallback"] = True
        value = r["drop"].get("fantasycalc_value")
        note = (
            f"{r['drop']['name']} is held for market value"
            + (f" (FantasyCalc {round(value)})" if value else "")
            + " - cutting him gives that away; trading him first is better if anyone will take him"
        )
        r["warning"] = note if not r.get("warning") else f"{r['warning']}; {note}"
    return rows


# ------------------------------------------------------------ empty slots

def build_slot_fixes(adds: list, roster: list, needed_positions, roster_full: bool, dynasty: bool,
                     exclude_add_ids=frozenset(), exclude_drop_ids=frozenset()):
    """
    One row per empty lineup slot the waiver plans didn't already fill
    (e.g. your only DEF is on a bye). A full lineup isn't optional, so
    unlike the other plans there is no gain bar: the best healthy free
    agent at that position is paired with a drop, and if the roster has
    an open spot, with no drop at all.

    `roster` is every player you could cut: healthy starters you need
    this week, IR/taxi stashes and anyone the caller marks `protected`
    (young + injured, young upside, rising role) must already be left
    out. The drop is, in order of preference:
      * a same-position player (usually the one leaving the hole - a
        bye-week DEF is worth nothing to a dynasty roster),
      * otherwise the least valuable player (lowest keep score),
      * never keeper-eligible unless nothing else is left (flagged).
    Dropping someone held for market value is allowed but flagged.
    """
    rows = []
    used_adds, used_drops = set(exclude_add_ids), set(exclude_drop_ids)

    for pos in sorted(needed_positions):
        options = [
            a for a in adds
            if a.get("position") == pos
            and a.get("injury_status") not in UNADDABLE_STATUSES
            and not a.get("on_bye")
            and a.get("id") not in used_adds
        ]
        if not options:
            continue
        add = max(options, key=lambda a: add_score(a, dynasty))

        drop = None
        if roster_full:
            candidates = [p for p in roster if p["id"] not in used_drops]
            if not candidates:
                continue
            candidates.sort(key=lambda p: (
                bool(p.get("keeper_eligible")),
                p.get("position") != pos,
                p.get("keep_score", 0.0),
            ))
            drop = candidates[0]

        add_pts = effective_points(add) or 0.0
        drop_pts = (effective_points(drop) or 0.0) if drop else 0.0

        warnings = [f"Fills your empty {pos} slot this week"]
        if drop is None:
            warnings.append("You have an open roster spot, so no drop is needed")
        else:
            if drop.get("keeper_eligible"):
                warnings.append("Dropping costs keeper eligibility for good")
            if drop.get("hold"):
                value = drop.get("fantasycalc_value")
                warnings.append(
                    f"{drop['name']} is held for market value"
                    + (f" (FantasyCalc {round(value)})" if value else "")
                    + " - trading him first is better if anyone will take him"
                )

        used_adds.add(add["id"])
        if drop:
            used_drops.add(drop["id"])
        rows.append({
            "add": {k: add.get(k) for k in ("id", "name", "position", "team", "injury_status", "expected_points", "fantasycalc_value", "age", "add_count_48h")},
            "drop": {k: drop.get(k) for k in ("id", "name", "position", "team", "expected_points", "fantasycalc_value", "age", "keeper_eligible")} if drop else None,
            "gain": round(add_pts - drop_pts, 1),
            "long_term": False,
            "fills_slot": True,
            "warning": "; ".join(warnings),
        })

    for i, r in enumerate(rows, start=1):
        r["claim_order"] = i
    return rows
