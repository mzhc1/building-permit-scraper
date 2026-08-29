"""
Offline tests. No network. These are the validation gates — if the normalizer
is wrong, everything downstream is confidently wrong, which is the worst kind.

    python -m pytest tests/ -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.schema import (  # noqa: E402
    Permit, parse_date, parse_money, normalize_status,
    infer_residential, split_address, normalize_zip, validate_batch,
    parse_address_parts,
)


def test_date_formats():
    assert parse_date("03/14/2026") == "2026-03-14"
    assert parse_date("2026-03-14") == "2026-03-14"
    assert parse_date("14-Mar-2026") == "2026-03-14"
    assert parse_date("Mar 14, 2026") == "2026-03-14"
    assert parse_date("") is None
    assert parse_date("garbage") is None


def test_money():
    assert parse_money("$64,200.00") == 64200.0
    assert parse_money("64200") == 64200.0
    assert parse_money("") is None
    assert parse_money("N/A") is None
    assert parse_money("-500") is None  # negative valuation is bad data, not a value


def test_status_maps_to_canonical():
    assert normalize_status("Issued") == "active"
    assert normalize_status("FINALED") == "final"
    assert normalize_status("Plan Check") == "in_review"
    assert normalize_status("Revoked") == "expired"
    assert normalize_status("something weird") == "unknown"
    assert normalize_status(None) == "unknown"


def test_status_maps_multi_word_accela_phrasing():
    # COHP (High Point) uses these exact multi-word phrases where other
    # Accela jurisdictions use a single word ("Submitted", "Pending").
    # normalize_status matches on the full trimmed/lowercased string, so a
    # phrase not literally in _STATUS_MAP falls through to "unknown" even
    # though it is clearly an in-review state. Caught scraping COHP live.
    assert normalize_status("Application Submitted") == "in_review"
    assert normalize_status("Awaiting Applicant Response") == "in_review"


def test_residential_refuses_to_guess():
    assert infer_residential("Single Family Dwelling", "reroof") is True
    assert infer_residential("Tenant Improvement", "office buildout") is False
    # both signals present -> refuse
    assert infer_residential("Mixed-Use", "residential over retail") is None
    # no signal -> refuse
    assert infer_residential("", "") is None
    assert infer_residential("MISC") is None


def test_address_split():
    assert split_address("1042 Oak St") == ("1042", "Oak St")
    assert split_address("12B Elm Avenue") == ("12B", "Elm Avenue")
    assert split_address("Corner of 5th and Main") == (None, "Corner of 5th and Main")
    assert split_address("") == (None, None)


def test_address_parts_splits_city_state_zip():
    # real Paso Robles (Accela/PRCITY) address cells, captured live
    assert parse_address_parts("1310 WHITE CLOVER LN, PASO ROBLES CA 93446") == (
        "1310", "WHITE CLOVER LN", "PASO ROBLES", "CA", "93446",
    )


def test_address_parts_keeps_unit_number_in_street():
    assert parse_address_parts("2800 RIVERSIDE AVE, 103, PASO ROBLES CA 93446") == (
        "2800", "RIVERSIDE AVE, 103", "PASO ROBLES", "CA", "93446",
    )


def test_address_parts_never_invents_missing_state_or_zip():
    # portal has no state/zip on file for this record -- must stay None,
    # not guess, and the raw text is kept as the street fallback.
    street_no, street, city, state, zipcode = parse_address_parts(
        "7990 SUNDANCE, United States"
    )
    assert street_no == "7990"
    assert city is None and state is None and zipcode is None
    assert "SUNDANCE" in street

    street_no, street, city, state, zipcode = parse_address_parts(
        "5874 Iron Gate RD, Paso Robles United States"
    )
    assert city is None and state is None and zipcode is None


def test_address_parts_handles_missing_zip_with_country_suffix():
    assert parse_address_parts("2005 Oak ST, Paso Robles CA United States") == (
        "2005", "Oak ST", "Paso Robles", "CA", None,
    )


def test_address_parts_empty():
    assert parse_address_parts("") == (None, None, None, None, None)
    assert parse_address_parts(None) == (None, None, None, None, None)


def test_zip():
    assert normalize_zip("94549-1234") == "94549"
    assert normalize_zip("94549") == "94549"
    assert normalize_zip("bad") is None


def _permit(number, **kw):
    base = dict(
        permit_number=number, jurisdiction="Testville",
        state="CA", file_date="2026-03-14",
    )
    base.update(kw)
    return Permit(**base)


def test_validate_rejects_incomplete():
    batch = [_permit("A-1"), Permit(permit_number="A-2")]  # second has no jurisdiction
    kept, report = validate_batch(batch)
    assert len(kept) == 1
    assert report.rejected == 1
    assert report.total == 2


def test_validate_dedupes_on_stable_id():
    batch = [_permit("A-1"), _permit("A-1"), _permit("A-2")]
    kept, report = validate_batch(batch)
    assert report.unique == 2
    assert report.duplicates_dropped == 1
    # ids are content-derived, so a re-run produces the same id
    assert kept[0].record_id == _permit("A-1").finalize().record_id


def test_fill_rate_is_honest():
    batch = [_permit("A-1", owner_name="Jane"), _permit("A-2")]
    kept, report = validate_batch(batch)
    assert report.fill_rate["owner_name"] == 0.5
    assert report.fill_rate["permit_number"] == 1.0
