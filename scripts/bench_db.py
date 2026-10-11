"""Measure database round-trip latency, so a region can be chosen by data.

Point it at any Postgres and compare. Latency, not bandwidth, is what a chat
gateway lives or dies on: a request issues ~8 statements, so every 100ms of
round trip is ~0.8s added to the first token.

    .venv/bin/python scripts/bench_db.py "postgresql://user:pass@host/db"

Requires only `psycopg`, so it works against a database the app is not yet
configured to use. Prints median and p90, because a regional distance shows up
in the median while connection-pool churn and compute suspend show up in the
tail.
"""

from __future__ import annotations

import statistics
import sys
import time


def measure(label: str, url: str, samples: int = 30) -> None:
    import psycopg

    try:
        t0 = time.time()
        conn = psycopg.connect(url, connect_timeout=20)
        connect_ms = (time.time() - t0) * 1000
    except Exception as exc:  # noqa: BLE001 — report and move on
        print(f"{label:<28} FAILED: {type(exc).__name__}: {str(exc)[:90]}")
        return

    try:
        conn.execute("select 1")  # warm the connection
        timings = []
        for _ in range(samples):
            start = time.time()
            conn.execute("select 1")
            timings.append((time.time() - start) * 1000)
        timings.sort()
        print(
            f"{label:<28} connect {connect_ms:7.0f}ms   "
            f"median {statistics.median(timings):6.1f}ms   "
            f"p90 {timings[int(len(timings) * 0.9)]:6.1f}ms   "
            f"max {timings[-1]:6.1f}ms"
        )
    finally:
        conn.close()


def main() -> int:
    urls = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not urls:
        print(__doc__)
        return 2
    labels = [f"dsn {i + 1}" for i in range(len(urls))]
    for label, url in zip(labels, urls):
        measure(label, url)
    print()
    print("Median = network distance.  p90 = pooler/compute churn.")
    print("A chat request issues ~8 statements, so median x 8 is the")
    print("database time added before the model is called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())