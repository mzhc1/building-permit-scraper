"""
Accela Citizen Access adapter.

Accela is the most widely deployed permit portal in US local government, so
this is the adapter with the best coverage-per-hour ratio.

The hard parts, which are the actual reason this work is worth paying for:
  * ASP.NET WebForms: every request must echo back __VIEWSTATE,
    __VIEWSTATEGENERATOR and __EVENTVALIDATION from the previous page.
  * Session must be warmed by hitting the search page first; posting cold
    returns a redirect to an error page.
  * Pagination is a postback (__doPostBack) rather than a URL, so page N+1 is
    only reachable from page N. No parallelism without N sessions.
  * Row layouts differ per jurisdiction, so columns are matched by HEADER TEXT
    rather than index — index-based parsing silently corrupts on any
    jurisdiction that reordered its grid.

STATUS: written against the documented Accela CA markup, NOT yet run against a
live jurisdiction (no outbound access to .gov hosts from the build box). Run
`python -m src.run smoke --config config.yaml` first — that tells you in one
request whether the selectors below match your target before you spend an
evening on it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..schema import (
    Permit, parse_date, parse_money, normalize_status,
    infer_residential, parse_address_parts, normalize_zip,
)
from .base import BaseAdapter

SEARCH_PATH = "/Cap/CapHome.aspx?module=Building&TabName=Building"

# Header text -> our field. Matched case-insensitively on substring, because
# jurisdictions rename these constantly ("Record Number" vs "Permit Number").
HEADER_MAP = {
    "record number": "permit_number",
    "permit number": "permit_number",
    "record id": "permit_number",
    "date": "file_date",
    "opened": "file_date",
    "file date": "file_date",
    "record type": "type",
    "permit type": "type",
    "type": "type",
    "status": "status",
    "description": "description",
    "project name": "description",
    "address": "address_raw",
    "location": "address_raw",
    "job value": "job_value",
    "valuation": "job_value",
    "owner": "owner_name",
}

_HIDDEN = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__VIEWSTATEENCRYPTED")


class PaginationMismatch(RuntimeError):
    """
    The grid's own "Showing X of Y" total disagrees with how many rows we
    actually collected for a window. Raised loudly instead of silently
    shipping an incomplete scrape -- this is exactly how the
    Page$N-that-never-matched bug produced (windows x 10)-shaped record
    counts for months without anyone noticing.
    """


class AccelaAdapter(BaseAdapter):
    platform = "accela_citizen_access"

    def _hidden_fields(self, soup: BeautifulSoup) -> dict[str, str]:
        payload = {}
        for name in _HIDDEN:
            tag = soup.find("input", {"name": name})
            if tag and tag.get("value") is not None:
                payload[name] = tag["value"]
        return payload

    def _join(self, path: str) -> str:
        # urljoin("https://host/PRCITY", "/Cap/x") drops "/PRCITY" because a
        # leading "/" makes it absolute-path. base_url has no agency segment
        # to lose only if we anchor it as a directory (trailing slash) and
        # treat path as relative.
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _search_url(self) -> str:
        return self._join(self.config.get("search_path", SEARCH_PATH))

    def _smoke_url(self) -> str:
        # smoke_test() must load the actual search page scrape() depends
        # on, not just base_url -- base_url alone can 200 even when
        # search_path's module/TabName params are wrong.
        return self._search_url()

    def _smoke_check(self, response) -> str | None:
        """
        Accela renders its own in-page error screen with an HTTP 200, so a
        status-code-only smoke test reports REACHABLE on a page that is
        actually broken. Confirmed live on COHP (High Point): the default
        search_path's module=Building isn't a real module for that agency
        (it uses "ConstPermit"), and the "warm" GET came back 200 with this
        exact error banner instead of the search form. Markers below
        (element ids + "An error has occurred.") are copied verbatim from
        that captured page.
        """
        soup = BeautifulSoup(response.text, "html.parser")
        message = soup.find(id=re.compile(r"systemErrorMessage_lblMessage$"))
        if message and message.get_text(strip=True):
            return f"Accela error page: {message.get_text(strip=True)}"
        return None

    def _resolve_href(self, href: str) -> str:
        # hrefs scraped from the grid (e.g. a CapDetail.aspx link) are
        # already agency-qualified absolute paths like "/PRCITY/Cap/...",
        # NOT paths relative to base_url. Resolving them with _join() (which
        # anchors base_url, itself ending in "/PRCITY", as a directory)
        # duplicated the agency segment: ".../PRCITY/PRCITY/Cap/...".
        # urljoin against the actual current page URL is the correct
        # resolution -- for an absolute-path href it replaces the whole
        # path against just scheme+host, exactly like a browser would.
        # Confirmed against a live grid: href was
        # "/PRCITY/Cap/CapDetail.aspx?...&agencyCode=PRCITY...".
        return urljoin(self._search_url(), href)

    def _post_headers(self) -> dict[str, str]:
        # Accela rejects any POST missing Referer/Origin as a forged
        # cross-site request ("Potential cross-site request forgery
        # attacks. The Referer and Origin headers are missing"), confirmed
        # against the live error page.
        origin = urlparse(self.base_url)
        return {
            "Referer": self._search_url(),
            "Origin": f"{origin.scheme}://{origin.netloc}",
        }

    def _warm(self) -> BeautifulSoup:
        """Accela rejects cold POSTs. Load the search page to get a session."""
        response = self.fetch("GET", self._search_url())
        return BeautifulSoup(response.text, "html.parser")

    def _submit_search(self, soup: BeautifulSoup, start: date, end: date) -> BeautifulSoup:
        prefix = self.config.get(
            "control_prefix",
            "ctl00$PlaceHolderMain$generalSearchForm",
        )
        payload = self._hidden_fields(soup)
        payload.update({
            # The search "button" is an <a> doing
            # WebForm_DoPostBackWithOptions(...btnNewSearch...) that lives
            # under ctl00$PlaceHolderMain directly, NOT under
            # generalSearchForm, and it isn't a submitted form value at all
            # — it's the postback target. Confirmed against the live DOM.
            "__EVENTTARGET": "ctl00$PlaceHolderMain$btnNewSearch",
            "__EVENTARGUMENT": "",
            f"{prefix}$txtGSStartDate": start.strftime("%m/%d/%Y"),
            f"{prefix}$txtGSEndDate": end.strftime("%m/%d/%Y"),
        })
        response = self.fetch("POST", self._search_url(), data=payload, headers=self._post_headers())
        return BeautifulSoup(response.text, "html.parser")

    def _find_grid(self, soup: BeautifulSoup):
        table = soup.find("table", id=re.compile(r"gdvPermitList|dgvPermitList", re.I))
        if table:
            return table

        # A genuine zero-result search shows this exact notice. Trust it —
        # don't let the fallback heuristic below match the search FORM's own
        # wrapper table instead (it has no id, but its label cells like
        # "Record Number:" and "Start Date:" spuriously satisfy HEADER_MAP,
        # producing fake rows with every value blank except the labels that
        # happened to line up). Confirmed against a live zero-result page.
        if "your search returned no results" in soup.get_text(" ", strip=True).lower():
            return None

        # Fallback: the widest table WITH AN ID (real Accela grids always
        # have one; the search-form wrapper never does) whose header row
        # mentions a record id.
        best, best_cols = None, 0
        for candidate in soup.find_all("table", id=True):
            header = candidate.find("tr")
            if not header:
                continue
            text = header.get_text(" ", strip=True).lower()
            if "record" in text or "permit" in text:
                cols = len(header.find_all(["th", "td"]))
                if cols > best_cols:
                    best, best_cols = candidate, cols
        return best

    def _header_row(self, table):
        # The grid's real header is NOT the first <tr>: Accela prepends a
        # "Showing 1-10 of 26 | Download results" toolbar row, and that
        # toolbar wraps a nested table whose own <tr>s get flattened into
        # table.find_all("tr") too, duplicating it. The true header row is
        # the first one made of <th> cells. Confirmed against a live grid.
        for row in table.find_all("tr"):
            if row.find("th"):
                return row
        return table.find("tr")

    def _column_index(self, table) -> dict[int, str]:
        header = self._header_row(table)
        mapping: dict[int, str] = {}
        if not header:
            return mapping
        for index, cell in enumerate(header.find_all(["th", "td"])):
            label = re.sub(r"\s+", " ", cell.get_text(strip=True)).lower()
            for needle, target in HEADER_MAP.items():
                if needle in label:
                    mapping.setdefault(index, target)
                    break
        return mapping

    def _rows_to_permits(self, table, columns: dict[int, str]) -> Iterator[Permit]:
        header = self._header_row(table)
        # Only true siblings of the header row, so the toolbar row's nested
        # (duplicate) <tr>s never get parsed as data.
        rows = header.find_next_siblings("tr") if header else table.find_all("tr")[1:]
        for row in rows:
            # The trailing "< Prev 1 2 3 Next >" pager is itself a <tr> in
            # this grid (single <td colspan="9"> wrapping a nested
            # aca_pagination table), so it survives the header-sibling
            # filter. Its cells shift into the real columns and land "< Prev"
            # in whatever column maps to permit_number with a blank date —
            # confirmed against a live grid, NOT a genuine missing file_date.
            row_classes = row.get("class") or []
            if "ACA_Table_Pages" in row_classes or row.find("table", class_="aca_pagination"):
                continue
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            raw: dict[str, str] = {}
            for index, cell in enumerate(cells):
                target = columns.get(index)
                if not target:
                    continue
                raw[target] = re.sub(r"\s+", " ", cell.get_text(strip=True))

            if not raw.get("permit_number"):
                continue

            street_no, street, parsed_city, parsed_state, parsed_zip = (
                parse_address_parts(raw.get("address_raw"))
            )
            description = raw.get("description")
            permit_type = raw.get("type")

            link = row.find("a", href=True)
            source_url = self._resolve_href(link["href"]) if link else self._search_url()

            yield Permit(
                permit_number=raw.get("permit_number"),
                jurisdiction=self.jurisdiction,
                state=self.state,
                file_date=parse_date(raw.get("file_date")),
                status=normalize_status(raw.get("status")),
                type=permit_type,
                description=description,
                job_value=parse_money(raw.get("job_value")),
                residential=infer_residential(permit_type, description),
                owner_name=raw.get("owner_name") or None,
                street_no=street_no,
                street=street,
                city=parsed_city or self.config.get("city"),
                zipcode=parsed_zip or normalize_zip(self.config.get("zipcode")),
                source_url=source_url,
            )

    def _grid_total(self, table) -> tuple[int, bool] | None:
        """
        The grid's own toolbar row reads "Showing 1-10 of 18 | Download
        results" -- an authoritative total for the window, independent of
        how many rows we actually parsed. Used to catch silent
        under-collection (see scrape()) instead of just trusting pagination
        to have worked.

        Returns (total, is_capped). On a high-volume jurisdiction (COHP/
        High Point, confirmed live) Accela stops counting past 100 and
        renders "Showing 1-10 of 100+" instead of the real total -- "100"
        there is a floor, not an exact count, and treating it as exact
        raised a false PaginationMismatch after correctly collecting 140
        rows. Low-volume jurisdictions (PRCITY/Paso Robles) never hit the
        cap and report an exact total, so is_capped lets scrape() keep the
        strict equality check there while relaxing it to >= for a capped
        window.
        """
        toolbar = table.find("tr")
        if not toolbar:
            return None
        text = toolbar.get_text(" ", strip=True)
        match = re.search(r"of\s+([\d,]+)(\+?)", text)
        if not match:
            return None
        return int(match.group(1).replace(",", "")), bool(match.group(2))

    def _next_page(self, soup: BeautifulSoup) -> BeautifulSoup | None:
        """
        Pagination is a postback, but NOT one whose __EVENTTARGET encodes
        the page number (there is no "...Page$N" anywhere in the real
        markup -- that was a guess that never matched, so pagination never
        advanced past page 1). The pager lives in a <tr class="ACA_Table_
        Pages"> wrapping a <table class="aca_pagination"> whose "Next >"
        control is an <a> (disabled Prev/Next render as inert <span>, so an
        <a> only exists when a next page actually exists). Confirmed
        against a live 2-page grid (18 records, page size 10).
        """
        table = self._find_grid(soup)
        if table is None:
            return None
        pager = table.find("table", class_="aca_pagination")
        if pager is None:
            return None
        next_link = None
        for a in pager.find_all("a", href=True):
            if "next" in a.get_text(strip=True).lower():
                next_link = a
                break
        if next_link is None:
            return None
        match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", next_link["href"])
        if not match:
            return None
        payload = self._hidden_fields(soup)
        payload.update({
            "__EVENTTARGET": match.group(1),
            "__EVENTARGUMENT": match.group(2),
        })
        response = self.fetch("POST", self._search_url(), data=payload, headers=self._post_headers())
        return BeautifulSoup(response.text, "html.parser")

    def scrape(self, start: date, end: date) -> Iterator[Permit]:
        # Accela caps result sets, so walk the window in slices rather than
        # asking for a year and silently getting the first 1000 rows.
        slice_days = int(self.config.get("slice_days", 7))
        max_pages = int(self.config.get("max_pages", 20))
        cursor = start

        while cursor <= end:
            window_end = min(cursor + timedelta(days=slice_days - 1), end)
            print(f"  window {cursor} .. {window_end}", flush=True)

            soup = self._warm()
            soup = self._submit_search(soup, cursor, window_end)

            page = 1
            window_total = None
            window_total_capped = False
            window_count = 0
            while page <= max_pages:
                table = self._find_grid(soup)
                if not table:
                    if page == 1:
                        print("    no result grid (likely zero results)")
                    break
                if window_total is None:
                    grid_total = self._grid_total(table)
                    if grid_total is not None:
                        window_total, window_total_capped = grid_total
                columns = self._column_index(table)
                if "permit_number" not in columns.values():
                    print("    WARNING: no permit-number column matched; "
                          "check HEADER_MAP against this jurisdiction")
                    break
                count = 0
                for permit in self._rows_to_permits(table, columns):
                    count += 1
                    window_count += 1
                    yield permit
                print(f"    page {page}: {count} rows")
                nxt = self._next_page(soup)
                if nxt is None:
                    break
                soup = nxt
                page += 1
            else:
                print(f"    WARNING: hit max_pages={max_pages} without running out of pages")

            if window_total is not None:
                undercollected = (
                    window_count < window_total if window_total_capped
                    else window_count != window_total
                )
                if undercollected:
                    suffix = "+" if window_total_capped else ""
                    raise PaginationMismatch(
                        f"{cursor}..{window_end}: grid says {window_total}{suffix} records "
                        f"but only collected {window_count} (max_pages={max_pages})"
                    )

            cursor = window_end + timedelta(days=1)
