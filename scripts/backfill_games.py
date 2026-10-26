"""One-off repair: load past-season games that never made it into games / player_game_stats.

The scheduled bridge only sees the current season's schedule, so a game missing
from an earlier season needs this. It fetches the same 11 box-score endpoints
jobs.nba collects (stored in raw_nba_boxscores for provenance), maps them with
the bridge's own build_rows, inserts the games row and player rows, then
recomputes days_rest from the earliest loaded date on (the game after each
loaded one changes too). Games already in player_game_stats are skipped, so
re-running is safe (it also loads defensive_matchups for every game id given, from the
stored payloads, so it can be re-run for games loaded before the loader existed). Needs a
connection stats.nba.com answers (a residential IP or NBA_PROXY).

Usage:
    python -m scripts.backfill_games GAME_ID [GAME_ID ...] [--commit]
"""

import argparse
import json
import time
from datetime import date

from jobs.bridge import (
    GAME_TYPE_LABEL, STAT_COLUMNS, _body, _num, _unknown_players,
    blank_implausible_defense, build_rows, ensure_players, load_matchups, recompute_days_rest,
)
from jobs.db import connect
from jobs.nba import BOXSCORE_ENDPOINTS, REQUEST_GAP, _call, _has_players


def season_of(game_id: str) -> str:
    """'0022400147' -> '2024-25'."""
    start = 2000 + int(game_id[3:5])
    return f"{start}-{(start + 1) % 100:02d}"


def fetch_endpoints(game_id: str) -> dict[str, dict]:
    payloads = {}
    for name in BOXSCORE_ENDPOINTS:
        time.sleep(REQUEST_GAP)
        payload = _call(name, game_id=game_id)
        if _has_players(payload):
            payloads[name] = payload
        else:
            print(f"  {game_id} {name}: no player rows; its columns stay NULL")
    return payloads


def run(game_ids: list[str], commit: bool) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT game_id FROM player_game_stats WHERE game_id = ANY(%s)",
                        (game_ids,))
            done = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT player_id FROM players")
            known_players = {r[0] for r in cur.fetchall()}
        todo = [g for g in game_ids if g not in done]
        if done:
            print(f"already loaded, skipping: {sorted(done)}")

        schedules = {}
        for season in sorted({season_of(g) for g in todo}):
            payload = _call("ScheduleLeagueV2", league_id="00", season=season)
            games = [g for d in payload["leagueSchedule"]["gameDates"] for g in d["games"]]
            schedules[season] = {g["gameId"]: g for g in games}

        for game_id in todo:
            season = season_of(game_id)
            game = schedules[season][game_id]
            eps = fetch_endpoints(game_id)
            if "BoxScoreTraditionalV3" not in eps:
                print(f"{game_id}: no traditional box score; skipped")
                continue
            rows = build_rows(game, eps)
            blanked = blank_implausible_defense(rows)
            body = _body(eps["BoxScoreTraditionalV3"])
            scores = {side: _num(body[side].get("statistics", {}).get("points"))
                      for side in ("homeTeam", "awayTeam")}
            new_players = _unknown_players(eps["BoxScoreTraditionalV3"], known_players)
            print(f"{'COMMIT' if commit else 'DRY RUN'} {game_id} {game['gameDateEst'][:10]} "
                  f"{game['awayTeam']['teamTricode']} {scores['awayTeam']:.0f} @ "
                  f"{game['homeTeam']['teamTricode']} {scores['homeTeam']:.0f} | {len(rows)} player rows | "
                  f"{len(eps)}/{len(BOXSCORE_ENDPOINTS)} endpoints | {len(new_players)} new players | "
                  f"{blanked} defensive rows blanked")
            if not commit:
                continue

            with conn.cursor() as cur:
                for name, payload in eps.items():
                    cur.execute(
                        "INSERT INTO raw_nba_boxscores (game_id, endpoint, game_date, payload) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT (game_id, endpoint) DO NOTHING",
                        (game_id, name, game["gameDateEst"][:10], json.dumps(payload)))
            conn.commit()
            ensure_players(conn, new_players, known_players)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO games (game_id, game_date, season, game_type, home_team_id, "
                    "away_team_id, tipoff_time_utc, status, home_score, away_score) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'final',%s,%s) ON CONFLICT (game_id) DO NOTHING",
                    (game_id, game["gameDateEst"][:10], season,
                     GAME_TYPE_LABEL.get(game_id[:3], "regular"),
                     str(game["homeTeam"]["teamId"]), str(game["awayTeam"]["teamId"]),
                     game["gameDateTimeUTC"], int(scores["homeTeam"]), int(scores["awayTeam"])))
                for row in rows:
                    if row["player_id"] in known_players:
                        cur.execute(f"INSERT INTO player_game_stats ({','.join(STAT_COLUMNS)}) "
                                    f"VALUES ({','.join(['%s'] * len(STAT_COLUMNS))})",
                                    [row[c] for c in STAT_COLUMNS])
            conn.commit()

        load_matchups(conn, game_ids, known_players, commit)
        if todo:
            earliest = min(schedules[season_of(g)][g]["gameDateEst"][:10] for g in todo)
            recompute_days_rest(conn, date.fromisoformat(earliest), commit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("game_ids", nargs="+")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    run(args.game_ids, args.commit)
