# shovels-gap

Scrapes building-permit records out of a US local-government permit portal
that has no public API, normalizes them into one consistent schema, and
verifies the output against the portal's own stated totals rather than
trusting that pagination and parsing quietly worked.

Built against Accela Citizen Access, the most widely deployed permit-portal
platform in US local government. The unit of work is the **platform, not
the city** — one adapter handles every Accela jurisdiction, and a new one
is a config change, not a rewrite (see "The leverage argument" below).

---

## Status — read this first

| Part | State |
|---|---|
| Schema + normalization | **Written and tested.** |
| Accela grid parser | **Written and tested**, including regressions pinned to real captured markup. |
| Coverage gap probe | **Run against the live Shovels API.** Susanville, CA confirmed as a real gap (0 permits). |
| Live scrape against a real portal | **Run against Paso Robles, CA (Accela/PRCITY).** 1,479 permits over the trailing 12 months. |

```
$ python -m pytest tests/ -q
48 passed
```

The target: **Paso Robles, CA**, Accela Citizen Access, `PRCITY` agency,
1,479 permits scraped over the trailing 12 months. A 50-row sample of the
actual scraped output is committed at
[`sample_paso_robles.csv`](sample_paso_robles.csv), so you can see real
output without running anything.

---

## Setup

macOS/Linux:

```bash
cd shovels-gap
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m pytest tests/ -q          # should be 48 passed
```

Windows (PowerShell or cmd.exe):

```bat
cd shovels-gap
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
python -m pytest tests/ -q          # should be 48 passed
```

---

## Why Paso Robles, not Susanville

`probe` found a real gap first: **Susanville, CA** — 0 permits in Shovels,
confirmed via the live API (see `out/coverage_probe.json`). It looked like
the obvious target. It isn't scrapeable: Susanville has **no online permit
portal at all**. Records are only available through a public-records
request filed via NextRequest — there's no grid, no search form, nothing to
point an adapter at. Lassen County (the surrounding county, where Susanville
is the seat) does have an online building-permit portal, but it only covers
a subset of permit types (electrical/mechanical/plumbing/roofing
applications) and isn't the city's own record set.

Paso Robles was picked instead precisely because it's on Accela — a real,
scrapeable target with an existing, verifiable coverage gap (see Origin,
near the end, for the numbers).

**The wider lesson:** a coverage gap in a small jurisdiction usually means
no scrapeable source exists at all, not that nobody's built the scraper yet
— that's very often *why* it's a gap. So the search can't run gap-first
("find the biggest gap, then go build for it"); it has to run
**portal-first**: find jurisdictions gapped *and* confirmed to be on a
known platform (Accela, EnerGov, OpenGov, CityView, eTRAKiT), and only then
rank by gap size. `probe` alone can't tell you which — it just tells you
the gap exists. Checking for a live portal is a separate, necessary step
before committing to a target.

---

## The three steps, in order

Each one is cheap and answers a question before the next one costs you an evening.

### 1. Prove the gap — `probe`

Get a free API key at `app.shovels.ai`, then:

```bash
export SHOVELS_API_KEY=your_key
python -m src.run probe --config config.yaml
```

This asks **Shovels' own API** whether each candidate city has a jurisdiction
record and how many permits sit behind it. Output lands in
`out/coverage_probe.json`.

Three verdicts:
- **gap** — zero permits. Best target.
- **thin** — under 500 permits. Also good: they already want it and have almost nothing.
- **covered** — skip it.

The candidate list in the config is a set of starting guesses, not verified
gaps. The probe is what turns a guess into evidence. Keep the JSON — it's the
strongest single thing you can attach to an email.

`/permits/search` has no working total field (`total_count` is always
`null` on this API, and there's no `/jurisdictions/{id}/metrics` endpoint on
the free plan), so `count_permits()` gets a real number by paginating itself
— capped at 500 items, since a gap/thin/covered verdict only needs to know
which side of 500 a jurisdiction falls on. It returns `-1` with a note,
never a guess, when the count genuinely can't be determined (rate limits,
credit exhaustion, an API error mid-page).

### 2. Check the portal is shaped right — `smoke`

Find the winner's permit portal, put its URL in `config.yaml`, then:

```bash
python -m src.run smoke --config config.yaml
```

One request. Tells you whether the host is reachable and responding, before
you build anything around it — and, on Accela, whether the page it loaded
is actually the search form rather than a 200-status error screen. A wrong
`module`/`TabName` param on a new jurisdiction (confirmed on COHP/High
Point) still returns HTTP 200; `smoke_test()` parses the body for Accela's
own in-page error banner rather than trusting the status code alone.

### 3. Pull the data — `scrape`

```bash
python -m src.run scrape --config config.yaml --days 365
```

Writes `out/<jurisdiction>_permits.csv`, `.json`, and a `_report.txt` with the
honest fill rate per field.

**Last real run — Paso Robles, CA, trailing 12 months:**

```
records scraped     : 1479
unique after dedupe : 1479
duplicates dropped  : 0
rejected            : 0
field fill rate:
    permit_number        100.0%
    jurisdiction          100.0%
    state                 100.0%
    file_date              100.0%
    status                100.0%
    type                   99.9%
    description            91.5%
    street                 96.8%
    city                   100.0%
    zipcode                64.4%
    owner_name              0.0%
    contractor_name         0.0%
    job_value                0.0%
    residential            44.8%
```

Every collected window's row count was cross-checked against the grid's own
`"Showing X of Y"` total before being accepted (see Bugs below) — this isn't
a best-effort scrape, it's a verified one.

Honest caveats, not gaps in the parser:
- **`zipcode` is 64.4%, not 100%**, because roughly a third of this
  jurisdiction's own address records have no state/zip on file — the portal
  itself just appends "United States" in place of them. The parser only
  fills a value it can clearly read; it never guesses one in.
- **`owner_name`, `contractor_name`, `job_value` are 0%** because Paso
  Robles' Building grid simply doesn't expose those columns — there's
  nothing to map them from, not a mapping bug.
- **The column labelled `Date` may not be the original filing date.** Some
  windows show a cluster of otherwise-unrelated permits (different numbers,
  addresses, types) sharing one identical date, mostly `Withdrawn` or
  `Finaled` records — consistent with it being a last-status/last-action
  date rather than strictly the filing date. It's read correctly from the
  column the portal labels `Date`; what that label means semantically on
  this jurisdiction's grid is a separate, open question worth flagging to
  whoever consumes this data.
- **The Building module returns Fee Estimates and Addenda alongside real
  permits** — record numbers prefixed `EST-` and `ADD-` (24 and 174 of the
  1,479, respectively) are Accela's own record types living in the same
  grid, not permits. They pass through with `type` reflecting that ("Fee
  Estimate", "Addendum"), same as everything else — worth filtering out
  downstream if the consumer wants permits only.

---

## Bugs found running this for real, and how they were caught

Every one of these was invisible from a single small test run — the code
looked done, the tests were green, and the output looked plausible. They
only surfaced by scraping a full year and treating every number in the
output as something to verify, not trust.

**Pagination silently stopped after page 1** (525 → 1,479 records once
fixed). `_next_page()` looked for a link matching
`__doPostBack(...Page$N...)` — a pattern that never appears anywhere in
Accela's real markup. The actual "Next >" control's postback target is an
opaque generated id with no page number in it at all, so that regex never
matched, pagination never advanced, and every window silently returned
only its first 10 rows. It went unnoticed because the record count still
*looked* plausible (`windows × 10` isn't an obviously wrong shape at a
glance) — it only became visible by comparing the collected count for a
window against the grid's own `"Showing 1-10 of 18"` toolbar text and
finding they didn't match. That check is now permanent: `scrape()` raises
`PaginationMismatch` — loudly, not a printed warning — if a window's
collected row count ever disagrees with the portal's own stated total, so
silent under-collection can't happen again without the run failing.

**The date column looked corrupted; it wasn't.** A large scrape showed the
same `file_date` repeated 8–10 times in a row before jumping exactly 7 days
(matching `slice_days`) — enough to suspect every record was getting
stamped with its search window's start date. Direct verification against
two separate live windows showed the repeated date matched neither the
window's start nor its end, and the individual per-row values matched the
raw grid cells exactly, permit-by-permit. Real, if administratively
clustered, data — not a parsing bug. Documented above rather than "fixed",
since there was nothing to fix.

**Address parsing left city/state/zip jammed into `street`.**
`split_address()` only ever split the leading house number off; the rest
of the string — including city, state, and zip — stayed in `street`
verbatim, and `zipcode` was pulled from a static config value (always
empty) rather than the scraped text. `zipcode` went from 0% to a real
64.4% fill after `parse_address_parts()` was added; it only splits out
city/state/zip when the text unambiguously contains them and leaves the
rest as `street` fallback otherwise — never a guess.

**Rejected records turned out to be the pagination footer, not real
permits.** A run reporting several "missing file_date" rejections traced
to the grid's own `< Prev 1 2 3 Next >` footer row being parsed as if it
were a data row — its cells shifted into the real columns and `< Prev`
landed wherever the header mapped to `permit_number`. Now explicitly
skipped by its `ACA_Table_Pages` class / nested `aca_pagination` table.

**`source_url` briefly duplicated the agency segment**
(`.../PRCITY/PRCITY/Cap/...`) after an earlier URL-join fix: grid hrefs are
already agency-qualified absolute paths, not paths relative to `base_url`
(which itself already ends in `/PRCITY`). Fixed by resolving hrefs against
the actual current page URL via plain `urljoin`, which handles an
absolute-path href correctly by construction — same as a browser would.

Every one of the above has a regression test pinned to the real markup
that exposed it, in `tests/test_accela_parse.py` and
`tests/test_schema.py`.

---

## What's actually hard here, and why it's the point

Accela Citizen Access is ASP.NET WebForms, so:

- every request must echo back `__VIEWSTATE`, `__VIEWSTATEGENERATOR` and
  `__EVENTVALIDATION` from the previous response
- a cold POST is rejected as a forged cross-site request unless it also
  carries `Referer`/`Origin` headers matching the search page
- pagination is a `__doPostBack`, not a URL — page N+1 only exists relative to
  page N, so you can't parallelize without N sessions, and the postback
  target is an opaque id with no page number encoded in it (see Bugs above)
- result sets are capped at 10/page and paginated hard, so a year-long query
  silently truncates if you don't actually walk every page. The adapter
  walks the window in 7-day slices and every page within each slice.

And the design choice that matters most: **columns are matched by header text,
not index.** Jurisdictions reorder and rename their grids constantly
("Record Number" vs "Permit Number", "Date Opened" vs "File Date"). Index-based
parsing doesn't crash when they do — it silently writes the address into the
status column. There's a test for exactly that case
(`test_reordered_columns_still_parse`). The header row itself isn't `tr[0]`
either — Accela prepends a "Showing X of Y" toolbar row first; the real
header is located by which row actually contains `<th>` cells.

## The leverage argument

There are ~20,000 US permitting authorities but only a handful of portal
platforms — Accela, Tyler EnerGov, OpenGov/ViewPoint, CityView, eTRAKiT — plus a
long tail of bespoke pages and PDFs.

So the unit of work is the **platform**, not the city. `adapters/base.py` defines
the contract; `adapters/accela.py` is the first implementation. Adding EnerGov
next is a new file, not a new project, and every Accela jurisdiction after the
first is a config change.

That claim was tested, not just asserted: **High Point, NC (COHP)** was
added as a second Accela target purely by writing `config_highpoint.yaml`
and running the same three steps. It took a couple of small, real fixes
along the way — COHP names its permits module `ConstPermit` rather than
`Building`, headers its record-number column `Record ID` rather than
`Record Number`, and (at higher volume than Paso Robles) its own grid caps
its displayed total at `"100+"` instead of an exact count, which needed an
adapter-level fix rather than a config one. None of it was a rewrite.

## The normalizer never guesses

`infer_residential()` returns `None` when the text doesn't say, rather than
picking a side. `parse_money("N/A")` returns `None`, not `0.0`.
`parse_address_parts()` leaves city/state/zip as `None` rather than mis-split
them when the source text is ambiguous. A missing field stays missing and
shows up honestly in the fill-rate report — see the caveats above for what
that looks like against a real, messy government data source.

This is the whole ballgame for data work. A pipeline that quietly invents values
is worse than one that returns fewer fields, because nobody downstream can tell
the difference until it's expensive. `validate_batch()` is the gate: it drops
records missing critical fields, dedupes on a content-derived `record_id` so
re-runs are idempotent, and reports what fraction of each field actually got
filled. Pass `reject_log=[]` to `validate_batch()` to capture what got dropped
and why — `src/run.py` writes it to `out/rejected.csv` whenever anything is
rejected, which is how the pagination-footer bug above was caught.

## Origin

This started as a proof-of-work piece for a Shovels Senior Data & Platform
Engineer opening, whose first listed responsibility is the spider fleet
pulling permits out of thousands of government systems — the same
scrape-normalize-verify problem this project solves end to end. It also
doubles as a concrete demonstration of the gap it was built to find:
Shovels' own API returns 6 permits for Paso Robles, CA; the live portal has
1,479.

## Layout

```
src/schema.py            Permit shape, normalizers, validation gate
src/gapfinder.py         Coverage probe against the Shovels API
src/adapters/base.py     Adapter contract; fetch() + smoke_test()
src/adapters/rate_limit.py  Cross-process, host-keyed rate limiting
src/adapters/accela.py   Accela Citizen Access implementation
src/run.py               CLI: smoke | probe | scrape
tests/                   48 offline tests, no network
```

## Manners

Default 1.5s + jitter between requests, single-threaded, identifying
User-Agent. **Put a real contact address in `user_agent` before running** —
`config.yaml` is gitignored precisely so that address doesn't end up
committed; `config.example.yaml` carries a placeholder instead.
Building permits are public records, but these are small government servers
and vendor platforms, and we're guests on them. Check the target's
`robots.txt` and terms before a full run.

**The scraper was polite; the tooling around it wasn't.** Rate limiting
originally lived on a per-adapter `RateLimiter` instance, whose "last
request" clock started at zero on construction. Inside one long `scrape()`
run that's fine. But investigating a new jurisdiction (as COHP/High Point
was) means firing off a series of one-off diagnostic scripts alongside the
scraper — dump a page, check a header row, confirm a config fix — and each
of those is a fresh Python process that builds its own adapter. Each one
individually respected `min_delay`; none of them knew about the others. Run
several back-to-back in a short window and the target sees a burst of
requests with no gap at all, even though every single caller thought it was
being polite. That's exactly what tripped a real HTTP 503 from COHP mid-
investigation — not the scraper misbehaving, but debugging traffic hitting
the same host outside the limiter's view.

Rate limiting now lives in [`src/adapters/rate_limit.py`](src/adapters/rate_limit.py):
a state file per host in the OS temp directory, read and updated under a
lock immediately before every request, shared by every process on the
machine rather than owned by any one adapter object. `BaseAdapter.fetch()`
is the sole place any code path — `scrape()`, `smoke_test()`, or a throwaway
diagnostic script that just imports `AccelaAdapter` and calls `.fetch()` —
reaches the network, so throttling isn't something a caller has to remember
to opt into. See `tests/test_rate_limit.py` for the regression coverage.

Two things worth knowing about the lock itself, rather than leaving them
unremarked:

- **A process that dies mid-request does not wedge every future run.** The
  lock file is timestamped; if it's older than 60s (a process that crashed
  or was killed while holding it — a live request never takes that long),
  the next caller reclaims it automatically instead of waiting on it
  forever. A caller that shows up while the lock is still fresh and
  genuinely held waits up to ~70s and then fails loudly with `TimeoutError`
  rather than hanging silently. See `test_stale_lock_from_a_dead_process_is_reclaimed_not_blocked_on_forever`
  and `test_fresh_lock_that_never_releases_raises_timeout_not_a_silent_hang`.
- **The lock/state files live in the OS temp directory**, which on a shared
  multi-user machine is typically world-writable — another local user could
  read, tamper with, or delete them. Left as-is on purpose: this is a
  politeness mechanism for being a good guest on a public-records server,
  not a security boundary, and the threat model here doesn't include a
  hostile local user. Noted so it isn't silently assumed safe if this code
  ends up reused somewhere that assumption doesn't hold.
