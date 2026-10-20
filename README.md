# Atlas Fantasy Football

A season-long fantasy football assistant for two Sleeper leagues, built in phases:

1. **Lineup HQ** - week-to-week: your real head-to-head, start/sit, waiver plan, drops, team needs and trades.
2. **Keepers** (later) - dynasty keeper valuation, carried forward from the old `atlas-fantasy` project.
3. **Draft** (later) - live draft board, carried forward from the old `atlas-draft-board` project.

## What Lineup HQ does

| Panel / endpoint | What it answers |
|---|---|
| `GET /api/leagues/{id}/live-matchup` | Who am I actually playing? Both rosters with live points, done / playing / yet-to-play, projected finals, win probability and its history |
| `GET /api/leagues/{id}/recommendations` | Start/sit on **expected points** (Sleeper projection blended with recent production, matchup-adjusted), best free agents, and the **waiver plan** |
| waiver plan (inside `recommendations`) | Add/drop pairs in claim order, from real free agents; respects position caps and keeper eligibility |
| `GET /api/leagues/{id}/drop-candidates` | Who is worth cutting - with long-term HOLD rules (young + injured, dynasty value) |
| `GET /api/leagues/{id}/team-needs` | Your starters vs every other roster's starters, ranked |
| `GET /api/leagues/{id}/trade-eval?give=A,2027 R2&get=B` | Trade balance on live market value, with pick values, consolidation weighting and team tax |
| `GET /api/leagues/{id}/trade-targets` | Who to target at your weak spots, priced, with a package from your own bench |
| `GET /api/leagues/{id}/matchups` | Each rostered player's real NFL opponent and defense-vs-position difficulty |
| `/overlay.html` | TV overlay (see below) |

### How the decisions are made

* **Start/sit** compares expected points. Out/Doubtful/IR/bye players are valued at 0, Questionable players are discounted 10%, and the single best swap is applied each round, so FLEX / SUPER_FLEX chains resolve properly (1.5-point margin to avoid noise).
* **Drops** hold anyone who is young (25 or under) with any injury designation, anyone on the live FantasyCalc board above the top-150 line in keeper/dynasty leagues, and rookies with upside. Real drop candidates are ranked by a keep score (market value x age curve + expected points), not just last week's box score.
* **Waiver plan** pairs each add with a drop: never a held player, same position preferred, keeper-eligible players only for a big upgrade (and flagged), and a position cap (3 QBs in DickMelt) forces a same-position drop.
* **Trades** price picks off the live board (a 1st is about the 18th-best player, a 2nd the 45th, ...), discount depth (best asset counts fully, then 75%, 55%, ...), and treat team tax as a price: you must clear the taxed price of what you receive for them to likely accept. Sending a protected pick or an untouchable player throws a warning.
* **FantasyCalc** values are dynasty values in keeper/dynasty leagues (inferred from Sleeper's league type, override in `config.DYNASTY_OVERRIDES`) and redraft values otherwise.
* **Defense rankings** are points allowed per *game* (weekly totals averaged over games played), not per player stat line.
* **Win probability**: each side's final ~ Normal(points now + remaining projections, summed per-player variance); remaining output shrinks as ESPN's game clock advances.

## TV overlay

Open `http://<atlas-ip>:8095/overlay.html` on the TV (or the "TV overlay" link in the app header).

* Quiet by default: a small corner chip with score and win %. The full panel pops open for about 12 seconds whenever a score changes, with a green or red flash.
* Press **OK / Enter** on the remote (or tap) to open or close it.
* Query options: `?league=<sleeper league id>` (default: first league), `?interval=20` (poll seconds), `?show=12` (seconds open after a change), `?mode=always` (keep the panel up).

## House rules and tuning

League-specific rules live in `backend/config.py`, keyed by the league's real Sleeper name: `POSITION_CAPS`, `TRADE_TAX`, `PROTECTED_PICK_ROUNDS`, `UNTOUCHABLE`, `DYNASTY_OVERRIDES`, plus the pick-value curve (`PICK_RANK_BY_ROUND`) and package weights (`PACKAGE_WEIGHTS`). Keeper eligibility (DickMelt's never-touched-waivers rule) is in `backend/keeper_eligibility.py`.

## Running locally / on Atlas

```
docker compose up -d --build
```

Then visit `http://<atlas-ip>:8095` (or the Tailscale address).

## Tests

```
python -m unittest discover -s tests -v
```

`tests/test_logic.py` covers the decision logic with no dependencies. `tests/test_endpoints.py` runs every route against a fake 10-team league (needs `fastapi`, skipped otherwise).

## Data sources

* [Sleeper API](https://docs.sleeper.com/) - rosters, live matchups, stats, trending adds. No key. API calls are cached for a few seconds to an hour depending on how fast the data moves; the full player list is cached for up to 20 hours.
* Sleeper weekly projections (`api.sleeper.com`, undocumented). If the endpoint is unavailable or changes shape, start/sit falls back to recent production with a matchup adjustment.
* ESPN public scoreboard - schedule and live game clock.
* FantasyCalc public API - market values for players and (via the board) picks.
