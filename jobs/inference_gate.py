"""Decide whether this bridge run should start the app's daily inference.

The bridge runs after every successful nba collection, several times a day.
Predictions should start once per ET day, as soon as last night's games are
final and bridged into player_game_stats, so features include every result.
Past DEADLINE_HOUR_ET the run starts anyway, so a postponed game (which stays
"scheduled" forever) cannot hold the whole day's predictions back. Whether
inference already ran today is checked in the workflow, which has the token.

Usage:
    python -m jobs.inference_gate   # prints ready=true|false, also to $GITHUB_OUTPUT
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jobs.db import connect

ET = ZoneInfo("America/New_York")
DEADLINE_HOUR_ET = 11


def decide(now_et: datetime, last_night: list[tuple[str, str, bool]]) -> tuple[bool, str]:
    """Return (ready, reason) from last night's (game_id, status, has_stats) rows."""
    blocking = [game_id for game_id, status, has_stats in last_night
                if status != "final" or not has_stats]
    if not blocking:
        return True, f"all {len(last_night)} games from last night are final and bridged"
    if now_et.hour >= DEADLINE_HOUR_ET:
        return True, (f"{len(blocking)} games from last night still not bridged "
                      f"({', '.join(blocking)}), starting anyway after {DEADLINE_HOUR_ET}:00 ET")
    return False, f"waiting on {len(blocking)} games from last night: {', '.join(blocking)}"


def _last_night(conn, day: str) -> list[tuple[str, str, bool]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.game_id, g.status, EXISTS (SELECT 1 FROM player_game_stats s "
            "WHERE s.game_id = g.game_id) FROM games g WHERE g.game_date = %s",
            (day,))
        return cur.fetchall()


if __name__ == "__main__":
    now_et = datetime.now(ET)
    yesterday = (now_et.date() - timedelta(days=1)).isoformat()
    with connect() as conn:
        ready, reason = decide(now_et, _last_night(conn, yesterday))
    print(f"ready={'true' if ready else 'false'} | {reason}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handle:
            handle.write(f"ready={'true' if ready else 'false'}\n")
