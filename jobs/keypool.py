"""Rotating pool of Odds API keys with state persisted in the database.

Each Actions run starts with no memory, so remaining-credit counts and
exhaustion dates live in ``raw_odds_key_usage``. Keys are only ever stored as a
short hash; nothing here logs or returns the raw key except ``acquire``.
"""

import hashlib
import os
from datetime import datetime, timezone

_NEVER = datetime(2100, 1, 1, tzinfo=timezone.utc)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _next_month_start() -> datetime:
    now = datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def load_keys() -> list[str]:
    raw = os.environ.get("ODDS_API_KEYS") or os.environ.get("ODDS_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("Set ODDS_API_KEYS (comma-separated) in the environment")
    return list(dict.fromkeys(keys))


class KeyPool:
    def __init__(self, conn, keys: list[str] | None = None):
        self.conn = conn
        self.keys = {key_hash(k): k for k in (keys or load_keys())}
        with conn.cursor() as cur:
            for h in self.keys:
                cur.execute(
                    "INSERT INTO raw_odds_key_usage (key_hash) VALUES (%s) ON CONFLICT DO NOTHING",
                    (h,),
                )

    def acquire(self, min_credits: int = 1) -> tuple[str, str]:
        """Return (hash, key) of the usable key with the most credits left.

        Keys with unknown balance are tried first; keys below ``min_credits``
        are skipped so a slate is not left half-fetched.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT key_hash FROM raw_odds_key_usage
                WHERE key_hash = ANY(%s)
                  AND (exhausted_until IS NULL OR exhausted_until <= now())
                  AND (remaining IS NULL OR remaining >= %s)
                ORDER BY remaining DESC NULLS FIRST
                LIMIT 1
                """,
                (list(self.keys), min_credits),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError("No Odds API key with enough credits left")
        return row[0], self.keys[row[0]]

    def record_remaining(self, h: str, remaining: int | None) -> None:
        if remaining is None:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE raw_odds_key_usage SET remaining=%s, updated_at=now() WHERE key_hash=%s",
                (remaining, h),
            )

    def mark_exhausted(self, h: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE raw_odds_key_usage SET remaining=0, exhausted_until=%s, updated_at=now() "
                "WHERE key_hash=%s",
                (_next_month_start(), h),
            )

    def mark_invalid(self, h: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE raw_odds_key_usage SET exhausted_until=%s, updated_at=now() WHERE key_hash=%s",
                (_NEVER, h),
            )
