"""Store the NBA schedule and final-game box scores from stats.nba.com via nba_api.

stats.nba.com drops connections from cloud datacenter IPs (GitHub runners
included) while answering residential ones, so requests go out through the
egress URL in the NBA_PROXY secret, which nba_api accepts via its documented
`proxy=` argument. An empty NBA_PROXY means a direct connection. Each endpoint's
raw JSON is its own row, so an interrupted run resumes where it stopped.

GitHub starts scheduled runs hours late, so a fixed "after games finish" cron
lands at a random time. With --wait-minutes, a run that starts while a slate is
being played waits until every game on it is final and collects straight away,
which lets the bridge start the day's predictions within minutes of the last
final buzzer instead of whenever the next delayed cron fires.

Usage:
    python -m jobs.nba [--since YYYY-MM-DD]   # default: final games in the last 14 days
    python -m jobs.nba --wait-minutes 300     # first wait for the current slate to finish
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
WAIT_POLL_SECONDS = 600
# A game still not final this long after tip-off was postponed or suspended;
# it stops holding up the slate.
POSTPONED_AFTER = timedelta(hours=6)


def current_season(today: date) -> str:
    start = today.year if today.month >= 8 else today.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _call(name: str, **params) -> dict:
    for attempt in range(4):
        ep = getattr(endpoints, name)(proxy=PROXY, timeout=30, get_request=False, **params)
        try:
            ep.get_request()
            return ep.get_dict()
        except Exception:  # read timeouts and blank bodies (JSONDecodeError) are transient
            # nba_api's parser crashes on the empty shell the NBA serves for tables it
            # hasn't published; that is a valid answer (no players yet), not a failure.
            response = getattr(ep, "nba_response", None)
            if response is not None:
                try:
                    return response.get_dict()
                except Exception:  # noqa: BLE001 - body wasn't JSON; retry below
                    pass
            if attempt == 3:
                raise
            time.sleep(5 * 2 ** attempt)


def _has_players(payload: dict) -> bool:
    """Tracking/hustle tables can lag the final buzzer; an empty one is retried next run."""
    body = next(v for k, v in payload.items() if k != "meta")
    return all((body.get(side) or {}).get("players") for side in ("homeTeam", "awayTeam"))


def _tipoff(game: dict) -> datetime:
    return datetime.fromisoformat(game["gameDateTimeUTC"].replace("Z", "+00:00"))


def unfinished_slate_games(games: list[dict], now: datetime) -> list[dict]:
    """Games on the most recently started slate that are not final yet.

    The slate is the ET date of the latest tip-off before `now`, so a run that
    starts after the early games also waits for that night's late games, even
    those that have not tipped yet. Before the first tip of the evening the
    latest started slate is last night's, which is already final.
    """
    games = [g for g in games if g["gameId"][:3] in GAME_TYPES]
    started = [g for g in games if _tipoff(g) <= now]
    if not started:
        return []
    slate = max(g["gameDateEst"][:10] for g in started)
    return [g for g in games
            if g["gameDateEst"][:10] == slate and g["gameStatus"] != 3
            and now - _tipoff(g) < POSTPONED_AFTER]


def wait_for_slate(max_wait: timedelta) -> None:
    """Poll the schedule until the current slate is final, or give up at max_wait."""
    deadline = datetime.now(timezone.utc) + max_wait
    season = current_season(datetime.now(ET).date())
    while True:
        try:
            payload = _call("ScheduleLeagueV2", league_id="00", season=season)
        except Exception as exc:  # noqa: BLE001 - a flaky poll must not end the watch
            print(f"  schedule poll failed: {type(exc).__name__} {str(exc)[:120]}")
            pending = None
        else:
            games = [g for d in payload["leagueSchedule"]["gameDates"] for g in d["games"]]
            pending = unfinished_slate_games(games, datetime.now(timezone.utc))
            if not pending:
                return
        now = datetime.now(timezone.utc)
        if now >= deadline:
            print(f"{now:%H:%M} UTC | stopped waiting after {max_wait}; collecting what is final")
            return
        if pending:
            print(f"{now:%H:%M} UTC | waiting on {len(pending)} unfinished games on "
                  f"{pending[0]['gameDateEst'][:10]}")
        time.sleep(WAIT_POLL_SECONDS)


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
    parser.add_argument("--wait-minutes", type=int, default=0,
                        help="first wait up to this long for the current slate to finish")
    args = parser.parse_args()
    if args.wait_minutes > 0:
        wait_for_slate(timedelta(minutes=args.wait_minutes))
    sys.exit(1 if run(args.since) else 0)
