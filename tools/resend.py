# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Enumeration-safe, rate-limited code resend.

This is the ONLY self-service send in the system, and it's built so a leaked
link can't be used to run up messaging cost:

  - The contact a user types is a LOOKUP KEY, never a destination. We look it
    up against the private contact index (built by generate_codes.py). If it
    matches a member, we resend that member's EXISTING code to that member's
    ON-RECORD contacts only. We never send to the typed contact unless it is
    itself already on the member's record.
  - So an attacker can only ever trigger sends to contacts ALREADY on the roll
    (i.e. to real members), never to numbers/addresses they control. There is
    no arbitrary-destination send. Cost is bounded to at most one resend per
    real member per window.
  - Anti-enumeration: the response is identical whether or not the contact
    matched, so the endpoint can't be used to probe who is a member.
  - Rate limited per contact, per IP, and with a global circuit breaker.

Endpoint:
  POST /resend   { contact }   -> always: {"status":"ok"} (generic)

Requires the private contact index loaded into Firestore (or a lookup service).
"""

import hashlib
import os
import re
import time

from flask import request, jsonify
from google.cloud import firestore

db = firestore.Client()

# tuning
PER_CONTACT_COOLDOWN_S = 15 * 60      # one resend per contact per 15 min
PER_IP_MAX_PER_HOUR = 20              # cap a single IP
GLOBAL_MAX_PER_MINUTE = 200          # circuit breaker: pause if exceeded

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^\+?1?\D*(\d{3})\D*(\d{3})\D*(\d{4})$")


def normalize_contact(raw: str):
    s = (raw or "").strip().lower()
    if EMAIL_RE.match(s):
        return ("email", s)
    m = PHONE_RE.match(s)
    if m:
        return ("phone", "+1" + "".join(m.groups()))
    return (None, None)


def _rl(key: str, window_s: int, limit: int) -> bool:
    """Simple Firestore fixed-window rate limiter. Returns True if allowed."""
    now = int(time.time())
    bucket = now // window_s
    ref = db.collection("rl").document(f"{key}:{bucket}")

    @firestore.transactional
    def bump(txn):
        snap = ref.get(transaction=txn)
        n = (snap.get("n") if snap.exists else 0) or 0
        if n >= limit:
            return False
        txn.set(ref, {"n": n + 1, "exp": (bucket + 1) * window_s}, merge=True)
        return True

    return bump(db.transaction())


def _send(channel: str, dest: str, link: str):
    """Hand off to your SMS/email provider. Stub here."""
    # e.g. solidarity_tech.send_sms(dest, f"Your DSA ballot: {link}")
    #      or ak_email.send(dest, template, {"link": link})
    print(f"[resend] {channel} -> {dest}: {link}")


def resend():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "0.0.0.0").split(",")[0].strip()
    data = request.get_json(silent=True) or {}
    kind, contact = normalize_contact(data.get("contact", ""))

    GENERIC = jsonify({"status": "ok"}), 200   # identical response either way

    # global circuit breaker first (cheapest defense)
    if not _rl("global", 60, GLOBAL_MAX_PER_MINUTE):
        return GENERIC
    # per-IP
    if not _rl(f"ip:{ip}", 3600, PER_IP_MAX_PER_HOUR):
        return GENERIC
    # malformed contact: return generic, do nothing
    if not kind:
        return GENERIC
    # per-contact cooldown
    if not _rl(f"c:{contact}", PER_CONTACT_COOLDOWN_S, 1):
        return GENERIC

    # look the contact up in the PRIVATE index; if no match, silently stop
    hits = list(db.collection("contact_index").where("contact", "==", contact).stream())
    if not hits:
        return GENERIC

    member_id = hits[0].to_dict()["member_id"]

    # gather this member's ON-RECORD contacts + their code + poll id
    member_rows = list(
        db.collection("contact_index").where("member_id", "==", member_id).stream()
    )
    if not member_rows:
        return GENERIC
    info = member_rows[0].to_dict()
    chapter = info["chapter"]
    pid = f"debs_endorsement__{chapter}"

    # reconstruct the vote link from the stored code hash? No - we don't store
    # plaintext. Resend uses a per-member deep link that the send platform
    # resolves, OR the platform holds the manifest and we trigger by member_id.
    # Simplest: trigger the send platform by member_id; it has the plaintext
    # link from the distribution manifest.
    _trigger_platform_resend(member_id, pid,
                             on_record_contacts=[r.to_dict()["contact"] for r in member_rows])

    return GENERIC


def _trigger_platform_resend(member_id, pid, on_record_contacts):
    """
    Re-deliver THIS member's existing ballot link to THEIR on-record contacts,
    using the same channels as the mass send (tools/senders.py). The plaintext
    link comes from the distribution manifest (member_id -> vote_link), read
    from MANIFEST_PATH — the app never stores plaintext codes. We resend to the
    member's OWN on-record contacts only, never a destination the caller typed,
    so /resend can't be used to spray a link at an arbitrary address.
    """
    import csv
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import senders

    link = None
    mpath = os.environ.get("MANIFEST_PATH", "")
    if mpath and os.path.exists(mpath):
        with open(mpath, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("member_id") == str(member_id):
                    link = row.get("vote_link")
                    break
    if not link:
        # manifest not mounted here — leave to the external send platform,
        # keyed by member_id (which holds the plaintext link). Never log the link.
        print(f"[resend] member={member_id} poll={pid} "
              f"contacts={len(on_record_contacts)} (deferred to send platform)")
        return

    mail = senders.MailgunSender()
    sms = senders.sms_sender()   # Twilio for one-off SMS if configured
    delivered = 0
    for contact in on_record_contacts:
        if "@" in contact and mail.configured():
            if mail.send_batch([{"email": contact, "link": link}]).ok:
                delivered += 1
        elif contact.replace("+", "").isdigit() and sms.configured():
            if sms.send_one(contact, link).ok:
                delivered += 1
    # counts only — never the contact or the link
    print(f"[resend] member={member_id} poll={pid} delivered={delivered}")


# In vote_service.py:  app.add_url_rule("/resend", "resend", resend, methods=["POST"])
