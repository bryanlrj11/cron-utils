"""recompute_days_rest retries CockroachDB serialization aborts instead of failing the bridge."""

import psycopg2.errors
import pytest

from jobs import bridge


class FakeConnection:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: None)


def test_retries_after_a_serialization_failure(monkeypatch):
    outcomes = [psycopg2.errors.SerializationFailure(), 7]

    def once(conn, since, commit):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(bridge, "_recompute_days_rest_once", once)
    conn = FakeConnection()
    assert bridge.recompute_days_rest(conn, None, True) == 7
    assert conn.rollbacks == 1


def test_gives_up_after_the_last_attempt(monkeypatch):
    def always_fails(conn, since, commit):
        raise psycopg2.errors.SerializationFailure()

    monkeypatch.setattr(bridge, "_recompute_days_rest_once", always_fails)
    with pytest.raises(psycopg2.errors.SerializationFailure):
        bridge.recompute_days_rest(FakeConnection(), None, True, attempts=3)
