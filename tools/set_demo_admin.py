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
    ap.add_argument("--token", default=DEMO_TOKEN, help="plaintext demo token")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--write", action="store_true")
    a = ap.parse_args()

    if a.role == "national":
        sys.stderr.write(
            "\n  ⚠  Setting the PUBLIC demo token to NATIONAL root.\n"
            "     Anyone opening the console gets full admin. Demo instances only.\n\n")

    from google.cloud import firestore
    try:
        db = firestore.Client(project=a.fs_project)
        next(iter(db.collection("config__admins").limit(1).stream()), None)
    except Exception:
        import subprocess
        import google.oauth2.credentials
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"]).decode().strip()
        db = firestore.Client(project=a.fs_project,
                              credentials=google.oauth2.credentials.Credentials(tok))

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
