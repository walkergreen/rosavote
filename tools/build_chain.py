"""
Post-close batch chaining + publication.

Run ONCE after the poll closes. This replaces the live hash-chain-head that
used to be a single hot document. Because it runs after voting, it has no
scale constraint: it reads all ballots, orders them deterministically, and
builds the tamper-evident chain in one pass.

Outputs three public verification artifacts:
  - ballots.csv        anonymous ballots + chain (receipt, answers JSON, prev, hash)
  - used_codes.csv     code hashes with used flag (from the pre-committed set)
  - chain_head.txt     the final chain head hash (publish/anchor this externally)

Verifiability properties this preserves:
  - Universal: anyone recomputes tallies from ballots.csv (q1 counts; STV over q2/q3 rankings).
  - Anti-stuffing: ballot count == used-code count; every ballot is anonymous.
  - Tamper-evidence: recompute the chain from ballots.csv and compare to the
    externally-published chain_head.txt.

A note on ordering: since ballots carry no meaningful timestamp (by design,
for secrecy), we order by record_hash. Ordering is arbitrary but must be
DETERMINISTIC and REPRODUCIBLE from the published file — sorting by the
published record_hash gives exactly that. The chain proves "this set of
ballots, unaltered," not "this arrival order."
"""

import csv
import hashlib
import os

from google.cloud import firestore

POLL_ID = os.environ.get("POLL_ID", "referendum_2026")
db = firestore.Client()

CODES = db.collection(f"{POLL_ID}__codes")
BALLOTS = db.collection(f"{POLL_ID}__ballots")

GENESIS = "0" * 64


def link_hash(prev_hash: str, record_hash: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{record_hash}".encode()).hexdigest()


def build():
    # 1. Pull all ballots. At 120k yes/no records this is trivially small.
    ballots = []
    for doc in BALLOTS.stream():
        d = doc.to_dict()
        ballots.append({
            "receipt": d["receipt"],
            "answers": d["answers_canon"],
            "nonce": d["nonce"],
            "record_hash": d["record_hash"],
            # voided ballots STAY in the chain, flagged — auditors see them,
            # tallies (make_blt) exclude them, certification reports the count
            "voided": bool(d.get("voided")),
            "void_reason": d.get("void_reason", ""),
        })

    # 2. Deterministic order (reproducible from the published file).
    ballots.sort(key=lambda b: b["record_hash"])

    # 3. Chain them.
    prev = GENESIS
    for b in ballots:
        b["prev_hash"] = prev
        b["chain_hash"] = link_hash(prev, b["record_hash"])
        prev = b["chain_hash"]
    chain_head = prev

    # 4. Write ballots.csv
    with open("ballots.csv", "w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["receipt", "answers", "nonce", "record_hash", "prev_hash", "chain_hash", "voided", "void_reason"]
        )
        w.writeheader()
        w.writerows(ballots)

    # 5. Write used_codes.csv (turnout accounting; no linkage to ballots).
    used = 0
    with open("used_codes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code_hash", "used"])
        for doc in CODES.stream():
            d = doc.to_dict()
            is_used = bool(d.get("used"))
            used += 1 if is_used else 0
            w.writerow([doc.id, is_used])

    # 6. Publish the chain head. Anchor this value externally (email observers,
    #    post publicly, OpenTimestamps) so it can't be retroactively changed.
    with open("chain_head.txt", "w") as f:
        f.write(chain_head + "\n")

    # 7. Tally + integrity summary for the operator.
    tally = {"YES": 0, "NO": 0}
    for b in ballots:
        tally[b["choice"]] = tally.get(b["choice"], 0) + 1

    print(f"ballots: {len(ballots)}")
    print(f"used codes: {used}")
    print(f"ballots == used codes: {len(ballots) == used}")
    print(f"tally: {tally}")
    print(f"chain head: {chain_head}")
    return chain_head


if __name__ == "__main__":
    build()
