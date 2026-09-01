"""
pipeline/load.py: DuckDB upsert on record_id.

No network -- pipeline tests only touch a temp DuckDB file. Skipped
entirely if the "pipeline" extra (duckdb) isn't installed, since the
scraper itself must stay usable without it (see pyproject.toml).

    python -m pytest tests/ -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

duckdb = pytest.importorskip("duckdb")

from pipeline.load import load_file  # noqa: E402


def _write_permits(tmp_path, permits):
    import json
    path = tmp_path / "permits.json"
    path.write_text(json.dumps(permits), encoding="utf-8")
    return path


def _permit(record_id="abc123", permit_number="B25-0001", status="final"):
    return {
        "record_id": record_id,
        "permit_number": permit_number,
        "jurisdiction": "Paso Robles",
        "state": "CA",
        "file_date": "2025-09-02",
        "issue_date": None,
        "final_date": None,
        "status": status,
        "type": "Mechanical",
        "subtype": None,
        "description": "",
        "job_value": None,
        "residential": None,
        "owner_name": None,
        "contractor_name": None,
        "contractor_license": None,
        "street_no": "1310",
        "street": "WHITE CLOVER LN",
        "city": "PASO ROBLES",
        "zipcode": "93446",
        "source_url": "https://example.invalid/CapDetail.aspx",
        "scraped_at": "2026-08-27T18:57:04Z",
    }


def test_load_inserts_rows(tmp_path):
    permits_path = _write_permits(tmp_path, [_permit()])
    db_path = tmp_path / "test.duckdb"

    count = load_file(permits_path, db_path)

    assert count == 1
    con = duckdb.connect(str(db_path))
    rows = con.execute("select record_id, status from permits").fetchall()
    con.close()
    assert rows == [("abc123", "final")]


def test_reload_same_file_does_not_duplicate(tmp_path):
    permits_path = _write_permits(tmp_path, [_permit()])
    db_path = tmp_path / "test.duckdb"

    load_file(permits_path, db_path)
    load_file(permits_path, db_path)

    con = duckdb.connect(str(db_path))
    total = con.execute("select count(*) from permits").fetchone()[0]
    con.close()
    assert total == 1


def test_reload_updates_changed_fields_in_place(tmp_path):
    # Same record_id, status changed between scrapes (e.g. issued -> final).
    # The upsert must overwrite the row, not append a second one -- this is
    # exactly the "re-run without duplicating" contract record_id exists for.
    db_path = tmp_path / "test.duckdb"
    load_file(_write_permits(tmp_path, [_permit(status="active")]), db_path)
    load_file(_write_permits(tmp_path, [_permit(status="final")]), db_path)

    con = duckdb.connect(str(db_path))
    rows = con.execute("select record_id, status from permits").fetchall()
    con.close()
    assert rows == [("abc123", "final")]


def test_scraped_at_trailing_z_is_stripped_for_timestamp_cast(tmp_path):
    # scraped_at from Permit.finalize() is ISO-8601 with a trailing "Z";
    # DuckDB's TIMESTAMP cast rejects that suffix outright, which would
    # otherwise fail every load silently-ish (a hard error, but on a value
    # the scraper always produces the same way -- worth pinning).
    permits_path = _write_permits(tmp_path, [_permit()])
    db_path = tmp_path / "test.duckdb"

    load_file(permits_path, db_path)

    con = duckdb.connect(str(db_path))
    scraped_at = con.execute("select scraped_at from permits").fetchone()[0]
    con.close()
    assert scraped_at.isoformat() == "2026-08-27T18:57:04"


def test_multiple_distinct_records_all_load(tmp_path):
    permits = [_permit(record_id="a1", permit_number="B25-0001"),
               _permit(record_id="a2", permit_number="B25-0002")]
    permits_path = _write_permits(tmp_path, permits)
    db_path = tmp_path / "test.duckdb"

    count = load_file(permits_path, db_path)

    assert count == 2
    con = duckdb.connect(str(db_path))
    total = con.execute("select count(*) from permits").fetchone()[0]
    con.close()
    assert total == 2
