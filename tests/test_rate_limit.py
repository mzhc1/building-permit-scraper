"""
rate_limit.wait() must be shared by host, not by object.

The bug this replaces: BaseAdapter used to own a RateLimiter instance whose
"last request" clock started at zero on construction. That worked fine
inside one long-running scrape, but every one-off diagnostic script in this
project is a fresh `python -c "..."` process that builds its own adapter --
so a handful of such scripts run back-to-back each saw themselves as the
very first request and never waited, even though together they hammered the
same host with no gap. That combination tripped a real HTTP 503 from COHP.

These tests simulate "two different callers" the way that actually
happened: two independent calls into rate_limit.wait() for the same host,
with no shared Python object between them (only the state file on disk),
exactly as two separate processes would see it.
"""

import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.adapters import rate_limit  # noqa: E402


def _fresh_host(prefix: str) -> str:
    # A unique per-test host name so tests don't share state through the
    # same on-disk files and can run in any order / in parallel.
    return f"{prefix}-{uuid.uuid4().hex}.test"


def test_second_call_for_same_host_waits_even_with_no_shared_object():
    host = _fresh_host("same-host")
    url = f"https://{host}/a"

    start = time.monotonic()
    rate_limit.wait(url, min_delay=0.2, jitter=0.0)
    first_done = time.monotonic()
    rate_limit.wait(url, min_delay=0.2, jitter=0.0)  # a second, independent call
    second_done = time.monotonic()

    assert first_done - start < 0.2  # first request never waits
    assert second_done - first_done >= 0.18  # second one honors the first's clock


def test_different_hosts_do_not_block_each_other():
    host_a = _fresh_host("host-a")
    host_b = _fresh_host("host-b")

    rate_limit.wait(f"https://{host_a}/x", min_delay=0.3, jitter=0.0)

    start = time.monotonic()
    rate_limit.wait(f"https://{host_b}/y", min_delay=0.3, jitter=0.0)
    elapsed = time.monotonic() - start

    assert elapsed < 0.1  # host_a's recent request must not throttle host_b


def test_zero_delay_config_does_not_sleep():
    host = _fresh_host("zero-delay")
    url = f"https://{host}/z"
    start = time.monotonic()
    rate_limit.wait(url, min_delay=0, jitter=0)
    rate_limit.wait(url, min_delay=0, jitter=0)
    assert time.monotonic() - start < 0.1


def test_wait_is_a_noop_for_a_urlless_target():
    # Defensive: a malformed/relative URL must not crash the caller.
    rate_limit.wait("not-a-url", min_delay=0, jitter=0)


# --- failure path: a process dies while holding the lock -------------------
#
# What must NOT happen: the next caller blocking forever with no way out.
# What must happen instead: a bounded wait, then one of two outcomes --
# self-healing recovery once the lock is old enough to be presumed
# abandoned (the crashed-holder case), or a loud TimeoutError if the lock is
# still fresh and genuinely held past the timeout (so a real stuck holder
# surfaces as an exception, not a silent stall).

def _create_lock_with_age(lock_path: Path, age_seconds: float) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    backdated = time.time() - age_seconds
    os.utime(lock_path, (backdated, backdated))  # simulate a lock left by a dead process


def test_stale_lock_from_a_dead_process_is_reclaimed_not_blocked_on_forever():
    host = _fresh_host("stale-lock")
    url = f"https://{host}/a"
    _, lock_path = rate_limit._paths(host)
    _create_lock_with_age(lock_path, age_seconds=999)  # long-dead holder

    start = time.monotonic()
    # Small stale_after so the test doesn't wait out the real ~60s default;
    # the mechanism under test is "does staleness get reclaimed", not the
    # specific production threshold.
    rate_limit.wait(url, min_delay=0, jitter=0, stale_after=0.05, lock_timeout=5)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # reclaimed almost immediately, not stuck for the full timeout
    assert not lock_path.exists()  # released cleanly after use


def test_fresh_lock_that_never_releases_raises_timeout_not_a_silent_hang():
    host = _fresh_host("held-lock")
    url = f"https://{host}/b"
    _, lock_path = rate_limit._paths(host)
    _create_lock_with_age(lock_path, age_seconds=0)  # a live, currently-held lock

    try:
        with pytest.raises(TimeoutError):
            # stale_after longer than lock_timeout: the lock never gets old
            # enough to be reclaimed before the timeout gives up.
            rate_limit.wait(url, min_delay=0, jitter=0, lock_timeout=0.3, stale_after=10)
    finally:
        lock_path.unlink(missing_ok=True)  # this test's own "dead holder" cleanup


def test_lock_timeout_exceeds_stale_threshold_with_margin():
    # Regression for the actual bug found in review: _acquire()'s original
    # default timeout (30s) was SHORTER than the staleness threshold (60s),
    # so any caller starting near when the lock was taken would hit
    # TimeoutError at 30s -- before the lock ever aged past 60s and the
    # reclaim branch could fire. A crashed holder produced a hard failure
    # for up to a minute instead of a bounded wait-then-recover. The
    # invariant that must hold going forward: timeout > stale_after, with
    # enough margin that a caller starting at age=0 can still watch the
    # lock cross the staleness line before its own timeout expires.
    assert rate_limit._LOCK_TIMEOUT_SECONDS > rate_limit._STALE_LOCK_SECONDS


def test_adapter_fetch_routes_through_the_shared_limiter(monkeypatch):
    # fetch() is the ONE place every code path -- scrape(), smoke_test(),
    # and any ad-hoc script -- goes through to hit the network. Confirms
    # that path always calls the shared, host-keyed limiter rather than
    # some adapter-local mechanism a caller could route around.
    from src.adapters.accela import AccelaAdapter

    calls = []
    monkeypatch.setattr(
        rate_limit, "wait",
        lambda url, min_delay, jitter: calls.append((url, min_delay, jitter)),
    )

    config = {
        "jurisdiction": "Testville", "state": "CA", "city": "Testville",
        "base_url": "https://example.invalid/CitizenAccess",
        "min_delay": 2.0, "jitter": 1.0,
    }
    adapter = AccelaAdapter(config)

    class _FakeResponse:
        status_code = 200
        content = b""

        def raise_for_status(self):
            pass

    monkeypatch.setattr(adapter.session, "request", lambda method, url, **kw: _FakeResponse())
    adapter.fetch("GET", "https://example.invalid/CitizenAccess/x")

    assert calls == [("https://example.invalid/CitizenAccess/x", 2.0, 1.0)]
