"""
Find jurisdictions Shovels does not cover — using Shovels' own public API.

Correct two-step flow (v2):
  1. resolve a place name to a geo_id via /cities/search?q= or
     /jurisdictions/search?q=
  2. count permits for that geo_id via /permits/search, which requires
     geo_id + permit_from + permit_to

Lesson learned the hard way: NEVER call raise_for_status() and throw the body
away. A 422 from FastAPI names the exact parameter that is wrong. Swallowing it
turns a five-second fix into an evening of guessing. Every error here carries
its body.

Needs a free API key from app.shovels.ai -> set SHOVELS_API_KEY=...
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any

import requests

BASE = "https://api.shovels.ai/v2"
TIMEOUT = 30


class ApiError(Exception):
    """Carries the response body, which is where the actual answer lives."""

    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body[:900]
        super().__init__(f"HTTP {status} - {self.body}")


class ShovelsClient:
    def __init__(self, api_key: str | None = None, pause: float = 0.3, debug: bool = False):
        self.api_key = api_key or os.environ.get("SHOVELS_API_KEY", "")
        if not self.api_key:
            raise SystemExit(
                "No API key. Get a free one at app.shovels.ai, then:\n"
                "  Windows  : set SHOVELS_API_KEY=your_key\n"
                "  mac/linux: export SHOVELS_API_KEY=your_key"
            )
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        })
        self.pause = pause
        self.debug = debug
        self.calls = 0

    def get(self, path: str, **params) -> dict[str, Any]:
        url = f"{BASE}{path}"
        response = self.session.get(url, params=params, timeout=TIMEOUT)
        self.calls += 1
        time.sleep(self.pause)

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "10"))
            print(f"    rate limited, sleeping {wait}s")
            time.sleep(wait)
            return self.get(path, **params)

        if response.status_code >= 400:
            raise ApiError(response.status_code, response.url, response.text)

        payload = response.json()
        if self.debug:
            print("    RAW:", json.dumps(payload)[:400])
        return payload


# --- step 1: name -> geo_id ------------------------------------------------

def resolve_geo_id(client: ShovelsClient, city: str, state: str):
    """
    Try cities first, then jurisdictions. Returns (geo_id, matched_name, note).
    Filters candidates by state so 'Cumberland' doesn't resolve to the wrong one.
    """
    notes = []
    for endpoint in ("/cities/search", "/jurisdictions/search"):
        try:
            payload = client.get(endpoint, q=city)
        except ApiError as exc:
            notes.append(f"{endpoint} -> {exc}")
            continue

        items = payload.get("items") or []
        if not items:
            notes.append(f"{endpoint} -> 0 items")
            continue

        for item in items:
            item_state = (item.get("state") or item.get("state_abbr") or "").upper()
            geo_id = item.get("geo_id") or item.get("id")
            if not geo_id:
                continue
            if item_state == state.upper() or str(geo_id).upper().startswith(state.upper()):
                return geo_id, item.get("name"), f"matched via {endpoint}"

        first = items[0]
        geo_id = first.get("geo_id") or first.get("id")
        if geo_id:
            return geo_id, first.get("name"), (
                f"matched via {endpoint} WITHOUT state confirmation - verify by hand"
            )

    return None, None, "; ".join(notes) or "no geo_id found"


# --- step 2: geo_id -> permit count ---------------------------------------

def count_permits(client: ShovelsClient, geo_id: str, days: int = 365, cap: int = 500):
    """
    /permits/search has no working total field ("total_count" is always null)
    and /jurisdictions/{id}/metrics 404s on this plan. The only honest count
    comes from walking next_cursor ourselves.

    We only need to place the result in 0 / <cap / >=cap, so we page in the
    max allowed size (100) and stop as soon as we've confirmed >=cap items,
    rather than exhausting a potentially huge jurisdiction. That keeps the
    call count bounded (<= cap/100 + 1) while never fabricating a number.
    """
    end = date.today()
    start = end - timedelta(days=days)
    page_size = 100
    total = 0
    cursor = None
    pages = 0

    while True:
        params = dict(
            geo_id=geo_id,
            permit_from=start.isoformat(),
            permit_to=end.isoformat(),
            size=page_size,
        )
        if cursor:
            params["cursor"] = cursor
        try:
            payload = client.get("/permits/search", **params)
        except ApiError as exc:
            return -1, (
                f"permit count unknown: paginated {total} items over {pages} page(s) "
                f"then failed: {exc}"
            )

        pages += 1
        total += len(payload.get("items") or [])
        cursor = payload.get("next_cursor")

        if not cursor:
            return total, f"exact count; {pages} page(s) over last {days}d"
        if total >= cap:
            return total, (
                f">= {total} (stopped paging at cap; more pages exist) "
                f"over last {days}d"
            )


@dataclass
class Coverage:
    state: str
    city: str
    geo_id: str | None
    matched_name: str | None
    permit_count: int
    verdict: str
    evidence: str


def probe_city(client: ShovelsClient, city: str, state: str) -> Coverage:
    notes = []
    geo_id, matched_name, note = resolve_geo_id(client, city, state)
    notes.append(note)

    if not geo_id:
        return Coverage(state, city, None, None, -1, "error", "; ".join(notes))

    permit_count, count_note = count_permits(client, geo_id)
    notes.append(count_note)

    if permit_count < 0:
        verdict = "error"
    elif permit_count == 0:
        verdict = "gap"
    elif permit_count < 500:
        verdict = "thin"
    else:
        verdict = "covered"

    return Coverage(state, city, geo_id, matched_name, permit_count, verdict, "; ".join(notes))


def scan(candidates, api_key=None, debug=False):
    client = ShovelsClient(api_key, debug=debug)
    results = []
    for city, state in candidates:
        print(f"probing {city}, {state} ...", flush=True)
        coverage = probe_city(client, city, state)
        label = coverage.matched_name or "?"
        print(f"  -> {coverage.verdict} ({coverage.permit_count} permits) [{label}]")
        if coverage.verdict == "error":
            print(f"     {coverage.evidence[:220]}")
        results.append(coverage)
    print(f"\n{client.calls} API calls used")
    return results


def save(results, path: str = "out/coverage_probe.json") -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([asdict(r) for r in results], handle, indent=2, ensure_ascii=False)
    print(f"wrote {path}\n")

    errors = [r for r in results if r.verdict == "error"]
    gaps = [r for r in results if r.verdict in ("gap", "thin")]

    if errors:
        print(f"{len(errors)} could not be resolved (NOT the same as a gap):")
        for err in errors:
            print(f"  - {err.city}, {err.state}: {err.evidence[:150]}")
        print()

    if gaps:
        print(f"{len(gaps)} real gaps found:")
        for gap in sorted(gaps, key=lambda g: g.permit_count):
            print(f"  - {gap.city}, {gap.state}: {gap.verdict} "
                  f"({gap.permit_count} permits, geo_id={gap.geo_id})")
    else:
        print("no gaps among the candidates - widen the list in config.yaml")