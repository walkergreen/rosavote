"""
Code generation + distribution-manifest builder for the referendum.

Run ONCE, before voting opens, against the frozen membership roll. Produces:
  1. Firestore code documents (hashed) per member, scoped by chapter poll_id.
  2. A distribution manifest (CSV) your SMS/email platform consumes: one row
     per member with their personalized tap-to-vote link on their BEST single
     channel (SMS if a mobile exists, else email). One message per member.

Design decisions (from the "simplest at scale" discussion):
  - Dedup on MEMBER ID, not contact. Two members sharing an email each get
    their own distinct code, so a shared inbox just holds two codes and each
    person votes once.
  - One code per member. Codes are single-use; the ballot enforces that.
  - Best single channel per member => one message each (no 2-3x cost).
  - We store ALL of a member's on-record contacts in a private lookup table
    (member_contacts) so the enumeration-safe /resend can reach every channel
    a member has WITHOUT us proactively sending to all of them.
  - Only the SHA-256 hash of each code is stored server-side. The plaintext
    lives only in the distribution manifest, which goes to the send platform
    and (ideally) a neutral committee, not the app.

This is a sketch to run in your environment; wire the BigQuery query to your
actual clean_member_table schema.
"""

import csv
import hashlib
import secrets
import os

# from google.cloud import bigquery, firestore   # enable in your environment

CODE_BYTES = 12  # secrets.token_urlsafe(12) -> ~16 chars, URL-safe
BASE_URL = os.environ.get("VOTE_BASE_URL", "https://vote.dsausa.org")


def new_code() -> str:
    return secrets.token_urlsafe(CODE_BYTES)


def code_hash(code_plaintext: str) -> str:
    return hashlib.sha256(code_plaintext.encode()).hexdigest()


def poll_id_for_chapter(chapter_slug: str) -> str:
    # one poll instance per chapter; questions are shared, timing/roll are not
    return f"debs_endorsement__{chapter_slug}"


# ---- 1. pull the frozen, deduped roll -----------------------------------
# Dedup on member_id. Pick best channel: mobile if present, else email.
# Collect ALL contacts for the private resend lookup.
ROLL_QUERY = """
WITH deduped AS (
  SELECT
    member_id,
    ANY_VALUE(chapter_slug)              AS chapter_slug,
    ANY_VALUE(mobile_e164)               AS mobile,       -- normalized +1..., or NULL
    ANY_VALUE(primary_email)             AS email,
    ARRAY_AGG(DISTINCT all_email IGNORE NULLS) AS all_emails,
    ANY_VALUE(mobile_e164)               AS all_mobile
  FROM `proj-tmc-mem-dsa.main.clean_member_table`
  WHERE memb_status IN ('M')            -- members in good standing as of cutoff
  GROUP BY member_id
)
SELECT member_id, chapter_slug, mobile, email, all_emails
FROM deduped
"""


def generate(rows, db=None):
    """
    rows: iterable of dicts with keys member_id, chapter_slug, mobile, email,
          all_emails (list). In production these come from the ROLL_QUERY.
    db:   firestore.Client() (None => dry run, manifest only)
    Returns the distribution manifest rows.
    """
    manifest = []          # goes to the SMS/email platform
    contact_index = []     # private: contact -> member_id, for resend lookup
    seen_members = set()

    for r in rows:
        mid = r["member_id"]
        if mid in seen_members:
            continue            # dedup safety net
        seen_members.add(mid)

        chapter = r["chapter_slug"]
        pid = poll_id_for_chapter(chapter)
        code = new_code()
        ch = code_hash(code)

        # store ONLY the hash server-side, scoped to the member's chapter poll
        if db is not None:
            db.collection(f"{pid}__codes").document(ch).set({
                "used": False,
                "member_id": mid,          # for turnout accounting / resend only
                "chapter": chapter,
            })

        # best single channel: SMS if mobile present, else email
        if r.get("mobile"):
            channel, dest = "sms", r["mobile"]
        else:
            channel, dest = "email", r.get("email")

        link = f"{BASE_URL}/p/{pid}/v/{code}"
        manifest.append({
            "member_id": mid,
            "chapter": chapter,
            "channel": channel,
            "destination": dest,
            "vote_link": link,
        })

        # private contact index for enumeration-safe resend: map every
        # on-record contact to this member (so resend can reach all channels
        # WITHOUT us proactively blasting all of them)
        contacts = set(r.get("all_emails") or [])
        if r.get("email"):
            contacts.add(r["email"])
        if r.get("mobile"):
            contacts.add(r["mobile"])
        for c in contacts:
            contact_index.append({"contact": c.strip().lower(), "member_id": mid,
                                   "chapter": chapter, "code_hash": ch})

    return manifest, contact_index


def write_outputs(manifest, contact_index):
    with open("distribution_manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["member_id", "chapter", "channel",
                                          "destination", "vote_link"])
        w.writeheader()
        w.writerows(manifest)

    # This file is SENSITIVE (maps contacts to codes). Keep it access-controlled;
    # it powers /resend. Do NOT publish it.
    with open("contact_index_private.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["contact", "member_id", "chapter",
                                          "code_hash"])
        w.writeheader()
        w.writerows(contact_index)

    # channel split for cost estimation
    sms = sum(1 for m in manifest if m["channel"] == "sms")
    email = sum(1 for m in manifest if m["channel"] == "email")
    print(f"members: {len(manifest)}")
    print(f"  SMS sends:   {sms}")
    print(f"  email sends: {email}")
    print("wrote distribution_manifest.csv (for send platform)")
    print("wrote contact_index_private.csv (SENSITIVE - for /resend only)")


if __name__ == "__main__":
    # DRY-RUN demo with fake rows so you can see the output shape
    demo = [
        {"member_id": "M001", "chapter_slug": "nyc", "mobile": "+12125550111",
         "email": "a@example.com", "all_emails": ["a@example.com", "a.old@example.com"]},
        {"member_id": "M002", "chapter_slug": "nyc", "mobile": None,
         "email": "b@example.com", "all_emails": ["b@example.com"]},
        # two members sharing an email -> each still gets a distinct code
        {"member_id": "M003", "chapter_slug": "chi", "mobile": "+13125550199",
         "email": "family@example.com", "all_emails": ["family@example.com"]},
        {"member_id": "M004", "chapter_slug": "chi", "mobile": None,
         "email": "family@example.com", "all_emails": ["family@example.com"]},
    ]
    manifest, idx = generate(demo, db=None)
    write_outputs(manifest, idx)
