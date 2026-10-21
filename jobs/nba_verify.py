"""Check raw_nba_boxscores coverage against the stored schedule.

Reports how many final games are missing some of the 11 endpoints, so a run
that got throttled part-way is easy to spot. Read-only.

Usage:
    python -m jobs.nba_verify [--since YYYY-MM-DD]   # default: whole stored season
"""

import argparse
from datetime import date, datetime
from zoneinfo import ZoneInfo

from jobs.db import connect
from jobs.nba import BOXSCORE_ENDPOINTS, GAME_TYPES, current_season

ET = ZoneInfo("America/New_York")
EXPECTED = len(BOXSCORE_ENDPOINTS)


def run(since: date | None) -> int:
    """Returns the number of final games missing at least one endpoint."""
    with connect() as conn:
        season = current_season(datetime.now(ET).date())
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM raw_nba_schedule WHERE season = %s", (season,))
            row = cur.fetchone()
        if not row:
            print(f"no schedule stored for {season}; run `python -m jobs.nba` first")
            return 0
        games = [g for d in row[0]["leagueSchedule"]["gameDates"] for g in d["games"]]
        final = {g["gameId"]: g["gameDateEst"][:10] for g in games
                 if g["gameStatus"] == 3 and g["gameId"][:3] in GAME_TYPES
                 and (since is None or g["gameDateEst"][:10] >= since.isoformat())}

        with conn.cursor() as cur:
            cur.execute("SELECT game_id, count(*) FROM raw_nba_boxscores "
                        "WHERE game_id = ANY(%s) GROUP BY game_id", (list(final),))
            counts = dict(cur.fetchall())

    complete = sum(1 for gid in final if counts.get(gid, 0) == EXPECTED)
    partial = {gid: counts.get(gid, 0) for gid in final if counts.get(gid, 0) != EXPECTED}
    print(f"season {season} | {len(final)} final games | {complete} complete "
          f"({EXPECTED}/{EXPECTED} endpoints) | {len(partial)} incomplete")
    for gid, n in sorted(partial.items(), key=lambda kv: final[kv[0]]):
        print(f"  {gid} {final[gid]}: {n}/{EXPECTED}")
    return len(partial)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    run(parser.parse_args().since)
