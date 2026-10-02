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
