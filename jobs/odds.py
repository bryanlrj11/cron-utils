"""Snapshot NBA player-prop odds into CockroachDB.

Usage:
    python -m jobs.odds open     # first snapshot for games starting in the next 30h
    python -m jobs.odds close    # snapshot for games starting in the next 40 min
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
WINDOWS = {"open": timedelta(hours=30), "close": timedelta(minutes=40)}


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
            cur.execute("SELECT event_id FROM raw_odds_snapshots WHERE kind=%s", (kind,))
            done = {r[0] for r in cur.fetchall()}
        due = [e for e in due if e["id"] not in done]
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
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO raw_odds_snapshots (event_id, kind, commence_time, key_hash, payload) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (event_id, kind) DO NOTHING",
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
