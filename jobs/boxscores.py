"""Store final box scores from ESPN's public API for recent games.

game_id is the ESPN event id (not the NBA game id); the payload carries the
team abbreviations and tip-off time needed to join on date + teams.

Usage:
    python -m jobs.boxscores [YYYY-MM-DD ...]   # default: yesterday and today (US Eastern)
"""

import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from jobs.db import connect, ensure_schema

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ET = ZoneInfo("America/New_York")


def _get(path: str, **params) -> dict:
    r = requests.get(f"{ESPN}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _final_events(d: date) -> list[dict]:
    events = _get("scoreboard", dates=d.strftime("%Y%m%d"))["events"]
    return [e for e in events if e["status"]["type"]["completed"]]


def run(dates: list[date]) -> int:
    saved = 0
    with connect() as conn:
        ensure_schema(conn)
        for d in dates:
            events = _final_events(d)
            with conn.cursor() as cur:
                cur.execute("SELECT game_id FROM raw_boxscores WHERE game_id = ANY(%s)",
                            ([e["id"] for e in events],))
                have = {r[0] for r in cur.fetchall()}
            for e in events:
                if e["id"] in have:
                    continue
                summary = _get("summary", event=e["id"])
                tipoff = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                comp = e["competitions"][0]["competitors"]
                payload = {
                    "espn_event_id": e["id"],
                    "tipoff_utc": e["date"],
                    "teams": {c["homeAway"]: c["team"]["abbreviation"] for c in comp},
                    "score": {c["homeAway"]: c.get("score") for c in comp},
                    "boxscore": summary["boxscore"],
                }
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO raw_boxscores (game_id, game_date, payload) "
                        "VALUES (%s,%s,%s) ON CONFLICT (game_id) DO NOTHING",
                        (e["id"], tipoff.astimezone(ET).date(), json.dumps(payload)),
                    )
                conn.commit()
                saved += 1
                print(f"saved {payload['teams']} {d}")
    print(f"{saved} new box scores")
    return saved


if __name__ == "__main__":
    if len(sys.argv) > 1:
        targets = [date.fromisoformat(a) for a in sys.argv[1:]]
    else:
        today = datetime.now(ET).date()
        targets = [today - timedelta(days=1), today]
    run(targets)
