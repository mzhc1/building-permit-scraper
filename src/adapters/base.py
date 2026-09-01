"""
Adapter contract.

The leverage argument: US permit portals are not 20,000 unique systems. They
are a handful of vendor platforms (Accela Citizen Access, Tyler EnerGov,
OpenGov/ViewPoint, CityView, eTRAKiT) wearing 20,000 different skins, plus a
long tail of bespoke pages and PDFs.

So the unit of work is the PLATFORM, not the city. One adapter that handles
Accela unlocks every Accela jurisdiction with a config change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterator

import requests

from ..schema import Permit
from . import rate_limit

USER_AGENT = (
    "building-permit-scraper/0.1 (public records research; contact: SET_YOUR_EMAIL)"
)


class BaseAdapter(ABC):
    platform: str = "unknown"

    def __init__(self, config: dict):
        self.config = config
        self.jurisdiction = config["jurisdiction"]
        self.state = config["state"]
        self.base_url = config["base_url"].rstrip("/")
        self.min_delay = config.get("min_delay", 1.5)
        self.jitter = config.get("jitter", 0.8)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.get("user_agent", USER_AGENT),
            "Accept-Language": "en-US,en;q=0.9",
        })

    def fetch(self, method: str, url: str, **kwargs) -> requests.Response:
        # rate_limit.wait() is keyed by host and shared across every
        # process on this machine (see rate_limit.py) -- deliberately not
        # a plain per-instance limiter, so any diagnostic script that
        # constructs an adapter and calls fetch() is throttled too, not
        # just the scraper's own scrape() loop.
        rate_limit.wait(url, self.min_delay, self.jitter)
        kwargs.setdefault("timeout", 45)
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    @abstractmethod
    def scrape(self, start: date, end: date) -> Iterator[Permit]:
        """Yield Permit objects for the date window. Raw values are fine —
        normalization happens in schema.py, not here."""
        raise NotImplementedError

    def _smoke_url(self) -> str:
        """
        Page smoke_test() should actually load. Defaults to base_url;
        adapters whose scrape() depends on a specific sub-page (e.g. a
        module-qualified search page) should override this so smoke checks
        the page that matters, not just the portal root.
        """
        return self.base_url

    def _smoke_check(self, response: requests.Response) -> str | None:
        """
        Inspect a 200 response's body for a platform-specific "this looks
        reachable but is actually wrong" signature. Return an error message
        to fail smoke_test, or None if the body looks fine. Base
        implementation does no body inspection (a 200 is a 200).
        """
        return None

    def smoke_test(self) -> tuple[bool, str]:
        """
        Cheap check before burning a full run. HTTP 200 alone is not
        enough: some platforms (confirmed on Accela) serve their own
        in-page error screen with a 200 status, so a request that "worked"
        can still be pointed at a completely broken page. Caught live on
        COHP (High Point): a wrong module param produced a 200 "Invalid
        Module param value" error page that a status-code-only check
        reported as REACHABLE.
        """
        try:
            response = self.fetch("GET", self._smoke_url())
        except Exception as exc:  # noqa: BLE001 - want the message, whatever it is
            return False, f"{type(exc).__name__}: {exc}"
        error = self._smoke_check(response)
        if error:
            return False, error
        return True, f"HTTP {response.status_code}, {len(response.content)} bytes"
