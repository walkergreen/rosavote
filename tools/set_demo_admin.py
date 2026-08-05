#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Set the role of the PUBLIC demo admin token in config__admins.

    python3 tools/set_demo_admin.py --role national --write
    python3 tools/set_demo_admin.py --role chapter --polls demo_sandbox --write

WARNING — DEMO INSTANCES ONLY. The demo token is printed publicly on the
console sign-in page, so making it `national` grants anyone who opens the
console FULL control of every election (build/edit/delete), voter-roll import,
and access to all ballots. Only run --role national on an instance whose data
is entirely synthetic. Never on a service holding real elections.

Uses ADC if available; otherwise falls back to the current gcloud user's
access token (no service-account key needed).
"""
import argparse
import hashlib
import os
import sys

DEMO_TOKEN = "DEMO-ADMIN-TOKEN-2026"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=["national", "chapter"], required=True)
    ap.add_argument("--polls", nargs="*", default=["demo_sandbox"],
                    help="chapter-scope poll_ids (ignored for national)")
    ap.add_argument("--fs-project", default="rosavote-app")
    ap.add_argument("--fs-database", default=os.environ.get("FIRESTORE_DATABASE", ""),
                    help="named Firestore database (default: (default)); matches the "
                         "service's FIRESTORE_DATABASE so the check hits the same data")
    ap.add_argument("--token", default=DEMO_TOKEN, help="plaintext demo token")
    # GUARDRAIL: a NATIONAL public demo token is a published national-root
    # credential — anyone who opens the console gets full control of every
    # election and every voter record. That is only ever acceptable on an
    # instance whose data is entirely synthetic. Writing one therefore requires
    # an explicit attestation AND a scan that refuses if the target holds any
    # poll not flagged demo:true (i.e. looks like real data). --force overrides
    # the scan, deliberately awkwardly.
    ap.add_argument("--confirm-synthetic-data", action="store_true",
                    help="required to WRITE a national demo token: attests the "
                         "target instance holds only synthetic data")
    ap.add_argument("--force", action="store_true",
                    help="override the real-data scan (dangerous)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--write", action="store_true")
    a = ap.parse_args()

    if a.role == "national":
        sys.stderr.write(
            "\n  ⚠  Setting the PUBLIC demo token to NATIONAL root.\n"
            "     Anyone opening the console gets full admin. Demo instances only.\n\n")
        if a.write and not a.confirm_synthetic_data:
            sys.stderr.write(
                "  ✋ Refusing to write a NATIONAL public demo token without\n"
                "     --confirm-synthetic-data. This publishes a national-root\n"
                "     credential; run it only against an instance whose data is\n"
                "     entirely synthetic.\n\n")
            sys.exit(2)

    from google.cloud import firestore
    dbname = (a.fs_database or "").strip()
    _mk = (lambda creds=None: firestore.Client(
        project=a.fs_project, database=dbname, credentials=creds) if dbname
        else firestore.Client(project=a.fs_project, credentials=creds))
    try:
        db = _mk()
        next(iter(db.collection("config__admins").limit(1).stream()), None)
    except Exception:
        import subprocess
        import google.oauth2.credentials
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"]).decode().strip()
        db = _mk(google.oauth2.credentials.Credentials(tok))

    # Real-data scan for the national+write path: any active poll that is not
    # flagged demo:true means this instance is probably serving real elections,
    # where a published national token would expose every voter record.
    if a.role == "national" and a.write:
        real = []
        try:
            for snap in db.collection("config__polls").stream():
                d = snap.to_dict() or {}
                if not d.get("archived") and not d.get("demo"):
                    real.append(snap.id)
        except Exception as e:
            sys.stderr.write(f"  ✋ Could not verify the target holds only synthetic "
                             f"data ({type(e).__name__}); refusing. Use --force to "
                             f"override.\n")
            if not a.force:
                sys.exit(2)
        if real and not a.force:
            sys.stderr.write(
                "  ✋ Refusing: this instance has polls NOT flagged demo:true, so it\n"
                "     may hold real elections. A public national token would expose\n"
                "     every voter record. Offending poll(s): "
                + ", ".join(sorted(real)[:10])
                + (" …" if len(real) > 10 else "") + "\n"
                "     If this really is a throwaway instance, re-run with --force.\n\n")
            sys.exit(2)
        if real and a.force:
            sys.stderr.write(f"  ⚠  --force: writing despite {len(real)} non-demo "
                             "poll(s). You asserted this data is disposable.\n\n")

    doc_id = hashlib.sha256(a.token.encode()).hexdigest()
    payload = {
        "name": "Public demo (national root)" if a.role == "national" else "Public demo",
        "role": a.role,
        "polls": [] if a.role == "national" else list(a.polls),
        "active": True,
    }
    print(f"config__admins/{doc_id}  <-  role={payload['role']}, "
          f"polls={payload['polls'] or '(all)'}")
    if a.dry_run:
        print("dry-run: nothing written.")
        return
    db.collection("config__admins").document(doc_id).set(payload)
    print("written ✓")


if __name__ == "__main__":
    main()
