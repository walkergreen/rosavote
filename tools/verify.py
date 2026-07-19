"""
Public verification script.

Anyone — observers, either side of the referendum, any member — can run this
against the three published files with no access to the live system. It needs
only Python's standard library.

    python verify.py ballots.csv used_codes.csv chain_head.txt [--committed committed_code_hashes.csv]

Checks:
  1. Chain integrity: recompute the whole chain from ballots.csv and confirm
     it equals the externally-published chain_head.txt. Any altered, added, or
     removed ballot changes the head.
  2. Turnout consistency: ballot count == number of used codes.
  3. (Optional) No stuffing: every used code hash appears in the pre-committed
     code-hash list published BEFORE voting opened.
  4. Tally: recompute YES/NO from the published ballots.

Individual verifiability: a voter finds their 8-char receipt in ballots.csv and
confirms the answers next to it are what they cast.
"""

import csv
import hashlib
import json
import sys

GENESIS = "0" * 64


def link_hash(prev_hash: str, record_hash: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{record_hash}".encode()).hexdigest()


def make_record_hash(receipt: str, answers_canon: str, nonce: str) -> str:
    # MUST match vote_service.make_record_hash exactly (answers_canon is the
    # deterministic JSON produced by vote_service.canon_answers).
    return hashlib.sha256(f"{receipt}|{answers_canon}|{nonce}".encode()).hexdigest()


def load_ballots(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def check_chain(ballots, published_head):
    # Re-sort by the published record_hash — the same deterministic order the
    # builder used — then recompute.
    ordered = sorted(ballots, key=lambda b: b["record_hash"])
    prev = GENESIS
    for b in ordered:
        # Recompute record_hash from the ballot's OWN content. This is what
        # binds `choice` into the chain: editing the choice column without
        # knowing how to regenerate every downstream hash breaks verification.
        expect_rh = make_record_hash(b["receipt"], b["answers"], b["nonce"])
        if b["record_hash"] != expect_rh:
            return False, f"record_hash does not match content at receipt {b['receipt']}"
        if b["prev_hash"] != prev:
            return False, f"prev_hash mismatch at receipt {b['receipt']}"
        expect = link_hash(prev, b["record_hash"])
        if b["chain_hash"] != expect:
            return False, f"chain_hash mismatch at receipt {b['receipt']}"
        prev = b["chain_hash"]
    if prev != published_head:
        return False, "final chain head does not match published head"
    return True, "chain intact"


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    ballots_path, used_codes_path, head_path = sys.argv[1:4]
    committed_path = None
    if "--committed" in sys.argv:
        committed_path = sys.argv[sys.argv.index("--committed") + 1]

    ballots = load_ballots(ballots_path)
    published_head = open(head_path).read().strip()

    ok, msg = check_chain(ballots, published_head)
    print(f"[1] chain: {'PASS' if ok else 'FAIL'} - {msg}")

    with open(used_codes_path, newline="") as f:
        rows = list(csv.DictReader(f))
    used = [r["code_hash"] for r in rows if r["used"].strip().lower() == "true"]
    turnout_ok = len(ballots) == len(used)
    print(f"[2] turnout: {'PASS' if turnout_ok else 'FAIL'} - "
          f"{len(ballots)} ballots vs {len(used)} used codes")

    if committed_path:
        committed = set()
        with open(committed_path, newline="") as f:
            for r in csv.reader(f):
                committed.add(r[0].strip())
        stuffed = [c for c in used if c not in committed]
        print(f"[3] commitment: {'PASS' if not stuffed else 'FAIL'} - "
              f"{len(stuffed)} used codes not in pre-committed set")
    else:
        print("[3] commitment: SKIPPED (pass --committed to check)")

    tally = {}
    for b in ballots:
        a = json.loads(b["answers"])
        tally.setdefault("q1", {})[a["q1"]] = tally.setdefault("q1", {}).get(a["q1"], 0) + 1
        for q in ("q2", "q3"):
            first = a[q][0] if a[q] else "ABSTAIN"
            tally.setdefault(q + "_first_pref", {})[first] = tally.setdefault(q + "_first_pref", {}).get(first, 0) + 1
        # full STV rounds for q2/q3: export with make_blt.py and run any
        # Scottish STV tabulator (this script checks integrity, not STV rounds)
    print(f"[4] tally: {tally}")


if __name__ == "__main__":
    main()
