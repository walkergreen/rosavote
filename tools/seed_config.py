#!/usr/bin/env python3
"""
Seed / manage self-service config in Firestore (project dsa-org-tools).

    python3 tools/seed_config.py polls            # push CHAPTERS seed -> config__polls
    python3 tools/seed_config.py polls --force    # overwrite existing docs
    python3 tools/seed_config.py admin --name "Walker" --role national
    python3 tools/seed_config.py admin --name "NYC admins" --role chapter \
        --polls debs_endorsement__nyc

Seeding polls is idempotent by default: existing docs are left alone so live
edits made in the admin console are never clobbered.

`admin` mints a token, prints it ONCE (only its SHA-256 is stored in
config__admins), and registers the scope. Store the printed token in a
password manager; it cannot be recovered.
"""

import argparse
import hashlib
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("polls", help="seed config__polls from the CHAPTERS fallback")
    p.add_argument("--force", action="store_true", help="overwrite existing poll docs")
    a = sub.add_parser("admin", help="mint a scoped admin token")
    a.add_argument("--name", required=True, help="who/what this token identifies")
    a.add_argument("--role", choices=["national", "chapter"], required=True)
    a.add_argument("--polls", nargs="*", default=[], help="poll_ids a chapter token may administer")
    args = ap.parse_args()

    import vote_service  # noqa: E402  (imports firestore client — needs GCP creds)
    db = vote_service.db

    if args.cmd == "polls":
        coll = db.collection(vote_service.CONFIG_COLL)
        wrote = skipped = 0
        for pid, cfg in vote_service.CHAPTERS.items():
            ref = coll.document(pid)
            if not args.force and ref.get().exists:
                skipped += 1
                continue
            ref.set(vote_service.cfg_to_doc(cfg))
            wrote += 1
        print(f"config__polls: wrote {wrote}, left {skipped} existing doc(s) untouched")

    elif args.cmd == "admin":
        if args.role == "chapter" and not args.polls:
            ap.error("--role chapter requires --polls")
        token = secrets.token_urlsafe(24)
        db.collection(vote_service.ADMINS_COLL).document(
            hashlib.sha256(token.encode()).hexdigest()
        ).set({
            "name": args.name,
            "role": args.role,
            "polls": args.polls,
            "active": True,
        })
        print(f"Admin token for {args.name!r} ({args.role}"
              + (f", polls: {', '.join(args.polls)}" if args.polls else "") + "):")
        print(f"\n    {token}\n")
        print("Shown ONCE — only its hash is stored. Save it in a password manager.")


if __name__ == "__main__":
    main()
