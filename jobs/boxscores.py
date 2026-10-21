"""Store final box scores from the NBA CDN for recent games.

Usage:
    python -m jobs.boxscores [YYYY-MM-DD ...]   # default: yesterday and today (US Eastern)
"""

import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from jobs.db import connect, ensure_schema

CDN = "https://cdn.nba.com/static/json"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/"}
FINAL = 3


def _get(url: str) -> dict:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def run(dates: list[date]) -> int:
    schedule = _get(f"{CDN}/staticData/scheduleLeagueV2.json")["leagueSchedule"]["gameDates"]
    wanted = {d.strftime("%m/%d/%Y 00:00:00") for d in dates}
    games = [g for gd in schedule if gd["gameDate"] in wanted for g in gd["games"]]
    games = [g for g in games if g["gameStatus"] == FINAL]
    saved = 0
    with connect() as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT game_id FROM raw_boxscores WHERE game_id = ANY(%s)",
                        ([g["gameId"] for g in games],))
            have = {r[0] for r in cur.fetchall()}
        for g in games:
            if g["gameId"] in have:
                continue
            box = _get(f"{CDN}/liveData/boxscore/boxscore_{g['gameId']}.json")
            game_date = datetime.fromisoformat(
                g["gameDateTimeEst"].replace("Z", "+00:00")
            ).date() if g.get("gameDateTimeEst") else dates[0]
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO raw_boxscores (game_id, game_date, payload) VALUES (%s,%s,%s) "
                    "ON CONFLICT (game_id) DO NOTHING",
                    (g["gameId"], game_date, json.dumps(box)),
                )
            conn.commit()
            saved += 1
            print(f"saved box score {g['gameId']}")
    print(f"{saved} new box scores")
    return saved


if __name__ == "__main__":
    if len(sys.argv) > 1:
        targets = [date.fromisoformat(a) for a in sys.argv[1:]]
    else:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        targets = [today - timedelta(days=1), today]
    run(targets)
