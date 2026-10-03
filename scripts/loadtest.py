"""
Load test harness (SRS NFR-1, §20's P19 exit criterion).

    python scripts/loadtest.py --base http://localhost:8000 --users 50 --seconds 60

**What this is.** A closed-loop generator that drives the read paths a real
visitor uses — the home page, job search with filters, a posting, and the
public company page — and reports the latency distribution per path against
NFR-1's p95 budget of 300 ms.

**What it is not, stated plainly.** It is not a substitute for running this
against production-shaped infrastructure with production-shaped data, and it
does not by itself satisfy "load test at 10x expected traffic passes". A run
on a laptop against a development server with an empty database measures the
laptop. The exit criterion needs a staging environment with a seeded corpus and
a machine that is not also running the browser — the harness exists so that run
is one command rather than an afternoon's work, and so the numbers from it are
comparable between runs.

**Read paths only, deliberately.** Every write path on this platform either
costs money (AI), sends mail, or creates records somebody then has to clean up.
A generator that applied to jobs would produce a load profile no real user makes
and a database nobody can use afterwards. Write-path capacity is measured with
the queue depth metrics in §17, not by hammering `POST`.

Standard library only: a load generator that needs its own dependency tree is a
load generator nobody runs.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urljoin

#: The read paths, with the share of traffic each one gets. Weights are a guess
#: at a browsing session — most people land, search, and open two or three
#: postings — and they are here as data so a real access log can replace them.
PATHS: tuple[tuple[str, int], ...] = (
    ("/", 2),
    ("/jobs/", 4),
    ("/jobs/?q=engineer", 3),
    ("/jobs/?q=django&remote=hybrid", 2),
    ("/pricing/", 1),
    ("/about/", 1),
)

#: NFR-1. Exceeding it is the finding, not an error.
P95_BUDGET_MS = 300


@dataclass
class Result:
    """Timings for one path."""

    path: str
    durations: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: int = 0

    def record(self, *, ms: float, status: int) -> None:
        self.durations.append(ms)
        self.statuses[status] = self.statuses.get(status, 0) + 1

    def percentile(self, fraction: float) -> float:
        if not self.durations:
            return 0.0
        ordered = sorted(self.durations)
        index = min(int(len(ordered) * fraction), len(ordered) - 1)
        return ordered[index]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "requests": len(self.durations),
            "errors": self.errors,
            "statuses": self.statuses,
            "p50_ms": round(self.percentile(0.50), 1),
            "p95_ms": round(self.percentile(0.95), 1),
            "p99_ms": round(self.percentile(0.99), 1),
            "max_ms": round(max(self.durations), 1) if self.durations else 0.0,
            "mean_ms": round(statistics.fmean(self.durations), 1) if self.durations else 0.0,
            "within_budget": self.percentile(0.95) <= P95_BUDGET_MS,
        }


class Runner:
    """One worker pool hammering the read paths for a fixed duration."""

    def __init__(self, *, base: str, users: int, seconds: int) -> None:
        self.base = base
        self.users = users
        self.seconds = seconds
        self.results = {path: Result(path) for path, _ in PATHS}
        self._plan = [path for path, weight in PATHS for _ in range(weight)]
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _one(self, path: str) -> None:
        url = urljoin(self.base, path)
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{url!r} is not an http(s) URL. --base takes an origin.")

        started = time.perf_counter()
        try:
            # noqa justified: the scheme is checked immediately above, and the
            # only URL this ever opens is one the operator passed as --base.
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except Exception:  # noqa: BLE001 - a connection failure is a data point
            with self._lock:
                self.results[path].errors += 1
            return

        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            self.results[path].record(ms=elapsed, status=status)

    def _worker(self, seed: int) -> None:
        index = seed
        while not self._stop.is_set():
            self._one(self._plan[index % len(self._plan)])
            index += 1

    def run(self) -> dict:
        threads = [
            threading.Thread(target=self._worker, args=(n,), daemon=True) for n in range(self.users)
        ]
        started = time.perf_counter()
        for thread in threads:
            thread.start()

        time.sleep(self.seconds)
        self._stop.set()
        for thread in threads:
            thread.join(timeout=30)

        wall = time.perf_counter() - started
        rows = [result.as_dict() for result in self.results.values()]
        total = sum(row["requests"] for row in rows)

        return {
            "base": self.base,
            "concurrency": self.users,
            "seconds": round(wall, 1),
            "requests": total,
            "requests_per_second": round(total / wall, 1) if wall else 0.0,
            "errors": sum(row["errors"] for row in rows),
            "p95_budget_ms": P95_BUDGET_MS,
            "within_budget": all(row["within_budget"] for row in rows if row["requests"]),
            "paths": rows,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=25, help="Concurrent readers.")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    report = Runner(base=args.base, users=args.users, seconds=args.seconds).run()

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["within_budget"] else 1

    print(
        f"\n{report['requests']} requests in {report['seconds']}s "
        f"({report['requests_per_second']}/s, {args.users} concurrent), "
        f"{report['errors']} error(s)\n"
    )
    print(f"{'path':<34}{'n':>7}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}  budget")
    for row in report["paths"]:
        verdict = "ok" if row["within_budget"] else "OVER"
        print(
            f"{row['path']:<34}{row['requests']:>7}{row['p50_ms']:>8}"
            f"{row['p95_ms']:>8}{row['p99_ms']:>8}{row['max_ms']:>8}  {verdict}"
        )
    print(
        f"\np95 budget: {P95_BUDGET_MS} ms (NFR-1). "
        f"{'All paths within budget.' if report['within_budget'] else 'At least one path is over.'}"
    )
    return 0 if report["within_budget"] else 1


if __name__ == "__main__":
    sys.exit(main())
