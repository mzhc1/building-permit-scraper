"""
Normalization into the Shovels permit shape.

Field names follow the public Shovels data dictionary. The subset here is the
one Shovels itself calls the important fields: file_date, permit_number,
owner_name, residential, jurisdiction, type, subtype, status, description,
and the address fields.

Design note: normalization NEVER guesses. A field the source did not provide
stays None. Coverage is then reported honestly by validate_batch() instead of
being papered over — a scraper that silently invents data is worse than one
that returns fewer fields.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict, field, fields
from datetime import date, datetime, timezone
from typing import Any, Iterable

# Shovels' own status vocabulary, per their "status 101" definitions.
CANONICAL_STATUSES = {
    "in_review",
    "active",
    "final",
    "expired",
    "withdrawn",
    "unknown",
}

_STATUS_MAP = {
    # in review
    "in review": "in_review", "applied": "in_review", "submitted": "in_review",
    "pending": "in_review", "under review": "in_review", "plan check": "in_review",
    "plan review": "in_review", "intake": "in_review", "received": "in_review",
    "application submitted": "in_review", "awaiting applicant response": "in_review",
    # active
    "issued": "active", "active": "active", "approved": "active",
    "in progress": "active", "permit issued": "active", "open": "active",
    # final
    "final": "final", "finaled": "final", "completed": "final", "complete": "final",
    "closed": "final", "co issued": "final", "certificate of occupancy": "final",
    # expired
    "expired": "expired", "void": "expired", "revoked": "expired", "cancelled": "expired",
    "canceled": "expired", "abandoned": "expired",
    # withdrawn
    "withdrawn": "withdrawn", "denied": "withdrawn", "rejected": "withdrawn",
}

_RESIDENTIAL_HINTS = re.compile(
    r"\b(single family|sfr|duplex|triplex|townhouse|townhome|condo|apartment|"
    r"residential|dwelling|adu|accessory dwelling)\b",
    re.I,
)
_COMMERCIAL_HINTS = re.compile(
    r"\b(commercial|retail|office|warehouse|industrial|restaurant|hotel|"
    r"tenant improvement|multi[- ]?family|mixed[- ]?use)\b",
    re.I,
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y",
    "%Y/%m/%d", "%m-%d-%Y", "%Y-%m-%dT%H:%M:%S",
)

_STREET_RE = re.compile(r"^\s*(?P<no>[0-9]+[A-Za-z]?(?:-[0-9]+)?)\s+(?P<street>.+?)\s*$")

# Accela's Address cell is comma-separated with city/state/zip (and
# sometimes a trailing "United States" when the portal has no zip on file)
# as the LAST segment, e.g. "1310 WHITE CLOVER LN, PASO ROBLES CA 93446" or
# "7990 SUNDANCE, United States". State must be a real 2-letter uppercase
# code so a bare "United States" (no city/state before it) never matches.
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>[A-Za-z .'-]+?)\s+(?P<state>[A-Z]{2})"
    r"(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?"
    r"(?:\s+United States)?$"
)


@dataclass
class Permit:
    # identity
    permit_number: str | None = None
    jurisdiction: str | None = None
    state: str | None = None
    # core
    file_date: str | None = None          # ISO 8601, YYYY-MM-DD
    issue_date: str | None = None
    final_date: str | None = None
    status: str | None = None
    type: str | None = None
    subtype: str | None = None
    description: str | None = None
    job_value: float | None = None
    residential: bool | None = None
    # parties
    owner_name: str | None = None
    contractor_name: str | None = None
    contractor_license: str | None = None
    # address
    street_no: str | None = None
    street: str | None = None
    city: str | None = None
    zipcode: str | None = None
    # provenance — not a Shovels field, but any serious pipeline needs it
    source_url: str | None = None
    scraped_at: str | None = None
    record_id: str | None = None

    def finalize(self) -> "Permit":
        """Stable, content-derived id so re-runs dedupe instead of duplicating."""
        basis = f"{self.state}|{self.jurisdiction}|{self.permit_number}".lower()
        self.record_id = hashlib.sha1(basis.encode()).hexdigest()[:16]
        if not self.scraped_at:
            self.scraped_at = (
                datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_date(raw: Any) -> str | None:
    if raw in (None, "", "-"):
        return None
    if isinstance(raw, (date, datetime)):
        return raw.strftime("%Y-%m-%d")
    text = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_money(raw: Any) -> float | None:
    if raw in (None, "", "-"):
        return None
    text = re.sub(r"[^0-9.\-]", "", str(raw))
    if text in ("", "-", "."):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def normalize_status(raw: Any) -> str:
    if raw in (None, ""):
        return "unknown"
    key = re.sub(r"\s+", " ", str(raw).strip().lower())
    return _STATUS_MAP.get(key, "unknown")


def infer_residential(*texts: Any) -> bool | None:
    """Return None when the text does not say. Never a coin flip."""
    blob = " ".join(str(t) for t in texts if t)
    if not blob.strip():
        return None
    commercial = bool(_COMMERCIAL_HINTS.search(blob))
    residential = bool(_RESIDENTIAL_HINTS.search(blob))
    if residential and not commercial:
        return True
    if commercial and not residential:
        return False
    return None


def split_address(raw: Any) -> tuple[str | None, str | None]:
    """'1042 Oak St' -> ('1042', 'Oak St'). Unparseable -> (None, whole string)."""
    if raw in (None, ""):
        return None, None
    text = re.sub(r"\s+", " ", str(raw).strip())
    match = _STREET_RE.match(text)
    if not match:
        return None, text or None
    return match.group("no"), match.group("street")


def parse_address_parts(
    raw: Any,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """
    Split a combined Accela address cell into (street_no, street, city,
    state, zip). Only splits off city/state/zip when the last comma
    segment clearly matches "CITY ST [ZIP]" (optionally trailing
    "United States") -- an unparseable tail (e.g. a bare "United States"
    with no city/state on file) leaves city/state/zip as None rather than
    guessing, and the whole text is kept as the street fallback.
    """
    if raw in (None, ""):
        return None, None, None, None, None
    text = re.sub(r"\s+", " ", str(raw).strip())
    parts = [p.strip() for p in text.split(",") if p.strip()]

    if len(parts) >= 2:
        match = _CITY_STATE_ZIP_RE.match(parts[-1])
        if match:
            street_no, street = split_address(", ".join(parts[:-1]))
            return (
                street_no, street,
                match.group("city"),
                match.group("state"),
                normalize_zip(match.group("zip")),
            )

    street_no, street = split_address(text)
    return street_no, street, None, None, None


def normalize_zip(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    if len(digits) >= 5:
        return digits[:5]
    return None


# --- validation gate -------------------------------------------------------

CRITICAL_FIELDS = ("permit_number", "jurisdiction", "state", "file_date")
IMPORTANT_FIELDS = (
    "status", "type", "description", "street", "city", "zipcode",
    "owner_name", "contractor_name", "job_value", "residential",
)


@dataclass
class BatchReport:
    total: int = 0
    unique: int = 0
    duplicates_dropped: int = 0
    rejected: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    fill_rate: dict[str, float] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            f"records scraped     : {self.total}",
            f"unique after dedupe : {self.unique}",
            f"duplicates dropped  : {self.duplicates_dropped}",
            f"rejected            : {self.rejected}",
        ]
        if self.reject_reasons:
            for reason, count in sorted(self.reject_reasons.items(), key=lambda x: -x[1]):
                lines.append(f"    - {reason}: {count}")
        lines.append("field fill rate:")
        for name, rate in self.fill_rate.items():
            bar = "#" * int(rate * 20)
            lines.append(f"    {name:<20} {rate*100:5.1f}%  {bar}")
        return "\n".join(lines)


def validate_batch(
    permits: Iterable[Permit], reject_log: list[Permit] | None = None
) -> tuple[list[Permit], BatchReport]:
    """Drop unusable records, dedupe, and measure honestly.

    reject_log: if given, rejected Permit objects (whatever fields they DO
    have, including permit_number/source_url) are appended to it rather than
    just being counted. Debug aid for telling "field genuinely absent on the
    portal" apart from "parser is reading the wrong column".
    """
    report = BatchReport()
    seen: set[str] = set()
    kept: list[Permit] = []

    for permit in permits:
        report.total += 1
        missing = [f for f in CRITICAL_FIELDS if not getattr(permit, f)]
        if missing:
            report.rejected += 1
            reason = "missing " + ",".join(missing)
            report.reject_reasons[reason] = report.reject_reasons.get(reason, 0) + 1
            if reject_log is not None:
                reject_log.append(permit)
            continue
        permit.finalize()
        if permit.record_id in seen:
            report.duplicates_dropped += 1
            continue
        seen.add(permit.record_id)
        kept.append(permit)

    report.unique = len(kept)
    if kept:
        for name in CRITICAL_FIELDS + IMPORTANT_FIELDS:
            filled = sum(1 for p in kept if getattr(p, name) not in (None, ""))
            report.fill_rate[name] = filled / len(kept)
    return kept, report


def field_names() -> list[str]:
    return [f.name for f in fields(Permit)]
