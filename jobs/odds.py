"""Snapshot NBA player-prop odds into CockroachDB.

Usage:
    python -m jobs.odds open     # first snapshot for games starting in the next 30h
    python -m jobs.odds close    # last pre-tip snapshot for games starting in the next 3h
    python -m jobs.odds close-watch   # stay up and take each game's closing line just before tip

GitHub drops most scheduled runs, so neither job can count on running at a
particular time. An event is fetched again (overwriting its row) when the
stored snapshot is thin (fewer than MIN_BOOKMAKERS books, as when lines are
not posted yet), or, for close, when it was taken well before tip and the game
is now about to start. Each rule fires at most once per RETRY_AFTER, so a game
costs a bounded number of credits however often the job runs.

``close-watch`` exists because of those dropped runs: a one-shot close job only
captures a game if a run happens to land in the hours before tip. The watcher
needs just one run to start, then stays up and fetches each game once, a few
minutes before tip (twice if that snapshot comes back thin).
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from jobs.db import connect, ensure_schema
from jobs.keypool import KeyPool

BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"
# TAB (Australia) lists player props only as alternate ladders: several "Over X.5"
# thresholds per player, no Under. It covers all six stats, blocks and steals included.
MARKETS = [
    "player_points_alternate", "player_rebounds_alternate", "player_assists_alternate",
    "player_threes_alternate", "player_blocks_alternate", "player_steals_alternate",
]
BOOKMAKERS = "tab"
CREDITS_PER_EVENT = len(MARKETS)  # one bookmaker costs the same as one region
WINDOWS = {"open": timedelta(hours=30), "close": timedelta(hours=3)}
MIN_BOOKMAKERS = 1  # a snapshot with no bookmaker means lines are not posted yet
RETRY_AFTER = timedelta(hours=1)
CLOSE_REFRESH_WITHIN = timedelta(minutes=40)  # re-take an early close snapshot this near tip

FINAL_LEAD = timedelta(minutes=4)  # close-watch takes the closing snapshot this long before tip
FINAL_RETRY_LEAD = timedelta(seconds=75)  # ...and retries a thin one this long before tip
FINAL_ACCEPTABLE = timedelta(minutes=10)  # a snapshot taken this near tip already counts as final
WATCH_POLL = timedelta(seconds=30)
WATCH_RELIST_EVERY = timedelta(minutes=10)  # re-read tip times; delays and postponements happen
WATCH_MAX_RUNTIME = timedelta(hours=5, minutes=40)  # inside the workflow's timeout


class QuotaExhausted(Exception):
    pass


class BadKey(Exception):
    pass


def _get(path: str, key: str, params: dict) -> tuple[object, int | None]:
    for attempt in range(3):
        r = requests.get(f"{BASE}{path}", params={**params, "apiKey": key}, timeout=30)
        if r.status_code >= 500 and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        break
    if r.status_code == 401:
        raise QuotaExhausted() if "quota" in r.text.lower() else BadKey()
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    return r.json(), int(float(remaining)) if remaining else None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def needs_snapshot(kind: str, stored: tuple[datetime, int] | None, commence: datetime,
                   now: datetime) -> bool:
    """Whether an event due in this run's window should be fetched.

    Args:
        kind: "open" or "close".
        stored: (fetched_at, bookmaker count) of the stored snapshot, or None.
        commence: The event's tip-off time.
        now: Current time.
    """
    if stored is None:
        return True
    fetched_at, books = stored
    if now - fetched_at < RETRY_AFTER:
        return False
    if books < MIN_BOOKMAKERS:
        return True
    return (kind == "close" and commence - fetched_at > CLOSE_REFRESH_WITHIN
            and commence - now <= CLOSE_REFRESH_WITHIN)


def _snapshot_event(conn, pool: KeyPool, kind: str, event: dict) -> str:
    """Fetch and store one event's player props.

    Returns:
        "saved", "skipped" (the key was rejected; carry on with other events) or
        "stop" (no usable keys left, or the pool could not supply one).
    """
    params = {"bookmakers": BOOKMAKERS, "markets": ",".join(MARKETS), "oddsFormat": "decimal"}
    try:
        h, key = pool.acquire(CREDITS_PER_EVENT)
        payload, remaining = _get(f"/events/{event['id']}/odds", key, params)
    except QuotaExhausted:
        pool.mark_exhausted(h)
        conn.commit()
        print(f"key {h} exhausted; rotating")
        try:
            h, key = pool.acquire(CREDITS_PER_EVENT)
            payload, remaining = _get(f"/events/{event['id']}/odds", key, params)
        except (QuotaExhausted, RuntimeError):
            print("no usable keys left; stopping", file=sys.stderr)
            return "stop"
    except BadKey:
        pool.mark_invalid(h)
        conn.commit()
        print(f"key {h} rejected as invalid; skipping it", file=sys.stderr)
        return "skipped"
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return "stop"

    pool.record_remaining(h, remaining)
    # A later close snapshot always wins; a later open one only if it is no thinner.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_odds_snapshots (event_id, kind, commence_time, key_hash, payload) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (event_id, kind) DO UPDATE SET "
            "payload=excluded.payload, key_hash=excluded.key_hash, fetched_at=now(), "
            "commence_time=excluded.commence_time "
            "WHERE excluded.kind = 'close' OR "
            "COALESCE(jsonb_array_length(excluded.payload->'bookmakers'), 0) >= "
            "COALESCE(jsonb_array_length(raw_odds_snapshots.payload->'bookmakers'), 0)",
            (event["id"], kind, event["commence_time"], h, json.dumps(payload)),
        )
    conn.commit()
    print(f"saved {event['id']} via key {h} ({remaining} credits left)")
    return "saved"


def final_snapshot_due(stored: tuple[datetime, int] | None, commence: datetime,
                       now: datetime) -> bool:
    """Whether close-watch should take the closing snapshot for an event right now.

    Args:
        stored: (fetched_at, bookmaker count) of the stored close snapshot, or None.
        commence: The event's tip-off time.
        now: Current time.
    """
    if not commence - FINAL_LEAD <= now < commence:
        return False
    if stored is None:
        return True
    fetched_at, books = stored
    if fetched_at < commence - FINAL_ACCEPTABLE:
        return True
    # A near-tip snapshot exists; retry once, closer to tip, only if it came back thin.
    return (books < MIN_BOOKMAKERS and now >= commence - FINAL_RETRY_LEAD
            and commence - fetched_at > FINAL_RETRY_LEAD)


def watch_close(list_events, load_stored, snapshot, clock=lambda: datetime.now(timezone.utc),
                sleep=time.sleep) -> int:
    """Take each upcoming game's closing snapshot just before tip, then return.

    Args:
        list_events: Returns the Odds API event list (costs no credits).
        load_stored: Returns {event_id: (fetched_at, bookmaker count)} of close snapshots.
        snapshot: Fetches and stores one event; returns "saved", "skipped" or "stop".
        clock: Current time; replaceable in tests.
        sleep: Sleeps for a number of seconds; replaceable in tests.

    Returns:
        The number of snapshots saved.
    """
    deadline = clock() + WATCH_MAX_RUNTIME
    events: list[dict] = []
    listed_at: datetime | None = None
    saved = 0
    while True:
        now = clock()
        if listed_at is None or now - listed_at >= WATCH_RELIST_EVERY:
            events = list_events()
            listed_at = now
        pending = [e for e in events if now < _parse(e["commence_time"]) <= deadline]
        if not pending:
            print("close-watch: no upcoming games left in this run's reach")
            return saved
        stored = load_stored()
        for event in pending:
            if final_snapshot_due(stored.get(event["id"]), _parse(event["commence_time"]), now):
                outcome = snapshot(event)
                if outcome == "stop":
                    return saved
                saved += outcome == "saved"
        sleep(WATCH_POLL.total_seconds())
        if clock() >= deadline:
            print("close-watch: runtime budget used; a later run picks up the rest")
            return saved


def run_close_watch() -> int:
    with connect() as conn:
        ensure_schema(conn)
        pool = KeyPool(conn)
        conn.commit()

        def list_events() -> list[dict]:
            _, key = pool.acquire()
            events, _ = _get("/events", key, {})  # events listing costs no credits
            return events

        def load_stored() -> dict:
            with conn.cursor() as cur:
                cur.execute("SELECT event_id, fetched_at, "
                            "COALESCE(jsonb_array_length(payload->'bookmakers'), 0) "
                            "FROM raw_odds_snapshots WHERE kind='close'")
                rows = cur.fetchall()
            conn.commit()
            return {r[0]: (r[1], r[2]) for r in rows}

        return watch_close(list_events, load_stored,
                           lambda event: _snapshot_event(conn, pool, "close", event))


def run(kind: str) -> int:
    now = datetime.now(timezone.utc)
    horizon = now + WINDOWS[kind]
    saved = 0
    with connect() as conn:
        ensure_schema(conn)
        pool = KeyPool(conn)
        conn.commit()

        h, key = pool.acquire()
        events, remaining = _get("/events", key, {})  # events listing costs no credits
        due = [e for e in events if now < _parse(e["commence_time"]) <= horizon]

        with conn.cursor() as cur:
            cur.execute("SELECT event_id, fetched_at, "
                        "COALESCE(jsonb_array_length(payload->'bookmakers'), 0) "
                        "FROM raw_odds_snapshots WHERE kind=%s", (kind,))
            stored = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        due = [e for e in due
               if needs_snapshot(kind, stored.get(e["id"]), _parse(e["commence_time"]), now)]
        print(f"{kind}: {len(due)} events to snapshot")

        for event in due:
            outcome = _snapshot_event(conn, pool, kind, event)
            if outcome == "stop":
                break
            saved += outcome == "saved"
    return saved


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in (*WINDOWS, "close-watch"):
        sys.exit("usage: python -m jobs.odds [open|close|close-watch]")
    if sys.argv[1] == "close-watch":
        run_close_watch()
    else:
        run(sys.argv[1])
