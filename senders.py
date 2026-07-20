#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Delivery channels for voting codes — Mailgun (email) and Scale to Win (SMS).

Design + safety:
  * PII-safe by construction. Senders take (destination, link, name) and return
    a Result. Callers MUST NOT log destinations, links, or bodies — only
    aggregate counts and status codes. Error bodies are truncated + returned to
    the caller to write to a file, never printed.
  * Injectable transport (`http=`) so the pipeline is testable offline with a
    fake — no network, no real keys, no PII in tests.
  * Idempotency + batching live in the orchestrator (send_codes.py); senders
    just deliver.

Env:
  Mailgun:   MAILGUN_DOMAIN, MAILGUN_API_KEY, MAILGUN_FROM?, MAILGUN_API_BASE?
             (EU region: https://api.eu.mailgun.net/v3)
  SMS: sms_sender() picks by config, best-for-one-off first:
       Twilio (transactional):  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and either
                                TWILIO_FROM or TWILIO_MESSAGING_SERVICE_SID.
       Scale to Win API:        STW_API_URL, STW_API_KEY (per-recipient POST).
       Scale to Win EXPORT:     neither set — the orchestrator writes a campaign-
                                upload CSV (STW's broadcast-texting workflow).
    Use Twilio for one-off "resend my code" texts; Scale to Win for a bulk
    broadcast if DSA's numbers are already registered there.
"""

import json
import os


class Result:
    def __init__(self, ok, status, detail="", count=0):
        self.ok, self.status, self.detail, self.count = ok, status, detail, count

    def __repr__(self):
        return f"Result(ok={self.ok}, status={self.status}, count={self.count})"


def _requests():
    import requests  # bundles certifi; only imported when a real send happens
    return requests


# ---- message templates ----------------------------------------------------
# Message wording. DSA staff can edit templates/ballot_messages.json (no code
# change) — see that file's _README key. %recipient.link% / %recipient.name% are
# Mailgun recipient-variable tokens; the SMS body uses {link}/{name} str.format
# tokens. PRECEDENCE (low -> high): built-in default < the JSON file <
# environment variable. So a chapter edits the JSON, and an env var can still
# override for a one-off send.
_DEFAULTS = {
    "email_subject": "Your DSA ballot is ready — vote now",
    "email_text": (
        "Hello,\n\n"
        "Your ballot for the DSA election is ready. Vote securely here — this link "
        "is unique to you, do not share it:\n\n"
        "%recipient.link%\n\n"
        "Voting is quick and your ballot is confidential. If you have questions, "
        "contact your chapter's elections committee.\n\n"
        "In solidarity,\nDSA Elections\n"),
    "email_html": (
        "<p>Hello,</p><p>Your ballot for the DSA election is ready. "
        "<a href=\"%recipient.link%\">Vote securely here</a> — this link is unique to "
        "you, please don't share it.</p><p>Voting is quick and your ballot is "
        "confidential. Questions? Contact your chapter's elections committee.</p>"
        "<p>In solidarity,<br>DSA Elections</p>"),
    "sms_body": (
        "DSA Elections: your ballot is ready. Vote here (unique to you, don't share): "
        "{link}  Reply STOP to opt out."),
}
_ENV_KEYS = {
    "email_subject": "BALLOT_SUBJECT", "email_text": "BALLOT_EMAIL_TEXT",
    "email_html": "BALLOT_EMAIL_HTML", "sms_body": "BALLOT_SMS_BODY",
}


def load_templates(path=None):
    """Built-in defaults, overlaid by the JSON template file (if present),
    overlaid by any env vars. Never raises — a malformed file falls back."""
    t = dict(_DEFAULTS)
    path = path or os.environ.get(
        "BALLOT_TEMPLATES",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "templates", "ballot_messages.json"))
    try:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for k, v in (json.load(f) or {}).items():
                    if k in t and isinstance(v, str):
                        t[k] = v
    except Exception:
        pass  # malformed file: keep defaults rather than fail a send
    for k, env in _ENV_KEYS.items():
        if os.environ.get(env):
            t[k] = os.environ[env]
    return t


_T = load_templates()
EMAIL_SUBJECT = _T["email_subject"]
EMAIL_TEXT = _T["email_text"]
EMAIL_HTML = _T["email_html"]
SMS_BODY = _T["sms_body"]


class MailgunSender:
    """Batch email via Mailgun. One API call personalizes up to 1000 recipients
    with recipient-variables, so 120k members is ~120 calls, not 120k."""
    MAX_BATCH = 1000

    def __init__(self, domain=None, api_key=None, sender=None, subject=None,
                 base=None, http=None):
        self.domain = domain or os.environ.get("MAILGUN_DOMAIN", "")
        self.api_key = api_key or os.environ.get("MAILGUN_API_KEY", "")
        self.sender = sender or os.environ.get(
            "MAILGUN_FROM", f"DSA Elections <ballots@{self.domain}>")
        self.subject = subject or EMAIL_SUBJECT
        self.base = base or os.environ.get("MAILGUN_API_BASE",
                                           "https://api.mailgun.net/v3")
        self._http = http  # injectable for tests

    def configured(self):
        return bool(self.domain and self.api_key)

    def send_batch(self, recipients, tag="ballot"):
        """recipients: [{email, link, name?}] (<= MAX_BATCH). Returns a Result."""
        if not recipients:
            return Result(True, 0, count=0)
        if len(recipients) > self.MAX_BATCH:
            return Result(False, 0, f"batch >{self.MAX_BATCH}", 0)
        to = [r["email"] for r in recipients]
        rvars = {r["email"]: {"link": r["link"], "name": r.get("name", "")}
                 for r in recipients}
        data = {
            "from": self.sender, "to": to, "subject": self.subject,
            "text": EMAIL_TEXT, "html": EMAIL_HTML,
            "o:tag": tag, "o:tracking": "no",
            "recipient-variables": json.dumps(rvars),
        }
        http = self._http or _requests()
        try:
            resp = http.post(f"{self.base}/{self.domain}/messages",
                             auth=("api", self.api_key), data=data, timeout=60)
        except Exception as e:  # network/DNS/etc.
            return Result(False, -1, f"{type(e).__name__}", 0)
        ok = 200 <= resp.status_code < 300
        return Result(ok, resp.status_code,
                      "" if ok else (resp.text or "")[:200], len(to) if ok else 0)

    def failed_recipients(self, tag="ballot", event="failed", limit=300):
        """Poll Mailgun's Events API for recipients whose email FAILED (hard
        bounce / permanent failure) — the reliable, provider-reported signal
        for 'this address didn't get it.' Returns a set of lowercased emails.
        (Mailgun can't report soft spam-foldering; hard failures are what you
        can act on — re-send those over another channel.)"""
        http = self._http or _requests()
        out, url = set(), f"{self.base}/{self.domain}/events"
        params = {"event": event, "limit": min(limit, 300)}
        if tag:
            params["tags"] = tag
        try:
            for _ in range(20):   # follow pagination up to ~6k events
                resp = http.get(url, auth=("api", self.api_key), params=params, timeout=60)
                if not (200 <= resp.status_code < 300):
                    break
                data = resp.json() if hasattr(resp, "json") else json.loads(resp.text)
                items = (data.get("items") or [])
                for it in items:
                    addr = ((it.get("recipient") or "")).lower()
                    if addr:
                        out.add(addr)
                nxt = (((data.get("paging") or {}).get("next")) or "")
                if not items or not nxt:
                    break
                url, params = nxt, {}
        except Exception:
            pass
        return out


class ScaleToWinSender:
    """SMS via Scale to Win. Two modes:
      * API mode (STW_API_URL set): POST {to, body} per recipient.
      * EXPORT mode (unset): send_one raises NotConfigured — the orchestrator
        writes a Scale to Win campaign-upload CSV instead (the normal STW
        broadcast-texting flow)."""

    class NotConfigured(Exception):
        pass

    def __init__(self, api_url=None, api_key=None, body_tmpl=None, http=None):
        self.api_url = api_url or os.environ.get("STW_API_URL", "")
        self.api_key = api_key or os.environ.get("STW_API_KEY", "")
        self.body_tmpl = body_tmpl or SMS_BODY
        self._http = http

    def configured(self):
        return bool(self.api_url)

    def body_for(self, link, name=""):
        return self.body_tmpl.format(link=link, name=name)

    def send_one(self, phone, link, name=""):
        if not self.api_url:
            raise ScaleToWinSender.NotConfigured()
        http = self._http or _requests()
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        payload = {"to": phone, "body": self.body_for(link, name)}
        try:
            resp = http.post(self.api_url, headers=headers,
                             data=json.dumps(payload), timeout=30)
        except Exception as e:
            return Result(False, -1, f"{type(e).__name__}", 0)
        ok = 200 <= resp.status_code < 300
        return Result(ok, resp.status_code,
                      "" if ok else (resp.text or "")[:200], 1 if ok else 0)


class TwilioSender:
    """Transactional per-message SMS via Twilio — the right tool for one-off
    'resend my code' texts (Scale to Win is a broadcast/campaign tool, not a
    per-message API). Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and either
    TWILIO_FROM (a sending number) or TWILIO_MESSAGING_SERVICE_SID."""

    def __init__(self, account_sid=None, auth_token=None, from_=None,
                 messaging_service_sid=None, body_tmpl=None, base=None, http=None):
        self.sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_ = from_ or os.environ.get("TWILIO_FROM", "")
        self.msid = messaging_service_sid or os.environ.get(
            "TWILIO_MESSAGING_SERVICE_SID", "")
        self.body_tmpl = body_tmpl or SMS_BODY
        self.base = base or os.environ.get(
            "TWILIO_API_BASE", "https://api.twilio.com/2010-04-01")
        self._http = http

    def configured(self):
        return bool(self.sid and self.token and (self.from_ or self.msid))

    def body_for(self, link, name=""):
        return self.body_tmpl.format(link=link, name=name)

    def send_one(self, phone, link, name=""):
        http = self._http or _requests()
        data = {"To": phone, "Body": self.body_for(link, name)}
        if self.msid:
            data["MessagingServiceSid"] = self.msid
        else:
            data["From"] = self.from_
        try:
            resp = http.post(f"{self.base}/Accounts/{self.sid}/Messages.json",
                             auth=(self.sid, self.token), data=data, timeout=30)
        except Exception as e:
            return Result(False, -1, f"{type(e).__name__}", 0)
        ok = 200 <= resp.status_code < 300     # Twilio returns 201 Created
        return Result(ok, resp.status_code,
                      "" if ok else (resp.text or "")[:200], 1 if ok else 0)


def sms_sender(http=None):
    """Pick the SMS provider by what's configured, best-for-one-off first:
    Twilio (transactional) > Scale to Win API > Scale to Win export. Returns a
    sender with .configured()/.send_one(); ScaleToWinSender in export mode is
    the fallback the orchestrator writes a campaign CSV for."""
    tw = TwilioSender(http=http)
    if tw.configured():
        return tw
    return ScaleToWinSender(http=http)
