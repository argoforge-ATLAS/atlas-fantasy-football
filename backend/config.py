"""
Configuration for Atlas Fantasy Football - Lineup HQ.

League IDs and your Sleeper username are not secrets (they're public,
read-only identifiers), so they're fine to commit to the repo.
"""

SLEEPER_USERNAME = "dpaters1"

LEAGUES = [
    {"id": "1382428809231347712", "label": "League 1"},
    {"id": "1388712035025424384", "label": "League 2"},
]

SLEEPER_API_BASE = "https://api.sleeper.app/v1"

# How long to keep the full NFL player list cached on disk before
# re-fetching (it's a big, slow-changing file - Sleeper asks that you
# not pull it more than once a day).
PLAYER_CACHE_HOURS = 20
PLAYER_CACHE_PATH = "/app/data/players_cache.json"

# --- League-specific house rules -------------------------------------
# Keyed by the league's real Sleeper name (not list order), same
# approach as KEEPER_RULE_LEAGUE_NAMES in main.py, so reordering or an
# ID change can't silently flip a rule onto the wrong league.

# Max players at a position a roster may hold. Sleeper doesn't expose
# this, so it lives here. DickMelt caps rosters at 3 QBs.
POSITION_CAPS = {
    "DickMelt Fantasies": {"QB": 3},
}

# Trade-price multipliers by NFL team when YOU are acquiring a player.
# DickMelt's owners are Chiefs fans, so KC players cost extra.
TRADE_TAX = {
    "DickMelt Fantasies": {"KC": 1.15},
}

# Force FantasyCalc dynasty vs. redraft values per league name. Leave a
# league out to infer from Sleeper's own league type (keeper/dynasty
# leagues use dynasty values; redraft leagues use redraft values).
DYNASTY_OVERRIDES = {
    # "League Name": True / False
}

# Empirical-ish value of a draft pick, expressed as "worth about the
# Nth most valuable player on the live FantasyCalc board". Values are
# derived from the live board at request time, so picks float with the
# market instead of being a hardcoded number. Round -> player rank.
PICK_RANK_BY_ROUND = {1: 18, 2: 45, 3: 80, 4: 120}
PICK_RANK_LATE = 180  # round 5+ - a sweetener, not an asset

# Package weighting for trades: the best asset counts fully, each
# additional asset counts less. Models "2-for-1 consolidation" - the
# side receiving the single best player wins ties, because roster
# spots and starting slots are scarce.
PACKAGE_WEIGHTS = [1.0, 0.75, 0.55, 0.4, 0.3]

# Picks you keep in each league (a pick from a protected round going
# out the door is flagged as "you're all in"). DickMelt: rounds 1-3
# are core; the 4th is negotiable; 5th+ is a sweetener.
PROTECTED_PICK_ROUNDS = {
    "DickMelt Fantasies": [1, 2, 3],
}

# Players you will not trade. The trade-package suggester skips them
# and the evaluator warns if you send one.
UNTOUCHABLE = {
    "DickMelt Fantasies": ["Zay Flowers"],
}
