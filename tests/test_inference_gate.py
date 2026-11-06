"""The bridge starts daily inference only once last night's games are bridged."""

from datetime import datetime

from jobs.inference_gate import ET, decide

EARLY = datetime(2026, 11, 7, 2, 0, tzinfo=ET)
LATE = datetime(2026, 11, 7, 11, 30, tzinfo=ET)


def test_ready_when_all_games_final_and_bridged():
    ready, _ = decide(EARLY, [("g1", "final", True), ("g2", "final", True)])
    assert ready


def test_waits_while_a_game_is_not_final():
    ready, reason = decide(EARLY, [("g1", "final", True), ("g2", "scheduled", False)])
    assert not ready and "g2" in reason


def test_waits_while_a_final_game_has_no_box_score_rows():
    ready, _ = decide(EARLY, [("g1", "final", False)])
    assert not ready


def test_starts_anyway_after_the_deadline():
    ready, reason = decide(LATE, [("g1", "scheduled", False)])
    assert ready and "anyway" in reason


def test_ready_after_an_off_night():
    ready, _ = decide(EARLY, [])
    assert ready
