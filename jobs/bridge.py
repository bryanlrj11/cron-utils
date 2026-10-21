"""Transform raw_nba_* JSON into the app's games / players / player_game_stats rows.

Reads the schedule and the 11 stored box-score endpoints per game, maps them
into the 147-column player_game_stats schema (identical to how the historical
171k rows were built), and fills the FK parents (games, and any player not yet
in players) first. Runs as a dry run unless --commit is passed.

Usage:
    python -m jobs.bridge [--since YYYY-MM-DD] [--commit]
"""

import argparse
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from jobs.db import connect
from jobs.nba import GAME_TYPES, current_season

ET = ZoneInfo("America/New_York")
GAME_TYPE_LABEL = {"002": "regular", "004": "playoff", "005": "playoff"}

# player_game_stats column <- camelCase field, grouped by the endpoint it comes from.
TRAD = {
    "pts": "points", "rebs": "reboundsTotal", "asts": "assists", "stls": "steals",
    "blks": "blocks", "fg3m": "threePointersMade", "tovs": "turnovers",
    "fga": "fieldGoalsAttempted", "fgm": "fieldGoalsMade", "fta": "freeThrowsAttempted",
    "ftm": "freeThrowsMade", "fg3a": "threePointersAttempted", "oreb": "reboundsOffensive",
    "dreb": "reboundsDefensive", "fouls_personal": "foulsPersonal",
    "fg_pct": "fieldGoalsPercentage", "fg3_pct": "threePointersPercentage",
    "ft_pct": "freeThrowsPercentage", "plus_minus": "plusMinusPoints",
}
ADV = {
    "efg_pct": "effectiveFieldGoalPercentage", "ts_pct": "trueShootingPercentage",
    "offensive_rating": "offensiveRating", "defensive_rating": "defensiveRating",
    "net_rating": "netRating", "estimated_offensive_rating": "estimatedOffensiveRating",
    "estimated_defensive_rating": "estimatedDefensiveRating",
    "estimated_net_rating": "estimatedNetRating", "usage_rate": "usagePercentage",
    "estimated_usage_pct": "estimatedUsagePercentage", "pace": "pace",
    "estimated_pace": "estimatedPace", "pace_per40": "pacePer40",
    "possessions": "possessions", "pie": "PIE", "assist_pct": "assistPercentage",
    "assist_to_turnover": "assistToTurnover", "assist_ratio": "assistRatio",
    "turnover_ratio": "turnoverRatio", "offensive_rebound_pct": "offensiveReboundPercentage",
    "defensive_rebound_pct": "defensiveReboundPercentage", "rebound_pct": "reboundPercentage",
}
MISC = {
    "points_off_turnovers": "pointsOffTurnovers", "points_second_chance": "pointsSecondChance",
    "points_fast_break": "pointsFastBreak", "points_paint": "pointsPaint",
    "opp_points_off_turnovers": "oppPointsOffTurnovers",
    "opp_points_second_chance": "oppPointsSecondChance",
    "opp_points_fast_break": "oppPointsFastBreak", "opp_points_paint": "oppPointsPaint",
    "blocks_against": "blocksAgainst", "fouls_drawn": "foulsDrawn",
}
SCORING = {
    "pct_fga_2pt": "percentageFieldGoalsAttempted2pt",
    "pct_fga_3pt": "percentageFieldGoalsAttempted3pt", "pct_pts_2pt": "percentagePoints2pt",
    "pct_pts_midrange_2pt": "percentagePointsMidrange2pt", "pct_pts_3pt": "percentagePoints3pt",
    "pct_pts_fast_break": "percentagePointsFastBreak", "pct_pts_ft": "percentagePointsFreeThrow",
    "pct_pts_off_tov": "percentagePointsOffTurnovers", "pct_pts_paint": "percentagePointsPaint",
    "pct_assisted_2pt": "percentageAssisted2pt", "pct_unassisted_2pt": "percentageUnassisted2pt",
    "pct_assisted_3pt": "percentageAssisted3pt", "pct_unassisted_3pt": "percentageUnassisted3pt",
    "pct_assisted_fgm": "percentageAssistedFGM", "pct_unassisted_fgm": "percentageUnassistedFGM",
}
USAGE = {
    "pct_fgm_team": "percentageFieldGoalsMade", "pct_fga_team": "percentageFieldGoalsAttempted",
    "pct_fg3m_team": "percentageThreePointersMade", "pct_fg3a_team": "percentageThreePointersAttempted",
    "pct_ftm_team": "percentageFreeThrowsMade", "pct_fta_team": "percentageFreeThrowsAttempted",
    "pct_reb_offensive": "percentageReboundsOffensive",
    "pct_reb_defensive": "percentageReboundsDefensive", "pct_reb_total": "percentageReboundsTotal",
    "pct_assists": "percentageAssists", "pct_turnovers": "percentageTurnovers",
    "pct_steals": "percentageSteals", "pct_blocks": "percentageBlocks",
    "pct_blocks_allowed": "percentageBlocksAllowed", "pct_personal_fouls": "percentagePersonalFouls",
    "pct_personal_fouls_drawn": "percentagePersonalFoulsDrawn", "pct_points": "percentagePoints",
}
FOUR = {
    "ft_attempt_rate": "freeThrowAttemptRate", "team_tov_pct": "teamTurnoverPercentage",
    "opp_efg_pct": "oppEffectiveFieldGoalPercentage", "opp_ft_attempt_rate": "oppFreeThrowAttemptRate",
    "opp_team_tov_pct": "oppTeamTurnoverPercentage", "opp_oreb_pct": "oppOffensiveReboundPercentage",
}
TRACK = {
    "speed": "speed", "distance": "distance", "rebound_chances_offensive": "reboundChancesOffensive",
    "rebound_chances_defensive": "reboundChancesDefensive", "rebound_chances_total": "reboundChancesTotal",
    "touches": "touches", "secondary_assists": "secondaryAssists", "ft_assists": "freeThrowAssists",
    "passes": "passes", "contested_fgm": "contestedFieldGoalsMade",
    "contested_fga": "contestedFieldGoalsAttempted", "contested_fg_pct": "contestedFieldGoalPercentage",
    "uncontested_fgm": "uncontestedFieldGoalsMade", "uncontested_fga": "uncontestedFieldGoalsAttempted",
    "uncontested_fg_pct": "uncontestedFieldGoalsPercentage", "defended_at_rim_fgm": "defendedAtRimFieldGoalsMade",
    "defended_at_rim_fga": "defendedAtRimFieldGoalsAttempted",
    "defended_at_rim_fg_pct": "defendedAtRimFieldGoalPercentage", "field_goal_pct": "fieldGoalPercentage",
}
HUSTLE = {
    "contested_shots": "contestedShots", "contested_shots_2pt": "contestedShots2pt",
    "contested_shots_3pt": "contestedShots3pt", "deflections": "deflections",
    "charges_drawn": "chargesDrawn", "screen_assists": "screenAssists",
    "screen_assist_points": "screenAssistPoints",
    "loose_balls_recovered_offensive": "looseBallsRecoveredOffensive",
    "loose_balls_recovered_defensive": "looseBallsRecoveredDefensive",
    "loose_balls_recovered_total": "looseBallsRecoveredTotal",
    "offensive_box_outs": "offensiveBoxOuts", "defensive_box_outs": "defensiveBoxOuts",
    "box_out_player_team_rebounds": "boxOutPlayerTeamRebounds",
    "box_out_player_rebounds": "boxOutPlayerRebounds", "box_outs": "boxOuts",
}
DEF = {
    "matchup_minutes": "matchupMinutes", "partial_possessions": "partialPossessions",
    "switches_on": "switchesOn", "player_points": "playerPoints",
    "defensive_rebounds": "defensiveRebounds", "matchup_assists": "matchupAssists",
    "matchup_turnovers": "matchupTurnovers", "matchup_fgm": "matchupFieldGoalsMade",
    "matchup_fga": "matchupFieldGoalsAttempted", "matchup_fg_pct": "matchupFieldGoalPercentage",
    "matchup_fg3m": "matchupThreePointersMade", "matchup_fg3a": "matchupThreePointersAttempted",
    "matchup_fg3_pct": "matchupThreePointerPercentage",
}
ENDPOINT_MAPS = {
    "BoxScoreTraditionalV3": TRAD, "BoxScoreAdvancedV3": ADV, "BoxScoreMiscV3": MISC,
    "BoxScoreScoringV3": SCORING, "BoxScoreUsageV3": USAGE, "BoxScoreFourFactorsV3": FOUR,
    "BoxScorePlayerTrackV3": TRACK, "BoxScoreHustleV2": HUSTLE, "BoxScoreDefensiveV2": DEF,
}
STAT_COLUMNS = ["player_id", "game_id", "team_id", "opponent_team_id", "minutes", "home_away",
                "was_starter", "dnp_reason", "injury_status", "days_rest"]
for _m in ENDPOINT_MAPS.values():
    STAT_COLUMNS.extend(_m)


def _num(v):
    """NBA sends numbers as numbers already; empty string / None become NULL."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_minutes(v):
    """'28:23' -> 28.383; '' (DNP) -> None."""
    if not v or ":" not in str(v):
        return None
    mm, ss = str(v).split(":")[:2]
    return int(mm) + int(ss) / 60


def _body(payload: dict) -> dict:
    return next(v for k, v in payload.items() if k != "meta")


# BoxScoreDefensiveV2 has served season-scale totals instead of one game's
# (e.g. 3,856 matchup FGA for one player; the historical max is 42). One bad
# player means the whole game's block is bad, so a game's defensive columns are
# blanked together rather than loaded as truth. Limits sit above the
# historical per-game maxima across 171k rows.
DEF_LIMITS = {"matchup_fga": 60, "matchup_fgm": 30, "matchup_fg3a": 25, "matchup_fg3m": 15,
              "partial_possessions": 200, "player_points": 60, "matchup_turnovers": 15,
              "matchup_assists": 20}


def _defense_implausible(row: dict) -> bool:
    if any(row.get(c) is not None and row[c] > lim for c, lim in DEF_LIMITS.items()):
        return True
    # defensive_rebounds duplicates dreb, so a mismatch shows the block is off too.
    return row.get("defensive_rebounds") is not None and row["defensive_rebounds"] != row.get("dreb")


def blank_implausible_defense(game_rows: list[dict]) -> int:
    """Blanks the defensive block for every row of a game if any row looks wrong.

    Returns the number of rows blanked.
    """
    if not any(_defense_implausible(r) for r in game_rows):
        return 0
    for r in game_rows:
        for c in DEF:
            r[c] = None
    return len(game_rows)


def _rest_lookup(games: list[dict]) -> dict:
    """(team_id, game_id) -> days since that team's previous game on the schedule."""
    by_team: dict[str, list[tuple[str, str]]] = {}
    for g in games:
        d = g["gameDateEst"][:10]
        for side in ("homeTeam", "awayTeam"):
            by_team.setdefault(str(g[side]["teamId"]), []).append((d, g["gameId"]))
    rest: dict[tuple[str, str], int] = {}
    for team, entries in by_team.items():
        entries.sort()
        for i, (d, gid) in enumerate(entries):
            if i:
                prev = date.fromisoformat(entries[i - 1][0])
                rest[(team, gid)] = (date.fromisoformat(d) - prev).days
    return rest


# players.position is CHECK-constrained to the five-position set; ESPN only has
# coarse labels for some players, so G/F are approximated (G -> PG, F -> SF).
POSITION_MAP = {"PG": "PG", "SG": "SG", "SF": "SF", "PF": "PF", "C": "C", "G": "PG", "F": "SF"}
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"


def _norm(name: str) -> str:
    """Accent-, case-, punctuation- and suffix-insensitive key for matching names across feeds."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s.replace("-", " "))
    return " ".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def _get(url: str) -> dict:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def espn_bios() -> dict[str, dict]:
    """Every rostered player's bio from ESPN's public team rosters, keyed by normalized name.

    stats.nba.com is unreachable from the cloud, so new players' birthdates
    (players.date_of_birth is NOT NULL) come from ESPN instead.
    """
    bios: dict[str, dict] = {}
    teams = _get(f"{ESPN}/teams")["sports"][0]["leagues"][0]["teams"]
    for t in teams:
        for a in _get(f"{ESPN}/teams/{t['team']['id']}/roster").get("athletes", []):
            dob = (a.get("dateOfBirth") or "")[:10]
            if not dob:
                continue
            inches = a.get("height")
            bios[_norm(a["fullName"])] = {
                "first_name": a.get("firstName") or "",
                "last_name": a.get("lastName") or "",
                "full_name": a["fullName"],
                "position": POSITION_MAP.get(a.get("position", {}).get("abbreviation"), "NA"),
                "height": f"{int(inches) // 12}ft{int(inches) % 12}in" if inches else None,
                "weight": int(a["weight"]) if a.get("weight") else None,
                "date_of_birth": dob,
                "country": {"United States": "USA"}.get(
                    a.get("birthPlace", {}).get("country"), a.get("birthPlace", {}).get("country")),
                "years_exp": a.get("experience", {}).get("years"),
                "draft_year": None,
                "is_active": True,
            }
    return bios


def build_rows(game: dict, eps: dict[str, dict], rest: dict) -> list[dict]:
    """One player_game_stats dict per player in the game's traditional box score."""
    trad = eps["BoxScoreTraditionalV3"]
    sides = {"homeTeam": ("home", "awayTeam"), "awayTeam": ("away", "homeTeam")}
    # Index every endpoint's players by (team_id, person_id) -> statistics.
    indexed: dict[str, dict] = {}
    for name, payload in eps.items():
        if name not in ENDPOINT_MAPS:
            continue
        body = _body(payload)
        idx = {}
        for side in ("homeTeam", "awayTeam"):
            for p in body.get(side, {}).get("players", []):
                idx[str(p["personId"])] = p.get("statistics", {})
        indexed[name] = idx

    rows = []
    tbody = _body(trad)
    for side, (home_away, opp_side) in sides.items():
        team_id = str(tbody[side]["teamId"])
        opp_id = str(tbody[opp_side]["teamId"])
        for p in tbody[side].get("players", []):
            pid = str(p["personId"])
            row = {
                "player_id": pid, "game_id": game["gameId"], "team_id": team_id,
                "opponent_team_id": opp_id, "home_away": home_away,
                "minutes": parse_minutes(p.get("statistics", {}).get("minutes")),
                "was_starter": bool(p.get("position")),
                "dnp_reason": p.get("comment") or None, "injury_status": None,
                "days_rest": rest.get((team_id, game["gameId"])),
            }
            for name, colmap in ENDPOINT_MAPS.items():
                stats = indexed.get(name, {}).get(pid, {})
                for col, field in colmap.items():
                    row[col] = _num(stats.get(field))
            rows.append(row)
    return rows


def run(since: date, commit: bool) -> int:
    with connect() as conn:
        season = current_season(datetime.now(ET).date())
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM raw_nba_schedule WHERE season = %s", (season,))
            srow = cur.fetchone()
            if not srow:
                print(f"no schedule for {season}; run jobs.nba first")
                return 0
            games = [g for d in srow[0]["leagueSchedule"]["gameDates"] for g in d["games"]]
            rest = _rest_lookup(games)

            cur.execute("SELECT DISTINCT game_id FROM player_game_stats")
            done = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT game_id FROM raw_nba_boxscores "
                        "WHERE endpoint='BoxScoreTraditionalV3'")
            collected = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT player_id FROM players")
            known_players = {r[0] for r in cur.fetchall()}

        sched = {g["gameId"]: g for g in games}
        todo = sorted(gid for gid in collected
                      if gid not in done and sched.get(gid, {}).get("gameDateEst", "")[:10] >= since.isoformat())
        if not todo:
            print("nothing to bridge (all collected games already in player_game_stats)")
            return 0

        all_rows, new_games, missing_players, blanked = [], [], {}, 0
        for gid in todo:
            with conn.cursor() as cur:
                cur.execute("SELECT endpoint, payload FROM raw_nba_boxscores WHERE game_id=%s", (gid,))
                eps = dict(cur.fetchall())
            if "BoxScoreTraditionalV3" not in eps:
                continue
            g = sched[gid]
            rows = build_rows(g, eps, rest)
            blanked += blank_implausible_defense(rows)
            all_rows.extend(rows)
            new_games.append(g)
            for side in ("homeTeam", "awayTeam"):
                for pl in _body(eps["BoxScoreTraditionalV3"])[side]["players"]:
                    pid = str(pl["personId"])
                    if pid not in known_players:
                        missing_players[pid] = (f"{pl['firstName']} {pl['familyName']}".strip(),
                                                str(_body(eps["BoxScoreTraditionalV3"])[side]["teamId"]))

        print(f"{'COMMIT' if commit else 'DRY RUN'} | season {season} | {len(todo)} games to bridge | "
              f"{len(all_rows)} player rows | {len(missing_players)} new players to fetch | "
              f"{blanked} rows with defensive-matchup block blanked as implausible")
        if all_rows:
            sample = all_rows[0]
            filled = sum(1 for c in STAT_COLUMNS if sample.get(c) is not None)
            print(f"  sample row {sample['player_id']} g{sample['game_id']}: "
                  f"pts={sample['pts']} rebs={sample['rebs']} asts={sample['asts']} min={sample['minutes']} "
                  f"ts_pct={sample['ts_pct']} matchup_fga={sample['matchup_fga']} | {filled}/{len(STAT_COLUMNS)} cols filled")

        if not commit:
            print("  (dry run — pass --commit to write)")
            return 0

        # 1) new players (FK) via ESPN roster bios
        bios = espn_bios() if missing_players else {}
        for pid in sorted(missing_players):
            name, team_id = missing_players[pid]
            bio = bios.get(_norm(name))
            if not bio:
                print(f"  no ESPN roster bio for {name} ({pid}); skipping its rows")
                continue
            info = {"player_id": pid, "team_id": team_id, **bio}
            cols = list(info)
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO players ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))}) "
                            f"ON CONFLICT (player_id) DO NOTHING", [info[c] for c in cols])
            conn.commit()
            known_players.add(pid)

        # 2) games (FK)
        for g in new_games:
            tbody_scores = {}
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM raw_nba_boxscores WHERE game_id=%s AND endpoint='BoxScoreTraditionalV3'", (g["gameId"],))
                body = _body(cur.fetchone()[0])
                for side, key in (("homeTeam", "home_score"), ("awayTeam", "away_score")):
                    tbody_scores[key] = _num(body[side].get("statistics", {}).get("points"))
                cur.execute(
                    "INSERT INTO games (game_id, game_date, season, game_type, home_team_id, "
                    "away_team_id, tipoff_time_utc, status, home_score, away_score) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'final',%s,%s) ON CONFLICT (game_id) DO NOTHING",
                    (g["gameId"], g["gameDateEst"][:10], season,
                     GAME_TYPE_LABEL.get(g["gameId"][:3], "regular"),
                     str(g["homeTeam"]["teamId"]), str(g["awayTeam"]["teamId"]),
                     g["gameDateTimeUTC"],
                     int(tbody_scores["home_score"]) if tbody_scores["home_score"] else None,
                     int(tbody_scores["away_score"]) if tbody_scores["away_score"] else None))
            conn.commit()

        # 3) player_game_stats (skip rows whose player couldn't be created)
        inserted = 0
        for row in all_rows:
            if row["player_id"] not in known_players:
                continue
            cols = [c for c in STAT_COLUMNS]
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO player_game_stats ({','.join(cols)}) "
                            f"VALUES ({','.join(['%s']*len(cols))})", [row[c] for c in cols])
            inserted += 1
        conn.commit()
        print(f"  committed {len(new_games)} games, {inserted} player_game_stats rows")
        return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=date.fromisoformat,
                        default=datetime.now(ET).date() - timedelta(days=30))
    parser.add_argument("--commit", action="store_true")
    run(parser.parse_args().since, parser.parse_args().commit)
    sys.exit(0)
