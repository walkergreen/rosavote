#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Code generation + distribution-manifest builder, wired to the real roll:
`proj-tmc-mem-dsa.main.ak_primary_id` (deduplicated primary-AKID mapping)
joined to `ak_user_fields_pivoted` for chapter assignment.

Run ONCE per election, at the eligibility cutoff. Produces:
  1. Firestore code documents (hashed) per member, scoped to their chapter's
     poll_id (--write; --dry-run skips Firestore).
  2. <out>/distribution_manifest.csv — one row per member, best single
     channel (EMAIL FIRST per the adopted cost model; SMS only if no email;
     "postcard" rows have no destination and go to the mailing-house list).
  3. <out>/contact_index_private.csv — SENSITIVE contact -> member map that
     powers the enumeration-safe /resend. Never publish.

Eligibility (Const. Art. V: paid up at time of election):
    is_primary AND membership_status = 'Member in Good Standing'
Dedup is on the primary ActionKit ID; contacts are aggregated across the
member's whole dedup group, so any on-record email/phone can receive a
resend, but each member gets exactly ONE code.

Chapter -> poll mapping comes from the poll configs (Firestore config__polls,
falling back to the CHAPTERS seed): a poll matches roll rows whose chapter
equals its `roll_chapter` field (defaulting to its display `name`). Roll
chapters with no matching poll are counted and skipped.

Usage:
    python3 tools/generate_codes.py --demo             # offline shape demo
    python3 tools/generate_codes.py --dry-run          # BigQuery, files only
    python3 tools/generate_codes.py --write            # + Firestore code docs
        [--project proj-tmc-mem-dsa] [--fs-project dsa-org-tools]
        [--poll debs_endorsement__nyc ...] [--limit N] [--out-dir codes_out]

PII: row-level member data only ever touches the output CSVs on local disk —
this tool prints aggregate counts alone.
"""

import argparse
import csv
import hashlib
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CODE_BYTES = 12  # secrets.token_urlsafe(12) -> ~16 chars, URL-safe
BASE_URL = os.environ.get("VOTE_BASE_URL", "https://vote.dsausa.org")

# Arrays come back TO_JSON_STRING-encoded so the bq CLI's CSV output stays
# one column per field; chapters are inlined as escaped string literals.
ROLL_QUERY = """
WITH eligible AS (
  SELECT actionkit_id, dedup_group_id
  FROM `{roll}.ak_primary_id`
  WHERE is_primary AND membership_status = 'Member in Good Standing'
),
contacts AS (
  SELECT dedup_group_id,
         ARRAY_AGG(DISTINCT NULLIF(email_norm, '') IGNORE NULLS) AS all_emails,
         ARRAY_AGG(DISTINCT NULLIF(phone_norm, '') IGNORE NULLS) AS all_phones
  FROM `{roll}.ak_primary_id`
  GROUP BY 1
)
SELECT
  e.actionkit_id           AS member_akid,
  f.chapter                AS roll_chapter,
  TO_JSON_STRING(c.all_emails) AS emails_json,
  TO_JSON_STRING(c.all_phones) AS phones_json
FROM eligible e
JOIN `{roll}.ak_user_fields_pivoted` f ON f.user_id = e.actionkit_id
JOIN contacts c ON c.dedup_group_id = e.dedup_group_id
WHERE f.chapter IN ({chapters})
"""


def new_code() -> str:
    return secrets.token_urlsafe(CODE_BYTES)


def code_hash(code_plaintext: str) -> str:
    return hashlib.sha256(code_plaintext.encode()).hexdigest()


def load_poll_mapping(polls: dict, only: list) -> dict:
    """roll chapter value -> poll_id, from poll configs."""
    mapping = {}
    for pid, cfg in polls.items():
        if only and pid not in only:
            continue
        mapping[cfg.get("roll_chapter") or cfg["name"]] = pid
    return mapping


def _norm_contact(c):
    """Match vote_service._norm_contact so a submitted contact hashes to the
    same key the resend index was provisioned under."""
    c = (c or "").strip().lower()
    if "@" in c:
        return c
    return "".join(ch for ch in c if ch.isdigit() or ch == "+")


def _resend_hash(pid, contact):
    import hashlib
    return hashlib.sha256(f"{pid}|{_norm_contact(contact)}".encode()).hexdigest()


def generate(rows, mapping, db=None, resend_index=False):
    """rows: iterables/dicts with member_akid, roll_chapter, all_emails,
    all_phones. Returns (manifest, contact_index, skipped_chapter_counts).

    resend_index: when True (and db given), also provision the OPT-IN
    `{poll}__resend` collection that powers self-serve /resend — one doc per
    on-record contact (keyed by a salted hash of the contact) holding the
    member's ballot link + their on-record destinations. This intentionally
    stores the link (= the code) in Firestore, so it's off by default; enable
    it per election only where self-serve resend is wanted."""
    manifest, contact_index = [], []
    skipped = {}
    seen = set()
    resend_docs = 0
    for r in rows:
        akid = r["member_akid"]
        if akid in seen:
            continue                      # dedup safety net
        seen.add(akid)
        pid = mapping.get(r["roll_chapter"])
        if pid is None:
            skipped[r["roll_chapter"]] = skipped.get(r["roll_chapter"], 0) + 1
            continue
        member_id = f"AK{akid}"
        code = new_code()
        ch = code_hash(code)
        vote_link = f"{BASE_URL}/p/{pid}/v/{code}"
        if db is not None:
            db.collection(f"{pid}__codes").document(ch).set({
                "used": False, "member_id": member_id, "chapter": r["roll_chapter"],
            })

        emails = [e for e in (r.get("all_emails") or []) if e]
        phones = [p for p in (r.get("all_phones") or []) if p]
        # EMAIL FIRST (near-free); SMS only when no email; postcard tier last
        if emails:
            channel, dest = "email", sorted(emails)[0]
        elif phones:
            channel, dest = "sms", sorted(phones)[0]
        else:
            channel, dest = "postcard", ""
        manifest.append({
            "member_id": member_id, "poll_id": pid, "chapter": r["roll_chapter"],
            "channel": channel, "destination": dest,
            "vote_link": vote_link,
        })
        for contact in set(e.lower() for e in emails) | set(phones):
            contact_index.append({"contact": contact.strip(), "member_id": member_id,
                                  "poll_id": pid, "code_hash": ch})
        if db is not None and resend_index and (emails or phones):
            dests = sorted(set(e.lower().strip() for e in emails)) + sorted(set(phones))
            for contact in dests:
                db.collection(f"{pid}__resend").document(_resend_hash(pid, contact)).set(
                    {"link": vote_link, "dests": dests})
                resend_docs += 1
    if resend_index:
        print(f"resend index docs written: {resend_docs}")
    return manifest, contact_index, skipped


def fetch_rows(project, roll_dataset, chapters, limit, out_dir):
    """Pull the roll via the bq CLI (same auth as `bq query` in a shell — no
    ADC needed). Raw rows land in a temp CSV under out_dir, are parsed, and
    the temp file is removed by the caller's cleanup."""
    import json
    import subprocess
    lits = ", ".join("'" + c.replace("\\", "\\\\").replace("'", "\\'") + "'"
                     for c in chapters)
    q = ROLL_QUERY.format(roll=f"{project}.{roll_dataset}", chapters=lits)
    if limit:
        q += f"\nLIMIT {int(limit)}"
    os.makedirs(out_dir, exist_ok=True)
    raw = os.path.join(out_dir, "_roll_raw.csv")
    with open(raw, "w") as f:
        subprocess.run(
            ["bq", "query", "--nouse_legacy_sql", "--format=csv",
             "--max_rows=1000000", "--project_id", project, q],
            stdout=f, stderr=subprocess.DEVNULL, check=True)
    with open(raw, newline="") as f:
        for row in csv.DictReader(f):
            yield {"member_akid": int(row["member_akid"]),
                   "roll_chapter": row["roll_chapter"],
                   "all_emails": json.loads(row["emails_json"] or "[]"),
                   "all_phones": json.loads(row["phones_json"] or "[]")}
    os.remove(raw)


def write_outputs(manifest, contact_index, skipped, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    mpath = os.path.join(out_dir, "distribution_manifest.csv")
    with open(mpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["member_id", "poll_id", "chapter",
                                          "channel", "destination", "vote_link"])
        w.writeheader()
        w.writerows(manifest)
    cpath = os.path.join(out_dir, "contact_index_private.csv")
    with open(cpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["contact", "member_id", "poll_id", "code_hash"])
        w.writeheader()
        w.writerows(contact_index)

    by_channel = {}
    by_poll = {}
    for m in manifest:
        by_channel[m["channel"]] = by_channel.get(m["channel"], 0) + 1
        by_poll[m["poll_id"]] = by_poll.get(m["poll_id"], 0) + 1
    print(f"members coded: {len(manifest)}")
    for pid in sorted(by_poll):
        print(f"    {pid}: {by_poll[pid]}")
    for chnl in ("email", "sms", "postcard"):
        print(f"  {chnl} sends: {by_channel.get(chnl, 0)}")
    if skipped:
        print(f"  skipped (no poll for roll chapter): {sum(skipped.values())} "
              f"members across {len(skipped)} chapters")
    print(f"wrote {mpath} (for the send platform)")
    print(f"wrote {cpath} (SENSITIVE — powers /resend; never publish)")


def firestore_bulk_client(fs_project):
    from google.cloud import firestore
    db = firestore.Client(project=fs_project)

    class Bulk:
        """Same .collection().document().set() shape, but batched."""
        def __init__(self):
            self.writer = db.bulk_writer()
            self.n = 0
        def collection(self, name):
            outer = self
            class C:
                def document(self, key):
                    ref = db.collection(name).document(key)
                    class D:
                        def set(self, doc):
                            outer.writer.set(ref, doc)
                            outer.n += 1
                            if outer.n % 10000 == 0:
                                print(f"    ...{outer.n} code docs queued")
                    return D()
            return C()
        def flush(self):
            self.writer.close()
            print(f"    {self.n} code docs written")
    return Bulk()


def demo():
    mapping = {"New York City": "debs_endorsement__nyc", "Chicago": "debs_endorsement__chi"}
    rows = [
        {"member_akid": 1, "roll_chapter": "New York City",
         "all_emails": ["a@example.com", "a.old@example.com"], "all_phones": ["+12125550111"]},
        {"member_akid": 2, "roll_chapter": "New York City", "all_emails": [],
         "all_phones": ["+12125550122"]},
        {"member_akid": 3, "roll_chapter": "Chicago", "all_emails": ["family@example.com"],
         "all_phones": []},
        {"member_akid": 4, "roll_chapter": "Chicago", "all_emails": ["family@example.com"],
         "all_phones": []},
        {"member_akid": 5, "roll_chapter": "Boise", "all_emails": ["c@example.com"],
         "all_phones": []},
    ]
    manifest, idx, skipped = generate(rows, mapping, db=None)
    write_outputs(manifest, idx, skipped, "codes_out_demo")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="offline demo rows")
    mode.add_argument("--dry-run", action="store_true", help="BigQuery, files only")
    mode.add_argument("--write", action="store_true", help="BigQuery + Firestore codes")
    ap.add_argument("--project", default="proj-tmc-mem-dsa", help="BigQuery project")
    ap.add_argument("--roll-dataset", default="main")
    ap.add_argument("--fs-project", default="dsa-org-tools", help="Firestore project")
    ap.add_argument("--poll", action="append", default=[],
                    help="restrict to these poll_ids (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="row cap for test runs")
    ap.add_argument("--out-dir", default="codes_out")
    ap.add_argument("--resend-index", action="store_true",
                    help="also provision the opt-in {poll}__resend index for "
                         "self-serve /resend (stores each member's link + on-record "
                         "contacts in Firestore; --write only)")
    args = ap.parse_args()

    if args.demo:
        demo()
        return

    # poll configs: Firestore config__polls when seeded, else the CHAPTERS seed
    import vote_service
    try:
        from google.cloud import firestore
        client = firestore.Client(project=args.fs_project)
        polls = {s.id: (s.to_dict() or {}) for s in client.collection("config__polls").stream()}
        polls = {pid: d for pid, d in polls.items() if not d.get("archived")}
    except Exception:
        polls = {}
    if not polls:
        polls = vote_service.CHAPTERS
    mapping = load_poll_mapping(polls, args.poll)
    print(f"polls: {len(mapping)} chapter mapping(s): "
          + ", ".join(f"{c!r}->{p}" for c, p in sorted(mapping.items())))

    # PII guard: on any failure, keep the traceback (which may quote row
    # values) out of the terminal — full details go to a local log instead.
    try:
        rows = fetch_rows(args.project, args.roll_dataset, list(mapping),
                          args.limit, args.out_dir)
        db = firestore_bulk_client(args.fs_project) if args.write else None
        manifest, idx, skipped = generate(rows, mapping, db=db,
                                          resend_index=args.resend_index and args.write)
        if db is not None:
            db.flush()
        write_outputs(manifest, idx, skipped, args.out_dir)
    except Exception as e:
        import traceback
        os.makedirs(args.out_dir, exist_ok=True)
        log = os.path.join(args.out_dir, "generate_codes_error.log")
        with open(log, "w") as f:
            traceback.print_exc(file=f)
        print(f"ERROR: {type(e).__name__} — full traceback in {log} "
              "(may contain row data; review locally, don't paste)")
        sys.exit(1)


if __name__ == "__main__":
    main()
