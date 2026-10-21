"""CockroachDB connection helper."""

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2

_SCHEMA = Path(__file__).with_name("schema.sql")


@contextmanager
def connect():
    conn = psycopg2.connect(os.environ["COCKROACH_DATABASE_URL"])
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA.read_text())
