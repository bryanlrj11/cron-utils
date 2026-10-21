CREATE TABLE IF NOT EXISTS raw_odds_key_usage (
    key_hash        TEXT PRIMARY KEY,
    remaining       INT,
    exhausted_until TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_odds_snapshots (
    id            INT8 PRIMARY KEY DEFAULT unique_rowid(),
    event_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    commence_time TIMESTAMPTZ NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_hash      TEXT,
    payload       JSONB NOT NULL,
    UNIQUE (event_id, kind)
);

CREATE TABLE IF NOT EXISTS raw_boxscores (
    game_id    TEXT PRIMARY KEY,
    game_date  DATE NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload    JSONB NOT NULL
);
