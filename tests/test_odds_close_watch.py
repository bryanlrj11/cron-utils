"""close-watch takes each game's closing snapshot just before tip, on a fake clock."""

from datetime import datetime, timedelta, timezone

import pytest

from jobs import odds

START = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)


class Harness:
    """A fake clock plus an in-memory snapshot store standing in for the database."""

    def __init__(self, tips, books=1, thin_first=False, events_after=None):
        self.now = START
        self.tips = dict(tips)
        self.events_after = events_after or {}
        self.books = books
        self.thin_first = thin_first
        self.stored = {}
        self.fetches = []
        self.listings = 0

    def list_events(self):
        self.listings += 1
        tips = self.tips
        for after, replacement in self.events_after.items():
            if self.now >= START + after:
                tips = replacement
        return [{"id": event_id, "commence_time": tip.isoformat()} for event_id, tip in tips.items()]

    def snapshot(self, event):
        books = 0 if self.thin_first and event["id"] not in self.stored else self.books
        self.stored[event["id"]] = (self.now, books)
        self.fetches.append((event["id"], self.now))
        return "saved"

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)

    def run(self):
        return odds.watch_close(
            self.list_events, lambda: dict(self.stored), self.snapshot,
            clock=lambda: self.now, sleep=self.sleep,
        )


def tip_in(**delta):
    return START + timedelta(**delta)


def lead(harness, event_id, tip):
    return [tip - at for e, at in harness.fetches if e == event_id]


def test_each_game_is_fetched_once_in_the_final_minutes_before_tip():
    harness = Harness({"a": tip_in(hours=1), "b": tip_in(hours=1), "c": tip_in(hours=3)})

    assert harness.run() == 3

    for event_id, tip in harness.tips.items():
        (gap,) = lead(harness, event_id, tip)
        assert odds.FINAL_RETRY_LEAD < gap <= odds.FINAL_LEAD + odds.WATCH_POLL


def test_a_thin_closing_snapshot_is_retried_once_closer_to_tip():
    harness = Harness({"a": tip_in(hours=1)}, thin_first=True)

    assert harness.run() == 2

    first, second = lead(harness, "a", harness.tips["a"])
    assert first > odds.FINAL_RETRY_LEAD >= second > timedelta(0)


def test_a_job_that_starts_just_before_tip_still_takes_the_snapshot():
    harness = Harness({"a": tip_in(minutes=2)})

    assert harness.run() == 1
    assert lead(harness, "a", harness.tips["a"])[0] <= timedelta(minutes=2)


def test_games_that_have_already_tipped_are_ignored():
    harness = Harness({"a": tip_in(minutes=-30)})

    assert harness.run() == 0
    assert harness.fetches == []


def test_a_game_beyond_the_runtime_budget_is_left_for_the_next_run():
    harness = Harness({"a": tip_in(hours=1), "late": START + odds.WATCH_MAX_RUNTIME + timedelta(hours=1)})

    assert harness.run() == 1
    assert [event_id for event_id, _ in harness.fetches] == ["a"]


def test_a_delayed_tip_off_is_picked_up_when_events_are_relisted():
    original, delayed = tip_in(hours=1), tip_in(hours=1, minutes=40)
    harness = Harness({"a": original}, events_after={timedelta(minutes=30): {"a": delayed}})

    assert harness.run() == 1
    (gap,) = lead(harness, "a", delayed)
    assert gap <= odds.FINAL_LEAD + odds.WATCH_POLL
    assert harness.fetches[0][1] > original


def test_snapshot_stop_ends_the_watch_without_more_fetches():
    harness = Harness({"a": tip_in(hours=1), "b": tip_in(hours=2)})
    harness.snapshot = lambda event: harness.fetches.append((event["id"], harness.now)) or "stop"

    assert harness.run() == 0
    assert len(harness.fetches) == 1


def test_no_events_returns_immediately():
    harness = Harness({})

    assert harness.run() == 0
    assert harness.listings == 1


@pytest.mark.parametrize(
    ("stored_seconds_before_tip", "books", "now_seconds_before_tip", "due"),
    [
        (None, 0, 600, False),    # too early
        (None, 0, 200, True),     # inside the final window, nothing stored
        (3600, 1, 200, True),     # only an early snapshot exists
        (200, 1, 100, False),     # fresh near-tip snapshot, healthy
        (200, 0, 100, False),     # thin, but not yet time for the retry
        (200, 0, 60, True),       # thin, retry window reached
        (60, 0, 30, False),       # the retry already happened
        (None, 0, -5, False),     # game started
    ],
)
def test_final_snapshot_due(stored_seconds_before_tip, books, now_seconds_before_tip, due):
    tip = tip_in(hours=1)
    stored = None
    if stored_seconds_before_tip is not None:
        stored = (tip - timedelta(seconds=stored_seconds_before_tip), books)

    now = tip - timedelta(seconds=now_seconds_before_tip)

    assert odds.final_snapshot_due(stored, tip, now) is due


def test_budget_exhaustion_reports_the_event_list_so_a_successor_can_start():
    harness = Harness({"a": tip_in(hours=1), "b": tip_in(hours=8)})
    reported = []

    odds.watch_close(
        harness.list_events, lambda: dict(harness.stored), harness.snapshot,
        clock=lambda: harness.now, sleep=harness.sleep, on_budget_used=reported.append,
    )

    assert len(reported) == 1
    assert {event["id"] for event in reported[0]} == {"a", "b"}


class FakeResponse:
    def raise_for_status(self):
        pass


def test_a_successor_is_dispatched_only_when_a_game_is_still_ahead(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    ahead = [{"id": "a", "commence_time": (START + timedelta(hours=2)).isoformat()}]
    behind = [{"id": "a", "commence_time": (START - timedelta(hours=2)).isoformat()}]

    assert odds.dispatch_successor(ahead, START, post=post) is True
    assert odds.dispatch_successor(behind, START, post=post) is False
    assert calls == ["https://api.github.com/repos/owner/repo/actions/workflows/odds-close.yml/dispatches"]


def test_a_failed_hand_over_is_swallowed(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def post(url, **kwargs):
        raise odds.requests.ConnectionError("down")

    ahead = [{"id": "a", "commence_time": (START + timedelta(hours=2)).isoformat()}]

    assert odds.dispatch_successor(ahead, START, post=post) is False


def test_an_idle_watcher_exits_without_handing_over_when_the_next_game_is_far_off():
    harness = Harness({"a": tip_in(hours=20)})
    reported = []

    saved = odds.watch_close(
        harness.list_events, lambda: dict(harness.stored), harness.snapshot,
        clock=lambda: harness.now, sleep=harness.sleep, on_budget_used=reported.append,
    )

    assert saved == 0
    assert reported == []
    assert harness.now == START
