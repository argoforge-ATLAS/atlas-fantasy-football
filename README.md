# Atlas Fantasy Football

A season-long fantasy football assistant for two Sleeper leagues, built in phases:

1. **Lineup HQ** (in progress) - week-to-week: current roster, this week's matchup, start/sit and waiver suggestions.
2. **Keepers** (later) - dynasty keeper valuation, carried forward from the old `atlas-fantasy` project (see `docs/old-app-notes.md` once copied over).
3. **Draft** (later) - live draft board, carried forward from the old `atlas-draft-board` project.

## Running locally / on Atlas

```
docker compose up -d --build
```

Then visit `http://<atlas-ip>:8095` (or the Tailscale address once that's wired up).

## Config

League IDs and the Sleeper username live in `backend/config.py`. These aren't secrets - they're public Sleeper identifiers - so they're committed directly.

## Data source

All data comes live from the public [Sleeper API](https://docs.sleeper.com/) - no login or API key required. The full NFL player list is cached to disk for up to 20 hours at a time (Sleeper asks API users not to pull it more often than that).
