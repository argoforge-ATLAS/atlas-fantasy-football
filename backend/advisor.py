"""
The AI reasoning layer: takes structured roster/league data (plus any
optional human notes - e.g. what you saw on Fantasy Footballers) and
asks Claude for a start/sit and waiver recommendation specific to a
league's actual rules.

Requires ANTHROPIC_API_KEY to be set in the environment (see .env,
which is never committed - see .gitignore).
"""
import os
import json

from anthropic import Anthropic

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = Anthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You are a fantasy football assistant helping one specific manager \
with one specific team in one specific league. You are given real roster data, \
player status, league scoring/roster settings, and optionally some human notes \
(e.g. expert commentary the user read elsewhere).

Give concrete, specific advice:
- Which bench players should start over which starters, and why (injury, bye, matchup, recent form)
- Which available waiver-wire players are worth adding, and who to drop for them
- Keep it tied to the ACTUAL league rules given - a generic PPR answer is wrong if \
this league scores differently

Be direct and concise. This is for one person's real lineup decision this week, not a general article."""


def get_weekly_advice(league_context: dict, human_notes: str = "") -> str:
    client = _get_client()

    user_content = "League and roster data:\n```json\n"
    user_content += json.dumps(league_context, indent=2)
    user_content += "\n```\n"

    if human_notes:
        user_content += f"\nAdditional notes from the user (e.g. things they read elsewhere):\n{human_notes}\n"

    user_content += "\nGive this week's start/sit and waiver wire advice."

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text
