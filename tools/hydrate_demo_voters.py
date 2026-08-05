#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Hydrate a DEMO election's anonymous ballots into full, non-secret ballot
records with SYNTHETIC voters — so the voters admin console shows a turnout
list you can troubleshoot (look up a voter's votes, void + reissue, adjust
weight), and the tamper-evidence chain / public verification files populate.

DEMO ONLY. This attaches fabricated voter identities to ballots and makes the
contest non-secret — exactly what a real secret ballot must never allow. Use it
only on demo polls. The voters are obviously synthetic (DEMO##### ids).

For each existing ballot it:
  * generates a synthetic member_id + chapter + code hash,
  * (re)builds the hash-chain fields (answers_canon, nonce, record_hash) so the
    published chain verifies,
  * writes a {poll}__codes doc (used=true) and a {poll}__ballots doc,
  * removes the old secret {poll}__delegate_ballots doc (moved, not copied).
You must ALSO flip the question to secret:false (the tool prints the command).

    python3 tools/hydrate_demo_voters.py --poll npc_cochair_2025 --question co --dry-run
    python3 tools/hydrate_demo_voters.py --poll npc_cochair_2025 --question co --write

PII: synthetic data only; prints counts, never a voter or a ballot.
"""

import argparse
import hashlib
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO_CHAPTERS = ["Metro DC", "New York City", "Chicago", "Los Angeles",
                 "East Bay", "Philadelphia", "Boston", "Seattle", "Twin Cities",
                 "Atlanta", "Pittsburgh", "Central New Jersey"]


def canon(a):
    return json.dumps(a, separators=(",", ":"), sort_keys=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--poll", required=True)
    ap.add_argument("--question", required=True, help="the ranked question key (e.g. co / al)")
    ap.add_argument("--fs-project", default="rosavote-app")
    ap.add_argument("--tag", default="", help="member_id tag (default: derived from poll)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--write", action="store_true")
    a = ap.parse_args()

    from google.cloud import firestore
    try:
        db = firestore.Client(project=a.fs_project)
        next(iter(db.collection(f"{a.poll}__delegate_ballots").limit(1).stream()), None)
    except Exception:
        # no ADC in this shell — build creds from the gcloud user token
        import subprocess
        import google.oauth2.credentials
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"]).decode().strip()
        db = firestore.Client(project=a.fs_project,
                              credentials=google.oauth2.credentials.Credentials(tok))
    tag = a.tag or "".join(x for x in a.poll.upper() if x.isalnum())[:8]

    src = db.collection(f"{a.poll}__delegate_ballots")
    n = created = 0
    writer = db.bulk_writer() if a.write else None
    for i, snap in enumerate(src.stream()):
        d = snap.to_dict() or {}
        ranking = d.get(a.question)
        receipt = d.get("receipt") or ("R" + secrets.token_hex(4).upper())
        if not ranking:
            continue
        n += 1
        member_id = f"DEMO{tag}{i:05d}"
        chapter = DEMO_CHAPTERS[i % len(DEMO_CHAPTERS)]
        answers = {a.question: [str(x).upper() for x in ranking]}
        ac = canon(answers)
        nonce = secrets.token_hex(8)
        rh = hashlib.sha256(f"{receipt}|{ac}|{nonce}".encode()).hexdigest()
        code_hash = hashlib.sha256(f"demo|{a.poll}|{member_id}".encode()).hexdigest()
        if a.write:
            writer.set(db.collection(f"{a.poll}__codes").document(code_hash),
                       {"used": True, "member_id": member_id, "chapter": chapter})
            writer.set(db.collection(f"{a.poll}__ballots").document(rh),
                       {"receipt": receipt, "answers": answers, "answers_canon": ac,
                        "nonce": nonce, "record_hash": rh, "code_hash": code_hash,
                        "member_id": member_id, "chapter": chapter,
                        "day_bucket": firestore.SERVER_TIMESTAMP})
            writer.delete(snap.reference)   # move out of the secret collection
            created += 1
            if created % 500 == 0:
                print(f"    ...{created} hydrated")
    if writer:
        writer.close()

    print(f"poll={a.poll} question={a.question}: {n} ballots "
          f"{'hydrated' if a.write else '(dry run — would hydrate)'}")
    if a.write:
        print("NEXT: flip the question to non-secret + refresh, e.g.:")
        print(f"  - set questions[].secret=false for {a.question!r} on {a.poll}")
        print(f"  - clear its frozen published results so it recomputes")


if __name__ == "__main__":
    main()
