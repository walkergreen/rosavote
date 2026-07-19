"""
Load-test harness for the referendum vote service.

Purpose: prove the vote path has headroom for your per-WAVE peak, and surface
Firestore contention if any remains. This is what tells you whether the
independent-writes refactor is sufficient on its own or whether you want to
add the Pub/Sub write-buffer.

IMPORTANT — run against a STAGING deployment with STAGING codes, never against
the live election. It casts real votes with the codes you give it.

What it does:
  - Loads a file of DISTINCT valid staging codes (one per line). Distinct codes
    test throughput/scaling. (Duplicate-code hammering tests atomicity — that's
    a different, separate test; see --dupe-check.)
  - Ramps concurrency in stages so you can see where latency degrades.
  - Reports per-stage: throughput, p50/p95/p99 latency, and a breakdown of
    HTTP statuses. 503/"try_again" counts are your contention signal.

Usage:
  python load_test.py \
      --url https://staging-xxxx.run.app/vote \
      --codes staging_codes.txt \
      --stages 25,50,100,200,400 \
      --per-stage 500

  # atomicity spot-check: fire N concurrent requests with ONE code, expect
  # exactly 1 "recorded" and N-1 "already_voted", zero double-records.
  python load_test.py --url .../vote --dupe-check CODE123 --dupe-n 50
"""

import argparse
import asyncio
import random
import time
from collections import Counter

import aiohttp


def load_codes(path):
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip()]


async def one_vote(session, url, code, choice, results):
    t0 = time.perf_counter()
    try:
        async with session.post(url, json={"code": code, "choice": choice}) as r:
            body = await r.json(content_type=None)
            dt = time.perf_counter() - t0
            status = body.get("status") or body.get("error") or f"http_{r.status}"
            results.append((dt, r.status, status))
    except Exception as e:
        dt = time.perf_counter() - t0
        results.append((dt, 0, f"exc_{type(e).__name__}"))


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1))))
    return sorted_vals[k]


async def run_stage(url, codes, concurrency, n_requests):
    """Fire n_requests across `concurrency` simultaneous workers, each using a
    distinct code drawn (without replacement) from `codes`."""
    results = []
    sem = asyncio.Semaphore(concurrency)
    picks = codes[:n_requests]

    connector = aiohttp.TCPConnector(limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async def guarded(code):
            async with sem:
                await one_vote(session, url, code, random.choice(["YES", "NO"]), results)

        t0 = time.perf_counter()
        await asyncio.gather(*(guarded(c) for c in picks))
        wall = time.perf_counter() - t0

    lats = sorted(r[0] for r in results)
    statuses = Counter(r[2] for r in results)
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "wall_s": round(wall, 2),
        "throughput_rps": round(len(results) / wall, 1) if wall else 0,
        "p50_ms": round(pct(lats, 50) * 1000, 1),
        "p95_ms": round(pct(lats, 95) * 1000, 1),
        "p99_ms": round(pct(lats, 99) * 1000, 1),
        "statuses": dict(statuses),
    }


async def dupe_check(url, code, n):
    """Atomicity test: N concurrent requests, SAME code. Expect exactly one
    'recorded'."""
    results = []
    connector = aiohttp.TCPConnector(limit=n)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*(
            one_vote(session, url, code, "EUGENE_DEBS", results) for _ in range(n)
        ))
    statuses = Counter(r[2] for r in results)
    recorded = statuses.get("recorded", 0)
    print(f"dupe-check: {n} concurrent requests, same code")
    print(f"  statuses: {dict(statuses)}")
    print(f"  recorded == 1 ? {'PASS' if recorded == 1 else 'FAIL (got %d)' % recorded}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="POST /vote endpoint (STAGING)")
    ap.add_argument("--codes", help="file of distinct valid staging codes")
    ap.add_argument("--stages", default="25,50,100,200",
                    help="comma-separated concurrency levels")
    ap.add_argument("--per-stage", type=int, default=500,
                    help="requests per stage")
    ap.add_argument("--dupe-check", help="single code to fire concurrently")
    ap.add_argument("--dupe-n", type=int, default=50)
    args = ap.parse_args()

    if args.dupe_check:
        asyncio.run(dupe_check(args.url, args.dupe_check, args.dupe_n))
        return

    codes = load_codes(args.codes)
    stages = [int(s) for s in args.stages.split(",")]
    needed = args.per_stage
    if len(codes) < needed:
        print(f"WARNING: only {len(codes)} codes; each stage reuses the pool "
              f"which will produce 'already_voted' after first use. Provide "
              f">= per-stage * n_stages distinct codes for a clean run.")

    print(f"target: {args.url}")
    print(f"stages (concurrency): {stages}, {args.per_stage} requests each\n")
    print(f"{'conc':>5} {'reqs':>6} {'wall_s':>7} {'rps':>7} "
          f"{'p50ms':>7} {'p95ms':>7} {'p99ms':>7}  statuses")
    print("-" * 78)

    offset = 0
    for c in stages:
        pool = codes[offset:offset + args.per_stage]
        offset += args.per_stage
        res = asyncio.run(run_stage(args.url, pool, c, len(pool)))
        print(f"{res['concurrency']:>5} {res['requests']:>6} {res['wall_s']:>7} "
              f"{res['throughput_rps']:>7} {res['p50_ms']:>7} {res['p95_ms']:>7} "
              f"{res['p99_ms']:>7}  {res['statuses']}")

    print("\nreading the results:")
    print("  - flat p99 as concurrency climbs  => healthy horizontal scaling")
    print("  - p99 climbing sharply            => a bottleneck; check Firestore")
    print("  - 'try_again'/http_503 > 0        => transaction contention remains")
    print("  - 'already_voted' unexpectedly    => code pool too small / reused")


if __name__ == "__main__":
    main()
