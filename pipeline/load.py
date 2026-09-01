"""
Load scraped permit output into DuckDB, idempotently.

    python -m pipeline.load out/paso_robles_permits.json
    python -m pipeline.load out/high_point_permits.json --db warehouse/building_permit_scraper.duckdb

Upserts on record_id -- the content-derived id validate_batch() already
computes in src/schema.py (state|jurisdiction|permit_number, hashed). This
is deliberately not a second identity scheme: re-running a load with the
same scraped file, or a re-scrape that reproduces the same permits, updates
rows in place instead of duplicating them. Only run() has adapter output as
input; it doesn't scrape, network, or re-derive fields the scraper already
normalized.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .db import DEFAULT_DB_PATH, connect

COLUMNS = [
    "record_id", "permit_number", "jurisdiction", "state",
    "file_date", "issue_date", "final_date", "status", "type", "subtype",
    "description", "job_value", "residential", "owner_name",
    "contractor_name", "contractor_license", "street_no", "street", "city",
    "zipcode", "source_url", "scraped_at",
]

UPSERT_SQL = f"""
INSERT INTO permits ({", ".join(COLUMNS)}, loaded_at)
VALUES ({", ".join("?" for _ in COLUMNS)}, ?)
ON CONFLICT (record_id) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "record_id")},
    loaded_at = excluded.loaded_at
"""


def _row(permit: dict, loaded_at: str) -> tuple:
    # scraped_at is ISO-8601 with a trailing "Z" (see Permit.finalize());
    # DuckDB's TIMESTAMP cast doesn't accept the "Z" suffix, so strip it --
    # the value is already UTC, nothing about the instant changes.
    scraped_at = permit.get("scraped_at")
    if scraped_at and scraped_at.endswith("Z"):
        scraped_at = scraped_at[:-1]
    values = tuple(
        scraped_at if col == "scraped_at" else permit.get(col)
        for col in COLUMNS
    )
    return values + (loaded_at,)


def load_file(path: Path, db_path: Path | str = DEFAULT_DB_PATH) -> int:
    permits = json.loads(path.read_text(encoding="utf-8"))
    if not permits:
        return 0
    loaded_at = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    con = connect(db_path)
    try:
        con.executemany(UPSERT_SQL, [_row(p, loaded_at) for p in permits])
    finally:
        con.close()
    return len(permits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("permits_json", type=Path, help="scrape output, e.g. out/paso_robles_permits.json")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    if not args.permits_json.exists():
        sys.exit(f"no such file: {args.permits_json}")

    count = load_file(args.permits_json, args.db)
    print(f"upserted {count} permits from {args.permits_json} into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
