"""
cmd_scrape's output-writing, in isolation from any adapter/network.

Caught scraping COHP (High Point) live: a real permit description contained
a non-ASCII character ("10' × 12' shed"), and cmd_scrape's open() calls
had no explicit encoding. On this machine open()'s platform default is the
system codepage (cp1251), not utf-8, so writing the CSV raised
UnicodeEncodeError partway through -- after the scrape itself had already
succeeded. Locking in utf-8 explicitly here means the bug can't come back
regardless of what locale the box running this happens to have.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.schema import Permit  # noqa: E402
from src import run as run_module  # noqa: E402


class _StubAdapter:
    platform = "accela_citizen_access"

    def __init__(self, permits):
        self._permits = permits

    def scrape(self, start, end):
        return iter(self._permits)


def test_scrape_output_round_trips_non_ascii_text(tmp_path, monkeypatch):
    permit = Permit(
        permit_number="26TMP-006254",
        jurisdiction="High Point",
        state="NC",
        file_date="2026-08-27",
        status="in_review",
        type="Residential Construction Permit",
        description="10' × 12' shed, replace façade",
    ).finalize()

    monkeypatch.setattr(run_module, "OUT", tmp_path)
    monkeypatch.setattr(run_module, "build_adapter", lambda config: _StubAdapter([permit]))
    monkeypatch.setattr(run_module, "load_config", lambda path: {
        "jurisdiction": "High Point", "state": "NC",
    })

    args = type("Args", (), {"config": "unused.yaml", "days": 7})()
    rc = run_module.cmd_scrape(args)
    assert rc == 0

    csv_path = tmp_path / "high_point_permits.csv"
    json_path = tmp_path / "high_point_permits.json"
    report_path = tmp_path / "high_point_report.txt"

    csv_text = csv_path.read_text(encoding="utf-8")
    assert "10' × 12' shed, replace façade" in csv_text

    records = json.loads(json_path.read_text(encoding="utf-8"))
    assert records[0]["description"] == "10' × 12' shed, replace façade"

    assert report_path.read_text(encoding="utf-8")  # writes without raising
