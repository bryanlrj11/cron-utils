"""Snapshot NBA player-prop odds into CockroachDB.

Usage:
    python -m jobs.odds open     # first snapshot for games starting in the next 30h
    python -m jobs.odds close    # last pre-tip snapshot for games starting in the next 3h

GitHub drops most scheduled runs, so neither job can count on running at a
particular time. An event is fetched again (overwriting its row) when the
stored snapshot is thin (fewer than MIN_BOOKMAKERS books, as when lines are
not posted yet), or, for close, when it was taken well before tip and the game
is now about to start. Each rule fires at most once per RETRY_AFTER, so a game
costs a bounded number of credits however often the job runs.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from jobs.db import connect, ensure_schema
from jobs.keypool import KeyPool

BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"
MARKETS = [
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_blocks", "player_steals",
]
REGIONS = "us"
CREDITS_PER_EVENT = len(MARKETS) * len(REGIONS.split(","))
WINDOWS = {"open": timedelta(hours=30), "close": timedelta(hours=3)}
MIN_BOOKMAKERS = 3
RETRY_AFTER = timedelta(hours=1)
CLOSE_REFRESH_WITHIN = timedelta(minutes=40)  # re-take an early close snapshot this near tip


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
            try:
                h, key = pool.acquire(CREDITS_PER_EVENT)
                payload, remaining = _get(
                    f"/events/{event['id']}/odds", key,
                    {"regions": REGIONS, "markets": ",".join(MARKETS), "oddsFormat": "decimal"},
                )
            except QuotaExhausted:
                pool.mark_exhausted(h)
                conn.commit()
                print(f"key {h} exhausted; rotating")
                try:
                    h, key = pool.acquire(CREDITS_PER_EVENT)
                    payload, remaining = _get(
                        f"/events/{event['id']}/odds", key,
                        {"regions": REGIONS, "markets": ",".join(MARKETS), "oddsFormat": "decimal"},
                    )
                except (QuotaExhausted, RuntimeError):
                    print("no usable keys left; stopping", file=sys.stderr)
                    break
            except BadKey:
                pool.mark_invalid(h)
                conn.commit()
                print(f"key {h} rejected as invalid; skipping it", file=sys.stderr)
                continue
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                break

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
            saved += 1
            print(f"saved {event['id']} via key {h} ({remaining} credits left)")
    return saved


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in WINDOWS:
        sys.exit("usage: python -m jobs.odds [open|close]")
    run(sys.argv[1])
