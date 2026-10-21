"""Store the NBA schedule and final-game box scores from stats.nba.com via nba_api.

stats.nba.com drops connections from cloud datacenter IPs (GitHub runners
included) while answering residential ones, so requests go out through the
egress URL in the NBA_PROXY secret, which nba_api accepts via its documented
`proxy=` argument. An empty NBA_PROXY means a direct connection. Each endpoint's
raw JSON is its own row, so an interrupted run resumes where it stopped.

Usage:
    python -m jobs.nba [--since YYYY-MM-DD]   # default: final games in the last 14 days
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nba_api.stats import endpoints

from jobs.db import connect, ensure_schema

ET = ZoneInfo("America/New_York")
LOOKBACK_DAYS = 14
# Regular season, playoffs, play-in. Preseason (001), All-Star (003) and the
# Cup final (006) don't count toward season stats.
GAME_TYPES = ("002", "004", "005")
BOXSCORE_ENDPOINTS = [
    "BoxScoreTraditionalV3", "BoxScoreAdvancedV3", "BoxScoreMiscV3", "BoxScoreScoringV3",
    "BoxScoreUsageV3", "BoxScoreFourFactorsV3", "BoxScorePlayerTrackV3", "BoxScoreHustleV2",
    "BoxScoreDefensiveV2", "BoxScoreSummaryV3", "BoxScoreMatchupsV3",
]
REQUEST_GAP = 0.6  # seconds between calls; stats.nba.com throttles bursts
PROXY = os.environ.get("NBA_PROXY", "")  # residential/VPN egress; "" = direct


def current_season(today: date) -> str:
    start = today.year if today.month >= 8 else today.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _call(name: str, **params) -> dict:
    for attempt in range(4):
        try:
            return getattr(endpoints, name)(proxy=PROXY, timeout=30, **params).get_dict()
        except Exception:  # read timeouts and blank bodies (JSONDecodeError) are transient
            if attempt == 3:
                raise
            time.sleep(5 * 2 ** attempt)


def _has_players(payload: dict) -> bool:
    """Tracking/hustle tables can lag the final buzzer; an empty one is retried next run."""
    body = next(v for k, v in payload.items() if k != "meta")
    return all(body.get(side, {}).get("players") for side in ("homeTeam", "awayTeam"))


def _store_schedule(conn, season: str) -> list[dict]:
    payload = _call("ScheduleLeagueV2", league_id="00", season=season)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_nba_schedule (season, payload) VALUES (%s, %s) "
            "ON CONFLICT (season) DO UPDATE SET payload = excluded.payload, fetched_at = now()",
            (season, json.dumps(payload)))
    conn.commit()
    return [g for d in payload["leagueSchedule"]["gameDates"] for g in d["games"]]


def run(since: date) -> int:
    """Returns the number of endpoint fetches that failed."""
    saved = failed = 0
    with connect() as conn:
        ensure_schema(conn)
        games = _store_schedule(conn, current_season(datetime.now(ET).date()))
        final = {g["gameId"]: g["gameDateEst"][:10] for g in games
                 if g["gameStatus"] == 3 and g["gameId"][:3] in GAME_TYPES
                 and g["gameDateEst"][:10] >= since.isoformat()}
        with conn.cursor() as cur:
            cur.execute("SELECT game_id, endpoint FROM raw_nba_boxscores WHERE game_id = ANY(%s)",
                        (list(final),))
            have = set(cur.fetchall())

        for game_id, game_date in sorted(final.items(), key=lambda kv: (kv[1], kv[0])):
            missing = [e for e in BOXSCORE_ENDPOINTS if (game_id, e) not in have]
            stored = 0
            for name in missing:
                time.sleep(REQUEST_GAP)
                try:
                    payload = _call(name, game_id=game_id)
                except Exception as exc:  # noqa: BLE001 - keep going; next run retries
                    print(f"  {game_id} {name}: {type(exc).__name__} {str(exc)[:120]}")
                    failed += 1
                    continue
                if not _has_players(payload):
                    print(f"  {game_id} {name}: no player rows yet, retrying next run")
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO raw_nba_boxscores (game_id, endpoint, game_date, payload) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (game_id, endpoint) DO NOTHING",
                        (game_id, name, game_date, json.dumps(payload)))
                conn.commit()
                stored += 1
            if missing:
                print(f"{game_id} {game_date}: stored {stored}/{len(missing)} missing endpoints")
            saved += stored

    print(f"{datetime.now():%Y-%m-%d %H:%M} | {len(final)} final games since {since} | "
          f"{saved} payloads stored | {failed} failed")
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=date.fromisoformat,
                        default=datetime.now(ET).date() - timedelta(days=LOOKBACK_DAYS))
    sys.exit(1 if run(parser.parse_args().since) else 0)
