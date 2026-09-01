"""
CLI.

    python -m src.run smoke   --config config.yaml
    python -m src.run probe   --config config.yaml
    python -m src.run scrape  --config config.yaml --days 30

Order matters. smoke costs one request and tells you whether the target is
even shaped the way the adapter expects. probe proves the coverage gap using
Shovels' own API. Only then is scrape worth your evening.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

from .schema import validate_batch, field_names
from .adapters.accela import AccelaAdapter

ADAPTERS = {
    "accela": AccelaAdapter,
}

OUT = Path("out")


def load_config(path: str) -> dict:
    with open(path) as handle:
        return yaml.safe_load(handle)


def build_adapter(config: dict):
    platform = config.get("platform", "accela")
    if platform not in ADAPTERS:
        sys.exit(f"Unknown platform '{platform}'. Known: {', '.join(ADAPTERS)}")
    return ADAPTERS[platform](config)


def cmd_smoke(args) -> int:
    config = load_config(args.config)
    adapter = build_adapter(config)
    print(f"target: {config['jurisdiction']}, {config['state']}")
    print(f"url   : {config['base_url']}")
    ok, detail = adapter.smoke_test()
    print(f"result: {'REACHABLE' if ok else 'FAILED'} — {detail}")
    if not ok:
        print("\nIf this failed: the portal may block non-browser agents, or the")
        print("URL may be wrong. Open it in a browser and copy the exact path of")
        print("the permit search page into base_url/search_path.")
    return 0 if ok else 1


def cmd_probe(args) -> int:
    from .gapfinder import scan, save
    config = load_config(args.config)
    candidates = [(c["city"], c["state"]) for c in config.get("probe_candidates", [])]
    if not candidates:
        candidates = [(config.get("city") or config["jurisdiction"], config["state"])]
    OUT.mkdir(exist_ok=True)
    results = scan(candidates)
    save(results, str(OUT / "coverage_probe.json"))
    return 0


def cmd_scrape(args) -> int:
    config = load_config(args.config)
    adapter = build_adapter(config)
    end = date.today()
    start = end - timedelta(days=args.days)
    print(f"scraping {config['jurisdiction']}, {config['state']}: {start} .. {end}")

    permits = list(adapter.scrape(start, end))
    rejected: list = []
    kept, report = validate_batch(permits, reject_log=rejected)

    OUT.mkdir(exist_ok=True)
    stem = config["jurisdiction"].lower().replace(" ", "_")

    # Explicit utf-8 everywhere: real permit descriptions carry non-ASCII
    # punctuation (e.g. "10' × 12'"), and open()'s platform default
    # encoding is NOT utf-8 on Windows -- it's the system codepage (cp1251
    # here), which raised UnicodeEncodeError partway through a real scrape.
    if rejected:
        rejected_path = OUT / "rejected.csv"
        with open(rejected_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=field_names())
            writer.writeheader()
            for permit in rejected:
                writer.writerow(permit.to_dict())
        print(f"wrote {rejected_path} ({len(rejected)} rejected records)")

    csv_path = OUT / f"{stem}_permits.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=field_names())
        writer.writeheader()
        for permit in kept:
            writer.writerow(permit.to_dict())

    json_path = OUT / f"{stem}_permits.json"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump([p.to_dict() for p in kept], handle, indent=2, ensure_ascii=False)

    report_path = OUT / f"{stem}_report.txt"
    body = (
        f"jurisdiction : {config['jurisdiction']}, {config['state']}\n"
        f"window       : {start} .. {end}\n"
        f"platform     : {adapter.platform}\n\n"
        + report.render()
        + "\n"
    )
    report_path.write_text(body, encoding="utf-8")

    print("\n" + report.render())
    print(f"\nwrote {csv_path}\nwrote {json_path}\nwrote {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="building-permit-scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("smoke", cmd_smoke), ("probe", cmd_probe)):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="config.yaml")
        sp.set_defaults(func=handler)

    sp = sub.add_parser("scrape")
    sp.add_argument("--config", default="config.yaml")
    sp.add_argument("--days", type=int, default=30)
    sp.set_defaults(func=cmd_scrape)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
