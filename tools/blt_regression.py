#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
BLT tabulation regression harness.

Runs every .blt in tools/blt_fixtures/ (real YDSA/NCC/chapter election ballot
files) through the shipped Scottish STV tabulator and, where seats>1, Meek STV
too. Verifies:
  * every file PARSES and COUNTS without error (real-world BLTs exercise
    skipped rankings '-', overvotes '=', weights >1, empty '1 0' ballots,
    packed weights, and withdrawn candidates);
  * the count is DETERMINISTIC — running each file twice yields identical
    winners (no hidden randomness in tabulation);
  * winner-set size == seats for every completed count.

    python3 tools/blt_regression.py            # human table
    python3 tools/blt_regression.py --json      # machine-readable (drives /accuracy)

This is a correctness harness, not a proof of OpaVote-equivalence for every
file — that anchor is the NPC At-Large 2025 election, which reproduced
OpaVote's exact 23 winners (see the /accuracy page). Here the claim is: the
tabulator handles the full variety of real BLT files deterministically and
without error, and its winner sets are stable.
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stv_tabulate  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blt_fixtures")
SCORE_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_fixtures")


def run_score(path):
    """Score fixtures: count under both Score and STAR, check determinism."""
    text = open(path, encoding="utf-8").read()
    s1 = stv_tabulate.count_score(text)
    s2 = stv_tabulate.count_score(text)
    star = stv_tabulate.count_star(text)
    pr1 = stv_tabulate.count_star_pr(text)
    pr2 = stv_tabulate.count_star_pr(text)
    return {
        "file": os.path.basename(path),
        "title": s1["title"],
        "seats": s1["seats"],
        "max_score": s1["max_score"],
        "valid_ballots": s1["valid_ballots"],
        "score_winners": s1["winners"],
        "star_winners": star["winners"],
        "starpr_winners": pr1["winners"],
        "deterministic": s1["winners"] == s2["winners"]
                         and pr1["winners"] == pr2["winners"],
        "seats_filled_ok": len(s1["winners"]) == s1["seats"]
                           and len(star["winners"]) == star["seats"]
                           and len(pr1["winners"]) == pr1["seats"],
    }


def run_one(path):
    text = open(path, encoding="utf-8").read()
    n_c, seats, withdrawn, ballots, names, title = stv_tabulate.parse_blt(text)
    weighted_ballots = sum(w for w, _ in ballots)
    r1 = stv_tabulate.count(text)
    r2 = stv_tabulate.count(text)          # determinism check
    deterministic = r1["winners"] == r2["winners"]
    entry = {
        "file": os.path.basename(path),
        "title": title,
        "candidates": n_c,
        "seats": seats,
        "ballots": len(ballots),
        "weighted_ballots": weighted_ballots,
        "valid_ballots": r1["valid_ballots"],
        "quota": r1["quota"],
        "stages": len(r1["stages"]),
        "winners": r1["winners"],
        "deterministic": deterministic,
        "seats_filled_ok": len(r1["winners"]) == seats or len(r1["winners"]) == min(seats, n_c),
        "meek_winners": None,
        "meek_matches_scottish": None,
    }
    if seats > 1:
        m = stv_tabulate.count_meek(text)
        entry["meek_winners"] = m["winners"]
        entry["meek_matches_scottish"] = set(m["winners"]) == set(r1["winners"])
    return entry


def main():
    as_json = "--json" in sys.argv
    files = sorted(glob.glob(os.path.join(FIX, "*.blt")))
    results, errors = [], []
    for f in files:
        try:
            results.append(run_one(f))
        except Exception as e:
            errors.append({"file": os.path.basename(f), "error": f"{type(e).__name__}: {e}"})

    score_results = []
    for f in sorted(glob.glob(os.path.join(SCORE_FIX, "*.blt"))):
        try:
            score_results.append(run_score(f))
        except Exception as e:
            errors.append({"file": os.path.basename(f), "error": f"{type(e).__name__}: {e}"})

    summary = {
        "elections": len(results),
        "errors": len(errors),
        "total_ballots": sum(r["ballots"] for r in results),
        "total_weighted_ballots": sum(r["weighted_ballots"] for r in results),
        "all_deterministic": all(r["deterministic"] for r in results)
                             and all(r["deterministic"] for r in score_results),
        "all_seats_filled": all(r["seats_filled_ok"] for r in results)
                            and all(r["seats_filled_ok"] for r in score_results),
        "meek_agreements": sum(1 for r in results if r["meek_matches_scottish"]),
        "meek_comparisons": sum(1 for r in results if r["meek_matches_scottish"] is not None),
        "score_elections": len(score_results),
    }

    if as_json:
        print(json.dumps({"summary": summary, "results": results,
                          "score_results": score_results, "errors": errors}, indent=2))
        return

    print(f"\nBLT REGRESSION — {len(files)} real election files\n" + "=" * 78)
    for r in results:
        flag = "OK " if (r["deterministic"] and r["seats_filled_ok"]) else "!! "
        meek = ""
        if r["meek_matches_scottish"] is not None:
            meek = "  meek=" + ("match" if r["meek_matches_scottish"] else "DIFFERS")
        print(f"{flag}{r['file']:<32} {r['candidates']:>2}c/{r['seats']}s  "
              f"{r['ballots']:>5} ballots  {r['stages']:>2} stages{meek}")
        print(f"     winners: {', '.join(r['winners'])}")
    if score_results:
        print("-" * 78 + "\nSCORE / STAR fixtures")
        for r in score_results:
            flag = "OK " if (r["deterministic"] and r["seats_filled_ok"]) else "!! "
            print(f"{flag}{r['file']:<32} {r['seats']}s max{r['max_score']}  "
                  f"{r['valid_ballots']:>4} ballots")
            print(f"     score:   {', '.join(r['score_winners'])}")
            print(f"     STAR:    {', '.join(r['star_winners'])}")
            print(f"     STAR-PR: {', '.join(r['starpr_winners'])}")
    for e in errors:
        print(f"ERR {e['file']}: {e['error']}")
    print("=" * 78)
    print(f"elections: {summary['elections']}  errors: {summary['errors']}  "
          f"ballots: {summary['total_ballots']} ({summary['total_weighted_ballots']} weighted)")
    print(f"deterministic: {summary['all_deterministic']}  "
          f"seats filled: {summary['all_seats_filled']}  "
          f"Meek≈Scottish: {summary['meek_agreements']}/{summary['meek_comparisons']}  "
          f"score/STAR fixtures: {summary['score_elections']}")
    sys.exit(1 if errors or not summary["all_deterministic"] else 0)


if __name__ == "__main__":
    main()
