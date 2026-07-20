#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Deliver voting codes to members — email via Mailgun, SMS via Scale to Win.

Reads the distribution manifest that tools/generate_codes.py produced
(member_id, poll_id, chapter, channel, destination, vote_link) and sends each
member their OWN one-tap ballot link over their best channel. Resumable and
idempotent: a per-run sent-log records who's been delivered, so re-running only
sends the remainder.

    # dry run — no network, just what WOULD be sent, by channel
    python3 tools/send_codes.py --manifest codes_out/distribution_manifest.csv --dry-run

    # real send (needs MAILGUN_* / STW_* env — see tools/senders.py)
    python3 tools/send_codes.py --manifest codes_out/distribution_manifest.csv --email --sms

    # offline self-test (fake transport, synthetic data — CI-safe)
    python3 tools/send_codes.py --self-test

Flags:
    --email / --sms   send only that channel (default: both)
    --limit N         cap sends (testing)
    --batch N         Mailgun batch size (default 1000, max 1000)
    --out-dir DIR     where sent-log + STW export + errors are written
    --force           ignore the sent-log (re-send everyone)

PII SAFETY: the manifest, sent-log, STW export, and error file all contain
member contacts/links and live ONLY on local disk. This tool prints AGGREGATE
COUNTS ONLY — never a destination, link, or message body. Do not cat/print
those files into a terminal or share them.
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import senders  # noqa: E402  (repo-root module, shared with vote_service)

SENT_LOG = "sent_log.csv"          # member_id, channel, status  (SENSITIVE-ish)
STW_EXPORT = "stw_campaign_upload.csv"  # phone, name, ballot_link  (SENSITIVE)
ERRORS = "send_errors.csv"         # channel, status, detail (may hold API text)


def _read_manifest(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_sent(path):
    done = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") in ("sent", "exported"):
                    done.add((row["member_id"], row["channel"]))
    return done


class _SentLog:
    def __init__(self, path):
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if self.f.tell() == 0:
            self.w.writerow(["member_id", "channel", "status", "ts"])

    def record(self, member_id, channel, status, ts):
        self.w.writerow([member_id, channel, status, ts])

    def close(self):
        self.f.close()


def run(manifest_rows, out_dir, do_email, do_sms, limit, batch_size,
        force, dry_run, mail=None, sms=None, clock=None, suppressed=None):
    """Core pipeline. Returns a counts dict. `mail`/`sms` injectable for tests;
    `clock` supplies timestamps (tests pass a fixed one — no wall-clock in CI)."""
    os.makedirs(out_dir, exist_ok=True)
    ts = clock() if clock else int(time.time())
    sent_path = os.path.join(out_dir, SENT_LOG)
    done = set() if force else _load_sent(sent_path)
    log = None if dry_run else _SentLog(sent_path)
    errf = None
    suppressed = {s.strip().lower() for s in (suppressed or set())}
    counts = {"email_sent": 0, "email_fail": 0, "sms_sent": 0, "sms_fail": 0,
              "sms_exported": 0, "skipped_done": 0, "skipped_postcard": 0,
              "no_destination": 0, "skipped_suppressed": 0}

    def err(channel, status, detail):
        nonlocal errf
        if dry_run:
            return
        if errf is None:
            errf = open(os.path.join(out_dir, ERRORS), "a", newline="", encoding="utf-8")
            csv.writer(errf).writerow(["channel", "status", "detail", "ts"])
        csv.writer(errf).writerow([channel, status, detail, ts])

    # ---- partition rows ----
    email_rows, sms_rows = [], []
    n = 0
    for r in manifest_rows:
        if limit and n >= limit:
            break
        ch = (r.get("channel") or "").lower()
        if ch == "postcard":
            counts["skipped_postcard"] += 1
            continue
        if not r.get("destination") or not r.get("vote_link"):
            counts["no_destination"] += 1
            continue
        if (r.get("destination") or "").strip().lower() in suppressed:
            counts["skipped_suppressed"] += 1     # member opted this contact out
            continue
        if ch == "email" and do_email:
            if (r["member_id"], "email") in done:
                counts["skipped_done"] += 1
            else:
                email_rows.append(r); n += 1
        elif ch == "sms" and do_sms:
            if (r["member_id"], "sms") in done:
                counts["skipped_done"] += 1
            else:
                sms_rows.append(r); n += 1

    # ---- EMAIL (Mailgun batch) ----
    if email_rows:
        mail = mail or senders.MailgunSender()
        if not dry_run and not mail.configured():
            print("ERROR: email requested but MAILGUN_DOMAIN/MAILGUN_API_KEY unset.",
                  file=sys.stderr)
            return counts
        for i in range(0, len(email_rows), batch_size):
            chunk = email_rows[i:i + batch_size]
            recips = [{"email": r["destination"], "link": r["vote_link"]} for r in chunk]
            res = senders.Result(True, 0, count=len(recips)) if dry_run \
                else mail.send_batch(recips)
            if res.ok:
                counts["email_sent"] += len(chunk)
                for r in chunk:
                    if log:
                        log.record(r["member_id"], "email", "sent", ts)
            else:
                counts["email_fail"] += len(chunk)
                err("email", res.status, res.detail)

    # ---- SMS (Scale to Win: API mode or EXPORT mode) ----
    if sms_rows:
        sms = sms or senders.sms_sender()   # Twilio > Scale to Win API > export
        if sms.configured():
            for r in sms_rows:
                if dry_run:
                    counts["sms_sent"] += 1
                    continue
                res = sms.send_one(r["destination"], r["vote_link"])
                if res.ok:
                    counts["sms_sent"] += 1
                    if log:
                        log.record(r["member_id"], "sms", "sent", ts)
                else:
                    counts["sms_fail"] += 1
                    err("sms", res.status, res.detail)
        else:
            # EXPORT mode: write a Scale to Win campaign-upload CSV (phone +
            # personalized ballot link) for broadcast-texting upload.
            if not dry_run:
                exp = os.path.join(out_dir, STW_EXPORT)
                new = not os.path.exists(exp)
                with open(exp, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["phone", "ballot_link", "message"])
                    for r in sms_rows:
                        w.writerow([r["destination"], r["vote_link"],
                                    sms.body_for(r["vote_link"])])
                        if log:
                            log.record(r["member_id"], "sms", "exported", ts)
            counts["sms_exported"] += len(sms_rows)

    if log:
        log.close()
    if errf:
        errf.close()
    return counts


def retry_failed(manifest_rows, contact_index_path, out_dir, dry_run,
                 mail=None, sms=None, failed=None, clock=None):
    """Resilience pass: find members whose EMAIL hard-failed (Mailgun events),
    and re-send their link over their ALTERNATE channel (SMS) — the
    'resend via a second channel/provider' pattern. Needs the private contact
    index (member_id -> contacts) from generate_codes to find their phone.
    Returns a counts dict."""
    os.makedirs(out_dir, exist_ok=True)
    ts = clock() if clock else int(__import__("time").time())
    mail = mail or senders.MailgunSender()
    sms = sms or senders.sms_sender()
    # member_id -> phones (from the private contact index)
    phones = {}
    if contact_index_path and os.path.exists(contact_index_path):
        with open(contact_index_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cct = (row.get("contact") or "").strip()
                if cct and "@" not in cct:
                    phones.setdefault(row.get("member_id"), []).append(cct)
    failed = failed if failed is not None else mail.failed_recipients()
    failed = {e.lower() for e in failed}
    c = {"failed_email": len(failed), "resent_sms": 0, "no_alt_channel": 0, "sms_fail": 0}
    log = None if dry_run else _SentLog(os.path.join(out_dir, SENT_LOG))
    for r in manifest_rows:
        if (r.get("channel") or "").lower() != "email":
            continue
        if (r.get("destination") or "").lower() not in failed:
            continue
        ph = phones.get(r.get("member_id"))
        if not ph:
            c["no_alt_channel"] += 1
            continue
        if dry_run:
            c["resent_sms"] += 1
            continue
        res = sms.send_one(ph[0], r["vote_link"]) if sms.configured() else None
        if res and res.ok:
            c["resent_sms"] += 1
            if log:
                log.record(r["member_id"], "sms_retry", "sent", ts)
        else:
            c["sms_fail"] += 1
    if log:
        log.close()
    return c


def _self_test():
    """Offline end-to-end: fake transports, synthetic (non-PII) rows, temp dir."""
    import tempfile

    class FakeResp:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    class FakeHTTP:
        def __init__(self, code=200):
            self.code = code
            self.calls = []

        def post(self, url, **kw):
            self.calls.append((url, kw))
            return FakeResp(self.code)

    rows = [
        {"member_id": "M1", "poll_id": "p", "chapter": "x", "channel": "email",
         "destination": "a@example.org", "vote_link": "https://v/p/p/v/CODE1"},
        {"member_id": "M2", "poll_id": "p", "chapter": "x", "channel": "email",
         "destination": "b@example.org", "vote_link": "https://v/p/p/v/CODE2"},
        {"member_id": "M3", "poll_id": "p", "chapter": "x", "channel": "sms",
         "destination": "+15550000003", "vote_link": "https://v/p/p/v/CODE3"},
        {"member_id": "M4", "poll_id": "p", "chapter": "x", "channel": "postcard",
         "destination": "", "vote_link": "https://v/p/p/v/CODE4"},
    ]
    d = tempfile.mkdtemp()
    okc = 0

    def check(cond, label):
        nonlocal okc
        assert cond, f"FAIL: {label}"
        okc += 1

    # 1. dry-run: no network, correct partition (default STW = export mode)
    c = run(rows, d + "/dry", True, True, 0, 1000, False, True, clock=lambda: 1)
    check(c["email_sent"] == 2 and c["sms_exported"] == 1 and c["skipped_postcard"] == 1,
          "dry-run partitions email/sms/postcard (unconfigured STW -> export)")

    # 2. real send via fake transports (API SMS mode)
    hm, hs = FakeHTTP(200), FakeHTTP(200)
    mail = senders.MailgunSender(domain="d", api_key="k", http=hm)
    sms = senders.ScaleToWinSender(api_url="https://stw/send", api_key="k", http=hs)
    c = run(rows, d + "/live", True, True, 0, 1000, False, False,
            mail=mail, sms=sms, clock=lambda: 2)
    check(c["email_sent"] == 2 and c["sms_sent"] == 1, "sends via fake transports")
    check(len(hm.calls) == 1, "email is ONE batched Mailgun call for 2 recipients")
    check(len(hs.calls) == 1, "sms is one STW API call")

    # 3. idempotency: re-run skips everyone
    c2 = run(rows, d + "/live", True, True, 0, 1000, False, False,
             mail=mail, sms=sms, clock=lambda: 3)
    check(c2["skipped_done"] == 3 and c2["email_sent"] == 0 and c2["sms_sent"] == 0,
          "re-run is idempotent (sent-log skips delivered members)")

    # 4. STW export mode when API not configured
    sms_exp = senders.ScaleToWinSender(api_url="", api_key="")
    c3 = run(rows, d + "/exp", False, True, 0, 1000, False, False,
             mail=None, sms=sms_exp, clock=lambda: 4)
    check(c3["sms_exported"] == 1 and os.path.exists(d + "/exp/" + STW_EXPORT),
          "SMS export mode writes a Scale to Win campaign CSV")

    # 5. Mailgun failure is counted, not fatal
    mail_fail = senders.MailgunSender(domain="d", api_key="k", http=FakeHTTP(401))
    c4 = run(rows[:2], d + "/fail", True, False, 0, 1000, False, False,
             mail=mail_fail, clock=lambda: 5)
    check(c4["email_fail"] == 2 and c4["email_sent"] == 0, "email failure counted")

    # 6. retry-failed: a hard-failed email member is re-sent over SMS
    ci = d + "/ci.csv"
    with open(ci, "w", encoding="utf-8") as f:
        f.write("contact,member_id,poll_id,code_hash\n")
        f.write("a@example.org,M1,p,h1\n+15550000001,M1,p,h1\n")  # M1 has a phone too
    hs = FakeHTTP(200)
    stw = senders.ScaleToWinSender(api_url="https://stw", api_key="k", http=hs)
    cr = retry_failed(rows, ci, d + "/retry", False, sms=stw,
                      failed={"a@example.org"}, clock=lambda: 6)
    check(cr["failed_email"] == 1 and cr["resent_sms"] == 1 and len(hs.calls) == 1,
          "retry-failed re-sends a hard-failed email member over SMS")

    print(f"SEND SELF-TEST: all {okc} checks passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest")
    ap.add_argument("--out-dir", default="codes_out")
    ap.add_argument("--email", action="store_true")
    ap.add_argument("--sms", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-send members whose email hard-failed (Mailgun events) "
                         "over their alternate channel (SMS)")
    ap.add_argument("--contact-index", help="contact_index_private.csv (for --retry-failed)")
    ap.add_argument("--suppress-file", help="one contact per line to skip (opt-outs)")
    a = ap.parse_args()
    if a.self_test:
        _self_test()
        return
    if not a.manifest:
        sys.exit("--manifest is required (or use --self-test)")
    if a.retry_failed:
        rows = _read_manifest(a.manifest)
        c = retry_failed(rows, a.contact_index, a.out_dir, a.dry_run)
        print(("DRY RUN — " if a.dry_run else "") + "failure-retry summary (counts only):")
        for k in ("failed_email", "resent_sms", "no_alt_channel", "sms_fail"):
            print(f"  {k}: {c[k]}")
        return
    do_email = a.email or not (a.email or a.sms)
    do_sms = a.sms or not (a.email or a.sms)
    rows = _read_manifest(a.manifest)
    suppressed = set()
    if a.suppress_file and os.path.exists(a.suppress_file):
        with open(a.suppress_file, encoding="utf-8") as f:
            suppressed = {ln.strip() for ln in f if ln.strip()}
    counts = run(rows, a.out_dir, do_email, do_sms, a.limit,
                 min(a.batch, senders.MailgunSender.MAX_BATCH),
                 a.force, a.dry_run, suppressed=suppressed)
    print(("DRY RUN — " if a.dry_run else "") + "delivery summary (counts only):")
    for k in ("email_sent", "email_fail", "sms_sent", "sms_fail", "sms_exported",
              "skipped_done", "skipped_postcard", "skipped_suppressed", "no_destination"):
        print(f"  {k}: {counts[k]}")
    if counts["sms_exported"]:
        print(f"  -> upload {os.path.join(a.out_dir, STW_EXPORT)} to a Scale to Win "
              "broadcast campaign (personalized 'ballot_link' column).")
    if counts["email_fail"] or counts["sms_fail"]:
        print(f"  -> see {os.path.join(a.out_dir, ERRORS)} for failures; re-run to "
              "retry (delivered members are skipped).")


if __name__ == "__main__":
    main()
