"""unfinished_slate_games decides when a night's slate is done, on a fixed clock."""

from datetime import datetime, timezone

from jobs.nba import unfinished_slate_games


def game(game_id, day, tip, status):
    return {"gameId": game_id, "gameDateEst": f"{day}T00:00:00Z",
            "gameDateTimeUTC": tip, "gameStatus": status}


LAST_NIGHT = [game("0022600100", "2026-11-05", "2026-11-06T00:00:00Z", 3),
              game("0022600101", "2026-11-05", "2026-11-06T03:00:00Z", 3)]
TONIGHT_EARLY = "2026-11-07T00:00:00Z"
TONIGHT_LATE = "2026-11-07T03:00:00Z"


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_before_first_tip_nothing_to_wait_for():
    games = LAST_NIGHT + [game("0022600110", "2026-11-06", TONIGHT_EARLY, 1)]
    assert unfinished_slate_games(games, at("2026-11-06T22:00:00")) == []


def test_waits_for_late_game_that_has_not_tipped_yet():
    games = LAST_NIGHT + [game("0022600110", "2026-11-06", TONIGHT_EARLY, 3),
                          game("0022600111", "2026-11-06", TONIGHT_LATE, 1)]
    pending = unfinished_slate_games(games, at("2026-11-07T02:40:00"))
    assert [g["gameId"] for g in pending] == ["0022600111"]


def test_live_games_block_until_final():
    games = [game("0022600110", "2026-11-06", TONIGHT_EARLY, 2),
             game("0022600111", "2026-11-06", TONIGHT_LATE, 2)]
    assert len(unfinished_slate_games(games, at("2026-11-07T04:30:00"))) == 2


def test_slate_done_when_every_game_is_final():
    games = [game("0022600110", "2026-11-06", TONIGHT_EARLY, 3),
             game("0022600111", "2026-11-06", TONIGHT_LATE, 3)]
    assert unfinished_slate_games(games, at("2026-11-07T06:30:00")) == []


def test_postponed_game_stops_blocking_after_six_hours():
    games = [game("0022600110", "2026-11-06", TONIGHT_EARLY, 3),
             game("0022600111", "2026-11-06", TONIGHT_LATE, 1)]
    assert unfinished_slate_games(games, at("2026-11-07T08:59:00")) != []
    assert unfinished_slate_games(games, at("2026-11-07T09:01:00")) == []


def test_preseason_games_are_ignored():
    games = [game("0012600110", "2026-11-06", TONIGHT_EARLY, 2)]
    assert unfinished_slate_games(games, at("2026-11-07T01:00:00")) == []
