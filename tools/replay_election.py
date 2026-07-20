#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Replay a real election through the LIVE voting API — as if every ballot were
cast from a separate remote device — then prove the stored result matches.

This is three tests in one:
  1. End-to-end integration: every ballot goes through the real code-claim
     transaction, validation, and dual-collection storage — not just the
     tabulator.
  2. Load / concurrency stress: ballots are POSTed concurrently from a pool
     of worker "devices"; reports throughput (votes/sec) and any failures,
     including one-code-one-vote races.
  3. Tabulation correctness: after casting, it reads the STORED ballots back
     through the results API and confirms the winners equal a direct count of
     the source BLT.

    python3 tools/replay_election.py tools/blt_fixtures/nlc_steering_19c13s.blt \\
        --base https://<host> --token $ADMIN_TOKEN [--workers 40] [--keep]

Creates a throwaway poll `replay_<name>` and, unless --keep, unpublishes and
leaves it finalized (ballots/codes remain as the record; delete manually if
desired). Weighted ballots are expanded to one device each, so the stored
election exactly equals the BLT's weighted count.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stv_tabulate  # noqa: E402


def load_blt(path):
    text = open(path, encoding="utf-8").read()
    n_c, seats, withdrawn, ballots, names, title = stv_tabulate.parse_blt(text)
    expanded = []
    for w, ranks in ballots:
        for _ in range(w):
            expanded.append(ranks)     # one device per weighted ballot
    return n_c, seats, names, title, expanded, text


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("blt")
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--token", required=True)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    n_c, seats, names, title, ballots, text = load_blt(args.blt)
    pid = "replay_" + os.path.basename(args.blt).replace(".blt", "").replace(".", "_")[:40]
    qkey = "contest"
    ids = [f"C{i+1}" for i in range(n_c)]
    options = [{"id": ids[i], "name": names[i]} for i in range(n_c)]
    hdr = {"X-Admin-Token": args.token, "Content-Type": "application/json"}
    S = requests.Session()

    print(f"Replaying {os.path.basename(args.blt)}: {n_c} candidates, {seats} seats, "
          f"{len(ballots)} ballots (weighted-expanded) -> poll {pid}")

    # 1. create the poll (non-secret so we can read stored ballots back)
    cfg = {"name": f"Replay — {title}", "questions": [{
        "key": qkey, "type": "ranked", "title": title, "seats": seats,
        "options": options}], "unfinalize": True}
    r = S.post(f"{args.base}/admin/api/polls/{pid}", headers=hdr, json=cfg)
    r.raise_for_status()

    # 2. mint one code per ballot
    members = [{"member_id": f"R{i:06d}"} for i in range(len(ballots))]
    r = S.post(f"{args.base}/admin/api/polls/{pid}/voters/import", headers=hdr,
               json={"members": members})
    r.raise_for_status()
    created = r.json()["created"]
    if len(created) < len(ballots):     # some members may pre-exist on a re-run
        print(f"  note: only {len(created)} fresh codes; using those")
    pairs = list(zip(created, ballots))

    # 3. cast concurrently — each worker is a "remote device"
    ok = {"n": 0}
    errs = []

    def cast(pair):
        code, ranks = pair
        answer = [ids[r - 1] for r in ranks if 1 <= r <= n_c]
        if not answer:
            answer = ["ABSTAIN"]
        try:
            resp = S.post(f"{args.base}/p/{pid}/vote",
                          json={"code": code["code"], "answers": {qkey: answer}}, timeout=30)
            if resp.status_code == 200 and resp.json().get("status") == "recorded":
                ok["n"] += 1
            else:
                errs.append((resp.status_code, resp.text[:80]))
        except Exception as e:
            errs.append(("exc", str(e)[:80]))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(cast, pairs))
    dt = time.time() - t0
    rate = ok["n"] / dt if dt else 0
    print(f"  cast {ok['n']}/{len(pairs)} in {dt:.1f}s = {rate:.0f} votes/sec "
          f"({args.workers} concurrent devices){'  ERRORS: ' + str(len(errs)) if errs else ''}")
    if errs:
        for code, msg in errs[:5]:
            print(f"    err {code}: {msg}")

    # 4. read the stored result back and compare to a direct BLT count
    r = S.get(f"{args.base}/admin/api/polls/{pid}/results?live=1&fresh=1", headers=hdr)
    r.raise_for_status()
    stored = next(q for q in r.json()["questions"] if q["key"] == qkey)
    # map stored winner NAMES back; direct count from the source BLT
    direct = stv_tabulate.count(text)
    stored_set = set(stored["winners"])
    direct_set = set(direct["winners"])
    match = stored_set == direct_set
    print(f"  stored winners: {len(stored['winners'])}  |  direct BLT winners: {len(direct['winners'])}")
    print(f"  WINNERS MATCH (stored-API vote path == direct BLT count): {match}")
    if not match:
        print("    only in stored:", sorted(stored_set - direct_set))
        print("    only in direct:", sorted(direct_set - stored_set))

    if not args.keep:
        S.post(f"{args.base}/admin/api/polls/{pid}/close", headers=hdr, json={})
    print("done.")
    sys.exit(0 if (match and not errs) else 1)


if __name__ == "__main__":
    main()
