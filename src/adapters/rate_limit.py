"""
Cross-process, host-keyed rate limiting.

Manners can't live on an adapter instance -- an in-process object only
throttles requests made by the process that created it. Every ad-hoc
diagnostic script in this project (dump a page, check a header row, poke a
config change) starts a fresh Python process and constructs its own
adapter. A per-instance limiter's "last request" clock starts at zero every
time, so several such scripts run back-to-back each see themselves as the
very first request and never wait -- even though, from the target server's
point of view, the same host was hit repeatedly with no gap at all. That is
exactly what tripped a real HTTP 503 from COHP mid-investigation: the
scraper itself was well-behaved, but the diagnostics run alongside it were
invisible to its limiter.

The fix has to sit below any adapter instance, keyed by host rather than by
object: a small state file per host, in the OS temp directory (so it is
shared by every process on this machine, not just this one), read and
updated under a lock immediately before every request. wait() is the only
entry point, and BaseAdapter.fetch() is the only thing that calls it -- so
anything that goes through fetch(), including a one-off script that just
imports AccelaAdapter and calls .fetch(), is throttled automatically. It is
not a substitute for a real distributed lock (a genuinely concurrent flood
from many machines would still get through), but for the actual failure
mode here -- one person, one machine, several sequential processes hitting
the same jurisdiction -- it closes the gap completely.
"""

from __future__ import annotations

import os
import random
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

# NOTE on where this lives: tempfile.gettempdir() is typically
# world-writable on a shared multi-user machine (e.g. /tmp with the sticky
# bit, or a shared TEMP on Windows). Any other local user could read,
# tamper with, or delete these lock/state files. Left unfixed deliberately
# -- this is a politeness mechanism for being a good guest on a public
# permits server, not a security boundary, and the realistic threat model
# for a solo scraper on a personal or CI machine doesn't include a hostile
# local user. Naming it so it isn't silently assumed safe if this ever runs
# somewhere that assumption doesn't hold.
_STATE_DIR = Path(tempfile.gettempdir()) / "building-permit-scraper-rate-limits"

# If a lock file is older than this, its owner almost certainly crashed
# (or was killed) while holding it. Break the lock instead of wedging every
# future run against this host forever.
_STALE_LOCK_SECONDS = 60.0

# Must exceed _STALE_LOCK_SECONDS with margin. _acquire() only reclaims a
# stale lock from INSIDE its own wait loop -- if this timeout were shorter
# than the staleness threshold (it used to be: 30s timeout vs. a 60s
# threshold), every caller that started around the time the lock was taken
# would hit TimeoutError at the 30s mark and bail before the lock ever aged
# past 60s, so a crashed holder produced a hard failure for up to a minute
# instead of a bounded wait-then-recover. With the 10s margin below, a
# caller that starts the instant the lock is (re)created still has enough
# runway to watch it cross the staleness threshold and reclaim it itself.
_LOCK_TIMEOUT_SECONDS = _STALE_LOCK_SECONDS + 10.0


def _paths(host: str) -> tuple[Path, Path]:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = host.replace(":", "_").replace("/", "_")
    return _STATE_DIR / f"{safe}.last", _STATE_DIR / f"{safe}.lock"


def _acquire(
    lock_path: Path,
    timeout: float = _LOCK_TIMEOUT_SECONDS,
    stale_after: float = _STALE_LOCK_SECONDS,
) -> None:
    """
    Failure path when a process dies while holding the lock: the next
    caller does NOT block forever. It polls every 50ms; once the lock
    file's age exceeds `stale_after` it is unlinked and re-acquired
    automatically (self-healing, no operator action needed). If the lock
    is genuinely still held (a live, well-behaved holder) past `timeout`,
    acquisition gives up loudly with TimeoutError rather than hanging --
    that should only happen if something is very wrong (e.g. a holder
    stuck mid-request for over a minute), and a raised exception surfaces
    that instead of a silent stall.
    """
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue  # released between the open() failing and stat()
            if age > stale_after:
                lock_path.unlink(missing_ok=True)
                continue
            if time.time() > deadline:
                raise TimeoutError(f"rate-limit lock held too long: {lock_path}")
            time.sleep(0.05)


def _release(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def wait(
    url: str,
    min_delay: float,
    jitter: float,
    *,
    lock_timeout: float = _LOCK_TIMEOUT_SECONDS,
    stale_after: float = _STALE_LOCK_SECONDS,
) -> None:
    """
    Block the calling process until it is polite to request `url`'s host
    again, honoring the last request made to that host by ANY process --
    not just this one. Safe to call with min_delay=jitter=0 (tests do): the
    lock round-trip still happens but nothing sleeps.

    lock_timeout/stale_after exist as overrides so tests can exercise the
    crashed-holder recovery path (see test_rate_limit.py) without waiting
    out the real ~70s default; production callers should never need them.
    """
    host = urlparse(url).netloc.lower()
    if not host:
        return

    last_path, lock_path = _paths(host)
    _acquire(lock_path, timeout=lock_timeout, stale_after=stale_after)
    try:
        try:
            last = float(last_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            last = 0.0
        delay = min_delay + random.uniform(0, jitter)
        elapsed = time.time() - last
        if elapsed < delay:
            time.sleep(delay - elapsed)
        last_path.write_text(repr(time.time()))
    finally:
        _release(lock_path)
