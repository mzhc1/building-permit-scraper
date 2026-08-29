"""
Parser tests against a saved Accela-shaped results grid. No network.

Why this matters: the live portal is the one thing you cannot control. If the
parser is only ever exercised against the live site, every failure is
ambiguous — was it the site or the code? A fixture makes the code half of that
question answerable offline, in milliseconds.

When a real jurisdiction breaks: save its HTML into tests/fixtures/ and add a
case here. That is how the adapter stays honest as jurisdictions redesign.
"""

import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.adapters.accela import AccelaAdapter, PaginationMismatch  # noqa: E402
from src.schema import validate_batch  # noqa: E402

CONFIG = {
    "jurisdiction": "Testville",
    "state": "CA",
    "city": "Testville",
    "base_url": "https://example.invalid/CitizenAccess",
    "min_delay": 0,
    "jitter": 0,
}

# Realistic Accela grid: note the columns are NOT in a canonical order and use
# jurisdiction-specific header names. Index-based parsing would mangle this.
GRID = """
<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
  <tr>
    <th>Date Opened</th><th>Record Number</th><th>Record Type</th>
    <th>Project Description</th><th>Address</th><th>Status</th>
    <th>Valuation</th><th>Owner</th>
  </tr>
  <tr>
    <td>03/14/2026</td>
    <td><a href="/CitizenAccess/Cap/CapDetail.aspx?id=BLD26-0042">BLD26-0042</a></td>
    <td>Residential Alteration</td>
    <td>Reroof single family dwelling</td>
    <td>1042 Oak St</td>
    <td>Issued</td>
    <td>$64,200.00</td>
    <td>SMITH, JANE</td>
  </tr>
  <tr>
    <td>03/15/2026</td>
    <td><a href="/CitizenAccess/Cap/CapDetail.aspx?id=BLD26-0043">BLD26-0043</a></td>
    <td>Commercial TI</td>
    <td>Tenant improvement, office buildout</td>
    <td>88 Market Avenue</td>
    <td>Plan Check</td>
    <td></td>
    <td></td>
  </tr>
  <tr>
    <td>03/15/2026</td><td></td><td>Junk</td><td></td><td></td><td></td><td></td><td></td>
  </tr>
</table>
"""


def _parse(html):
    adapter = AccelaAdapter(CONFIG)
    soup = BeautifulSoup(html, "html.parser")
    table = adapter._find_grid(soup)
    assert table is not None, "grid not located"
    columns = adapter._column_index(table)
    return adapter, list(adapter._rows_to_permits(table, columns))


def test_finds_grid_and_maps_headers_by_name():
    adapter, permits = _parse(GRID)
    # third row has no record number and must be skipped, not yielded blank
    assert len(permits) == 2
    assert [p.permit_number for p in permits] == ["BLD26-0042", "BLD26-0043"]


def test_fields_normalized_correctly():
    _, permits = _parse(GRID)
    first = permits[0]
    assert first.file_date == "2026-03-14"
    assert first.status == "active"          # "Issued" -> canonical
    assert first.job_value == 64200.0
    assert first.street_no == "1042"
    assert first.street == "Oak St"
    assert first.residential is True
    assert first.city == "Testville"          # GRID fixture has a bare street, no city/state/zip
    assert first.owner_name == "SMITH, JANE"
    assert first.source_url.endswith("BLD26-0042")


def test_missing_values_stay_none_not_empty_string():
    _, permits = _parse(GRID)
    second = permits[1]
    assert second.job_value is None
    assert second.owner_name is None
    assert second.status == "in_review"
    assert second.residential is False       # "Commercial TI" -> commercial


def test_batch_validates_clean():
    _, permits = _parse(GRID)
    kept, report = validate_batch(permits)
    assert report.unique == 2
    assert report.rejected == 0
    assert report.fill_rate["permit_number"] == 1.0
    assert report.fill_rate["job_value"] == 0.5



# Paso Robles (PRCITY) actual grid shape: a "Showing X of Y | Download
# results" toolbar row comes BEFORE the real <th> header row, and that
# toolbar cell wraps its own nested <table>/<tr> which BeautifulSoup's
# find_all("tr") flattens in, duplicating it. Naively treating the first
# <tr> as the header (and everything after as data) silently parsed the
# toolbar as data and produced rows with every field blank. Caught by
# running the real adapter against the live portal.
GRID_WITH_TOOLBAR = """
<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
  <tr><td>
    <table><tr>
      <td>Showing 1-10 of 26</td><td>|</td><td>Download results</td>
    </tr></table>
  </td></tr>
  <tr>
    <th></th><th>Date</th><th>Record Number</th><th>Project Name</th>
    <th>Address</th><th>Status</th><th>Record Type</th><th>Action</th><th></th>
  </tr>
  <tr>
    <td></td><td>08/26/2026</td>
    <td><a href="/PRCITY/Cap/CapDetail.aspx?id=B26-0698">B26-0698</a></td>
    <td></td><td>2640 Riverside AVE, Paso Robles CA</td><td>Applied</td>
    <td>Demolition Permit / Mixed Use</td><td></td><td></td>
  </tr>
</table>
"""

NO_RESULTS_PAGE = """
<html><body>
<table><tr><td>some unrelated search form widget</td></tr></table>
<p>Notice: Your search returned no results. Please modify your search
criteria and try again.</p>
</body></html>
"""


def test_toolbar_row_not_mistaken_for_header():
    adapter, permits = _parse(GRID_WITH_TOOLBAR)
    assert len(permits) == 1
    assert permits[0].permit_number == "B26-0698"
    assert permits[0].file_date == "2026-08-26"
    assert permits[0].status == "in_review"   # "Applied" -> canonical


def test_no_results_notice_returns_no_grid_not_the_form():
    adapter = AccelaAdapter(CONFIG)
    soup = BeautifulSoup(NO_RESULTS_PAGE, "html.parser")
    assert adapter._find_grid(soup) is None



# Paso Robles' grid ends with a "< Prev 1 2 3 Next >" pager row that is
# itself a <tr> sibling of the data rows (single <td colspan> wrapping a
# nested aca_pagination table). Without filtering it out, its cells shift
# into the real columns and "< Prev" lands in whatever column maps to
# permit_number, with a blank date column -- a fake "missing file_date"
# rejection that has nothing to do with the actual data. Caught by dumping
# rejected records from a live scrape and finding permit_number="< Prev".
GRID_WITH_PAGER = """
<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
  <tr><th>Date</th><th>Record Number</th><th>Status</th></tr>
  <tr>
    <td>08/03/2026</td>
    <td><a href="/PRCITY/Cap/CapDetail.aspx?id=B26-0626">B26-0626</a></td>
    <td>Withdrawn</td>
  </tr>
  <tr align="center" class="ACA_Table_Pages ACA_Table_Pages_FontSize" valign="bottom">
    <td colspan="3">
      <table class="aca_pagination" role="presentation"><tr>
        <td class="aca_pagination_td aca_pagination_PrevNext">
          <span class="aca_simple_text font11px">&lt; Prev</span>
        </td>
        <td class="aca_pagination_td"><span class="SelectedPageButton">1</span></td>
        <td class="aca_pagination_td aca_pagination_PrevNext">
          <a href="javascript:__doPostBack('x','')">Next &gt;</a>
        </td>
      </tr></table>
    </td>
  </tr>
</table>
"""


GRID_WITH_FULL_ADDRESS = GRID.replace(
    "<td>1042 Oak St</td>",
    "<td>1042 Oak St, Testville CA 94549</td>",
)


def test_combined_address_splits_city_state_zip_into_own_fields():
    _, permits = _parse(GRID_WITH_FULL_ADDRESS)
    first = permits[0]
    assert first.street == "Oak St"
    assert first.city == "Testville"
    assert first.zipcode == "94549"


def test_source_url_does_not_duplicate_agency_segment():
    # Real href captured from the live PRCITY grid: it's already an
    # agency-qualified absolute path (starts with "/PRCITY/..."), not a
    # path relative to base_url (which itself already ends in "/PRCITY").
    # Naively prefixing base_url again produced
    # ".../PRCITY/PRCITY/Cap/CapDetail.aspx?...".
    href = (
        "/PRCITY/Cap/CapDetail.aspx?Module=Building&TabName=Building"
        "&capID1=26REC&capID2=00000&capID3=001DW&agencyCode=PRCITY"
        "&IsToShowInspection="
    )
    config = {
        "jurisdiction": "Paso Robles",
        "state": "CA",
        "city": "El Paso De Robles",
        "base_url": "https://aca-prod.accela.com/PRCITY",
        "min_delay": 0,
        "jitter": 0,
    }
    adapter = AccelaAdapter(config)
    resolved = adapter._resolve_href(href)
    assert resolved == (
        "https://aca-prod.accela.com/PRCITY/Cap/CapDetail.aspx"
        "?Module=Building&TabName=Building&capID1=26REC&capID2=00000"
        "&capID3=001DW&agencyCode=PRCITY&IsToShowInspection="
    )
    assert resolved.count("/PRCITY") == 1


def test_pager_row_not_parsed_as_a_permit():
    adapter, permits = _parse(GRID_WITH_PAGER)
    assert len(permits) == 1
    assert permits[0].permit_number == "B26-0626"


# High Point (COHP) actual grid shape: the record-number column is headed
# "Record ID", not "Record Number" or "Permit Number" -- neither needle in
# the original HEADER_MAP matched it, so permit_number came back empty for
# every row and _rows_to_permits silently dropped the entire grid (the
# "no permit_number -> skip" guard exists for genuinely blank cells, not for
# an unmapped header). Caught by dumping the real search-results page for
# COHP before scraping and diffing its header row against HEADER_MAP.
GRID_WITH_RECORD_ID = """
<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
  <tr>
    <th></th><th>Action</th><th>Date</th><th>Record ID</th><th>Record Type</th>
    <th>Status</th><th>Address</th><th>Project Name</th><th>Description</th>
  </tr>
  <tr>
    <td></td><td></td><td>08/27/2026</td>
    <td><a href="/COHP/Cap/CapDetail.aspx?id=26TMP-006254">26TMP-006254</a></td>
    <td>Residential Construction Permit</td>
    <td>Application Submitted</td>
    <td>1321 PENNY RD, High Point NC 27260</td>
    <td>1321 Penny rd Front Porch</td>
    <td>Redo front porch, enclose the carport</td>
  </tr>
</table>
"""


def test_record_id_header_maps_to_permit_number():
    adapter, permits = _parse(GRID_WITH_RECORD_ID)
    assert len(permits) == 1
    assert permits[0].permit_number == "26TMP-006254"
    assert permits[0].status == "in_review"   # "Application Submitted" -> canonical


def test_reordered_columns_still_parse():
    """A jurisdiction that swaps column order must not corrupt the output."""
    swapped = GRID.replace(
        "<th>Date Opened</th><th>Record Number</th>",
        "<th>Record Number</th><th>Date Opened</th>",
    ).replace(
        "<td>03/14/2026</td>\n    <td><a href=\"/CitizenAccess/Cap/CapDetail.aspx?id=BLD26-0042\">BLD26-0042</a></td>",
        "<td><a href=\"/CitizenAccess/Cap/CapDetail.aspx?id=BLD26-0042\">BLD26-0042</a></td>\n    <td>03/14/2026</td>",
    )
    _, permits = _parse(swapped)
    assert permits[0].permit_number == "BLD26-0042"
    assert permits[0].file_date == "2026-03-14"


# --- pagination: real markup captured from a live 2-page window ------------
#
# _next_page() used to look for a link matching __doPostBack(...Page$N...),
# a pattern that never appears anywhere in the real markup below -- the
# "Next >" control's postback target is an opaque generated id
# (...ctl13$ctl04) with no page number encoded in it at all. That regex never
# matched, so pagination silently stopped after page 1 on every window,
# which is why scrapes came back shaped like (windows x page_size).
#
# Captured live: PRCITY, 2025-11-01..2025-11-05, 18 records / page size 10.

_REAL_PAGER_HTML = """
<tr align="center" class="ACA_Table_Pages ACA_Table_Pages_FontSize" valign="bottom">
  <td colspan="9">
    <table align="Center" border="0" class="aca_pagination" role="presentation"><tr>
      <td class="ACA_Hide">
        <a href="javascript:__doPostBack('ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$lb4btnExport','')"></a>
      </td>
      <td class="aca_pagination_td aca_pagination_PrevNext">
        <span class="aca_simple_text font11px">&lt; Prev</span>
      </td>
      <td class="aca_pagination_td"><span class="SelectedPageButton font11px">1</span></td>
      <td class="aca_pagination_td">
        <a href="javascript:__doPostBack('ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl03','')">2</a>
      </td>
      <td class="aca_pagination_td aca_pagination_PrevNext">
        <a class="aca_simple_text font11px" href="javascript:__doPostBack('ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl04','')">Next &gt;</a>
      </td>
    </tr></table>
  </td>
</tr>
"""

_HEADER_ROW = (
    "<tr><th></th><th>Date</th><th>Record Number</th><th>Project Name</th>"
    "<th>Address</th><th>Status</th><th>Record Type</th><th>Action</th><th></th></tr>"
)


def _data_row(permit_number: str, date_str: str) -> str:
    return (
        f"<tr><td></td><td>{date_str}</td>"
        f'<td><a href="/PRCITY/Cap/CapDetail.aspx?id={permit_number}">{permit_number}</a></td>'
        f"<td></td><td>1 Main St, Testville CA 94549</td><td>Issued</td>"
        f"<td>Reroof</td><td></td><td></td></tr>"
    )


def _grid_page(toolbar_text: str, n_rows: int, start_n: int, with_pager: bool) -> str:
    rows = "".join(_data_row(f"P-{i:03d}", "11/03/2025") for i in range(start_n, start_n + n_rows))
    pager = _REAL_PAGER_HTML if with_pager else ""
    return f"""
    <table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
      <tr><td colspan="9">{toolbar_text} | Download results</td></tr>
      {_HEADER_ROW}
      {rows}
      {pager}
    </table>
    """


PAGE1_HTML = _grid_page("Showing 1-10 of 18", 10, 1, with_pager=True)
PAGE2_HTML = _grid_page("Showing 11-18 of 18", 8, 11, with_pager=False)


def test_grid_total_parses_showing_of_toolbar():
    adapter = AccelaAdapter(CONFIG)
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")
    table = adapter._find_grid(soup)
    assert adapter._grid_total(table) == (18, False)


# COHP (High Point) actual toolbar text, captured live: once a window has
# more than 100 records Accela stops counting and renders "Showing 1-10 of
# 100+" -- "100" there is a floor, not the real total. Treating it as exact
# raised a false PaginationMismatch after correctly collecting all 140 rows
# of a window. is_capped lets scrape() relax the check to >= instead of ==.
CAPPED_TOOLBAR_HTML = """
<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
  <tr><td colspan="9">Showing 1-10 of 100+ | Download results</td></tr>
  {header}
</table>
""".format(header=_HEADER_ROW)


def test_grid_total_detects_capped_plus_total():
    adapter = AccelaAdapter(CONFIG)
    soup = BeautifulSoup(CAPPED_TOOLBAR_HTML, "html.parser")
    table = adapter._find_grid(soup)
    assert adapter._grid_total(table) == (100, True)


def test_scrape_does_not_raise_when_real_count_exceeds_capped_total(monkeypatch):
    # Real total (110) exceeds the capped "100+" toolbar figure -- exactly
    # the live COHP shape (toolbar said 100, 140 rows actually collected).
    # A strict == check would raise PaginationMismatch even though
    # pagination completed correctly and nothing was under-collected.
    adapter = AccelaAdapter(CONFIG)
    page1 = _grid_page("Showing 1-10 of 100+", 10, 1, with_pager=True)
    page2 = _grid_page("Showing 11-110 of 100+", 100, 11, with_pager=False)
    pages = [BeautifulSoup(page1, "html.parser"), BeautifulSoup(page2, "html.parser")]

    monkeypatch.setattr(adapter, "_warm", lambda: pages[0])
    monkeypatch.setattr(adapter, "_submit_search", lambda soup, start, end: pages[0])
    calls = iter(pages[1:])
    monkeypatch.setattr(adapter, "_next_page", lambda soup: next(calls, None))

    from datetime import date
    permits = list(adapter.scrape(date(2025, 11, 1), date(2025, 11, 5)))
    assert len(permits) == 110


def test_scrape_still_raises_on_undercollection_when_total_is_capped(monkeypatch):
    # Capping the total to ">=" must not turn off the loud-failure guard
    # entirely -- collecting fewer than the capped floor (100) is still a
    # genuine under-collection and must still raise.
    adapter = AccelaAdapter(CONFIG)
    page1 = BeautifulSoup(
        _grid_page("Showing 1-10 of 100+", 10, 1, with_pager=False), "html.parser"
    )

    monkeypatch.setattr(adapter, "_warm", lambda: page1)
    monkeypatch.setattr(adapter, "_submit_search", lambda soup, start, end: page1)
    monkeypatch.setattr(adapter, "_next_page", lambda soup: None)

    from datetime import date
    with pytest.raises(PaginationMismatch):
        list(adapter.scrape(date(2025, 11, 1), date(2025, 11, 5)))


def test_next_page_follows_the_real_next_link_not_a_page_number_guess():
    adapter = AccelaAdapter(CONFIG)
    soup = BeautifulSoup(PAGE1_HTML, "html.parser")

    captured = {}

    class FakeResponse:
        text = PAGE2_HTML

    def fake_fetch(method, url, **kwargs):
        captured["data"] = kwargs["data"]
        return FakeResponse()

    adapter.fetch = fake_fetch
    result = adapter._next_page(soup)

    assert result is not None
    assert captured["data"]["__EVENTTARGET"] == (
        "ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl04"
    )
    assert captured["data"]["__EVENTARGUMENT"] == ""
    table2 = adapter._find_grid(result)
    assert adapter._grid_total(table2) == (18, False)


def test_next_page_returns_none_on_last_page():
    adapter = AccelaAdapter(CONFIG)
    soup = BeautifulSoup(PAGE2_HTML, "html.parser")  # no pager: this is the last page
    assert adapter._next_page(soup) is None


def test_scrape_collects_all_pages_and_matches_toolbar_total(monkeypatch):
    adapter = AccelaAdapter(CONFIG)
    pages = [BeautifulSoup(PAGE1_HTML, "html.parser"), BeautifulSoup(PAGE2_HTML, "html.parser")]

    monkeypatch.setattr(adapter, "_warm", lambda: pages[0])
    monkeypatch.setattr(adapter, "_submit_search", lambda soup, start, end: pages[0])
    calls = iter(pages[1:])
    monkeypatch.setattr(adapter, "_next_page", lambda soup: next(calls, None))

    from datetime import date
    permits = list(adapter.scrape(date(2025, 11, 1), date(2025, 11, 5)))
    assert len(permits) == 18


def test_scrape_raises_loudly_on_undercollection(monkeypatch):
    """If pagination silently stops early, this must fail loudly, not ship
    a truncated scrape that looks complete."""
    adapter = AccelaAdapter(CONFIG)
    page1 = BeautifulSoup(PAGE1_HTML, "html.parser")  # toolbar says 18, only page 1 (10) delivered

    monkeypatch.setattr(adapter, "_warm", lambda: page1)
    monkeypatch.setattr(adapter, "_submit_search", lambda soup, start, end: page1)
    monkeypatch.setattr(adapter, "_next_page", lambda soup: None)  # pretend no next page exists

    from datetime import date
    with pytest.raises(PaginationMismatch):
        list(adapter.scrape(date(2025, 11, 1), date(2025, 11, 5)))
