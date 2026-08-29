"""
smoke_test() must catch a 200 that isn't actually the page it looks like.

Caught live on COHP (High Point): the default search_path's module param
("Building") isn't valid for that agency (it's "ConstPermit"), and Accela
served its own in-page error screen with a plain HTTP 200 -- a status-code-
only smoke test reported REACHABLE on a completely broken page. This is the
same failure class ("looks fine at a glance, corrupts silently") the whole
adapter is built to guard against elsewhere (HEADER_MAP, PaginationMismatch);
smoke_test needed the same discipline applied to itself.

ERROR_PAGE_HTML below is the real error banner captured from that response,
trimmed to the markers _smoke_check() keys off.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.accela import AccelaAdapter  # noqa: E402

CONFIG = {
    "jurisdiction": "High Point",
    "state": "NC",
    "city": "High Point",
    "base_url": "https://aca-prod.accela.com/COHP",
    "min_delay": 0,
    "jitter": 0,
}

# Captured live: GET .../Cap/CapHome.aspx?module=Building&TabName=Building
# against COHP returned HTTP 200 with this body instead of the search form.
ERROR_PAGE_HTML = """
<html><body>
<main>
<div class="ACA_Message_Error ACA_Message_Error_FontSize" id="ctl00_PlaceHolderMain_systemErrorMessage_messageBar">
  <span id="ctl00_PlaceHolderMain_systemErrorMessage_lblMessageTitle">An error has occurred.</span>
  <span class="ACA_Body_Text ACA_Body_Text_FontSize" id="ctl00_PlaceHolderMain_systemErrorMessage_lblMessage">Invalid Module param value</span>
</div>
</main>
</body></html>
"""

SEARCH_FORM_HTML = """
<html><body>
<form id="aspnetForm">
  <input type="text" name="ctl00$PlaceHolderMain$generalSearchForm$txtGSStartDate"/>
  <table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList"></table>
</form>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")


def test_smoke_check_flags_accela_error_page_despite_200():
    adapter = AccelaAdapter(CONFIG)
    error = adapter._smoke_check(_FakeResponse(ERROR_PAGE_HTML))
    assert error is not None
    assert "Invalid Module param value" in error


def test_smoke_check_passes_a_real_search_page():
    adapter = AccelaAdapter(CONFIG)
    assert adapter._smoke_check(_FakeResponse(SEARCH_FORM_HTML)) is None


def test_smoke_url_is_the_search_page_not_bare_base_url():
    # smoke_test() must actually exercise search_path -- that's the only
    # page a bad module/TabName param breaks. Hitting bare base_url (the
    # old behavior) would 200 regardless of whether search_path is wrong.
    adapter = AccelaAdapter(CONFIG)
    assert adapter._smoke_url() == adapter._search_url()
    assert adapter._smoke_url() != adapter.base_url


def test_smoke_test_fails_on_accela_error_page(monkeypatch):
    adapter = AccelaAdapter(CONFIG)
    monkeypatch.setattr(adapter, "fetch", lambda method, url, **kw: _FakeResponse(ERROR_PAGE_HTML))
    ok, detail = adapter.smoke_test()
    assert ok is False
    assert "Invalid Module param value" in detail


def test_smoke_test_passes_on_a_real_search_page(monkeypatch):
    adapter = AccelaAdapter(CONFIG)
    monkeypatch.setattr(adapter, "fetch", lambda method, url, **kw: _FakeResponse(SEARCH_FORM_HTML))
    ok, detail = adapter.smoke_test()
    assert ok is True
    assert detail.startswith("HTTP 200")
