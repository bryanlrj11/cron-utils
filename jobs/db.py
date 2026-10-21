"""CockroachDB connection helper."""

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2

_SCHEMA = Path(__file__).with_name("schema.sql")
_DOTENV = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    """Local runs read secrets from .env at the repo root; Actions injects repo secrets."""
    if not _DOTENV.exists():
        return
    for line in _DOTENV.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()


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
