"""One-off test: drain the pool's lowest-credit key and verify exhaustion handling.

Spends ~1 credit per call on a single key until the API reports the quota is
gone, then checks that the pool marks it exhausted and stops handing it out.
Manual use only (workflow_dispatch).
"""

import time

import requests

from jobs import odds
from jobs.db import connect, ensure_schema
from jobs.keypool import KeyPool

URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"


def main() -> None:
    with connect() as conn:
        ensure_schema(conn)
        pool = KeyPool(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key_hash, remaining FROM raw_odds_key_usage "
                "WHERE key_hash = ANY(%s) AND remaining IS NOT NULL "
                "ORDER BY remaining ASC LIMIT 1", (list(pool.keys),))
            h, start = cur.fetchone()
        key = pool.keys[h]
        print(f"draining key {h}, starting at {start} credits")

        calls = 0
        while True:
            r = requests.get(URL, params={"apiKey": key, "regions": "us", "markets": "h2h"},
                             timeout=30)
            calls += 1
            if r.status_code == 429:
                time.sleep(5)
                continue
            if r.status_code == 401:
                print(f"after {calls} calls: HTTP 401, message={r.json().get('message')!r}")
                break
            r.raise_for_status()
            left = r.headers.get("x-requests-remaining")
            if calls % 50 == 0:
                print(f"{calls} calls, {left} credits left")
            if left is not None and float(left) <= 0:
                print(f"header reports 0 credits after {calls} calls")

        # Real code path: does odds._get classify this as quota exhaustion?
        try:
            odds._get("/events", key, {})
            print("RESULT: /events still allowed (free endpoint)")
        except odds.QuotaExhausted:
            print("RESULT: odds._get raised QuotaExhausted on the drained key")
        except odds.BadKey:
            print("RESULT: odds._get raised BadKey (misclassified!)")
        try:
            odds._get("/events/x/odds", key, {"regions": "us", "markets": "player_points"})
        except odds.QuotaExhausted:
            print("RESULT: paid endpoint raised QuotaExhausted")
        except Exception as exc:  # noqa: BLE001
            print(f"RESULT: paid endpoint raised {type(exc).__name__}")

        pool.mark_exhausted(h)
        conn.commit()
        picks = {pool.acquire(odds.CREDITS_PER_EVENT)[0] for _ in range(5)}
        print(f"pool.acquire never returns drained key: {h not in picks} "
              f"(picked {len(picks)} other key(s))")


if __name__ == "__main__":
    main()
