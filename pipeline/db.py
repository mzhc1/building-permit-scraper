"""
DuckDB schema for loaded permits.

One file, no server -- matches the project's own scraper output (CSV/JSON on
disk) with a storage layer that's just as easy to commit a small sample of.
Schema mirrors src/schema.py's Permit fields exactly; this module doesn't
re-derive or reinterpret anything the scraper already normalized.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

DEFAULT_DB_PATH = Path("warehouse/shovels_gap.duckdb")

# Column order/types follow src/schema.py's Permit dataclass. record_id is
# the content-derived id validate_batch() already computes -- the upsert key
# below relies on it being unique and stable across re-scrapes.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS permits (
    record_id           VARCHAR PRIMARY KEY,
    permit_number       VARCHAR,
    jurisdiction         VARCHAR,
    state                VARCHAR,
    file_date            DATE,
    issue_date           DATE,
    final_date           DATE,
    status               VARCHAR,
    type                 VARCHAR,
    subtype              VARCHAR,
    description          VARCHAR,
    job_value            DOUBLE,
    residential          BOOLEAN,
    owner_name           VARCHAR,
    contractor_name      VARCHAR,
    contractor_license   VARCHAR,
    street_no            VARCHAR,
    street               VARCHAR,
    city                 VARCHAR,
    zipcode              VARCHAR,
    source_url           VARCHAR,
    scraped_at           TIMESTAMP,
    loaded_at            TIMESTAMP NOT NULL DEFAULT current_timestamp
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA_SQL)
    return con
