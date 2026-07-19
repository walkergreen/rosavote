"""
Log-leak inspection for the referendum vote service.

Flaw being tested: even though ballots carry no code and no precise timestamp
in Firestore, the linkage can REAPPEAR in logs. If a voting code ever lands in
a Cloud Run request log line (URL path, query string, or a stray print), then
code + timestamp + IP sit together in Logging — reconstructing exactly the
voter<->vote correlation the schema was designed to prevent.

This script scans your Cloud Run logs for leaked codes and PII patterns and
reports any hits. Run it against STAGING after a load test, and again against
production shortly after go-live, so you catch a leak while it's still cheap
to fix (redact + rotate) rather than after the election.

It checks for the three most common leak vectors:
  1. Codes in the URL path/query      (e.g. GET /v/<code> or ?code=<code>)
  2. Codes echoed in log text          (accidental logging.info(f"...{code}"))
  3. Raw PII (emails / phone numbers)   appearing anywhere in log payloads

Requires: pip install google-cloud-logging
Auth: run as an identity with roles/logging.viewer on the project.
"""

import argparse
import re
import sys
from collections import Counter

try:
    from google.cloud import logging as gcloud_logging
except ImportError:
    gcloud_logging = None

# Same shape the vote service issues (URL-safe token, 12-64 chars). Tighten to
# your exact issued length to cut false positives.
CODE_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{12,64}\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# North-American-ish phone; adjust for your roll.
PHONE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")


def scan_text(text, known_codes=None):
    """Return a set of leak categories found in a single log line's text."""
    hits = set()
    if EMAIL.search(text):
        hits.add("email_in_log")
    if PHONE.search(text):
        hits.add("phone_in_log")
    if known_codes:
        # Highest-signal check: does an ACTUAL issued code appear verbatim?
        for tok in CODE_TOKEN.findall(text):
            if tok in known_codes:
                hits.add("issued_code_in_log")
                break
    return hits


def load_known_codes(path):
    if not path:
        return None
    with open(path) as f:
        return {ln.strip() for ln in f if ln.strip()}


def run(project, service, hours, known_codes_path, sample_only):
    if gcloud_logging is None:
        print("google-cloud-logging not installed. "
              "pip install google-cloud-logging --break-system-packages")
        sys.exit(1)

    known = load_known_codes(known_codes_path)
    client = gcloud_logging.Client(project=project)

    # Cloud Run request + application logs for the service, last N hours.
    flt = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service}" '
        f'timestamp>="-{hours}h"'
    )

    print(f"scanning project={project} service={service} window={hours}h")
    if known:
        print(f"loaded {len(known)} known codes for verbatim matching")
    else:
        print("no --known-codes provided: checking URL path/query + PII only "
              "(cannot confirm a token is a REAL code without the list)")

    cat = Counter()
    examples = {}
    n = 0

    for entry in client.list_entries(filter_=flt, page_size=1000):
        n += 1
        # Assemble the searchable text from the parts that commonly leak.
        parts = []
        payload = entry.payload
        if isinstance(payload, str):
            parts.append(payload)
        elif isinstance(payload, dict):
            parts.append(str(payload))
        http = getattr(entry, "http_request", None)
        request_url = None
        if http and isinstance(http, dict):
            request_url = http.get("requestUrl") or http.get("request_url")
        elif http:
            request_url = getattr(http, "request_url", None)
        if request_url:
            parts.append(request_url)
            # Explicit URL check: any code-shaped token in the path/query is a
            # design smell even before we confirm it's a real code.
            if "?" in request_url or re.search(r"/v/|/vote/", request_url):
                if CODE_TOKEN.search(request_url.split("?", 1)[-1]) or \
                   re.search(r"/v/[A-Za-z0-9_-]{12,}", request_url):
                    cat["code_shaped_token_in_url"] += 1
                    examples.setdefault("code_shaped_token_in_url", request_url[:200])

        text = " ".join(parts)
        for h in scan_text(text, known):
            cat[h] += 1
            examples.setdefault(h, text[:200])

        if sample_only and n >= sample_only:
            break

    print(f"\nscanned {n} log entries\n")
    if not cat:
        print("PASS: no leaked codes, emails, or phone numbers found.")
        return

    print("FINDINGS (investigate each):")
    for k, v in cat.most_common():
        print(f"  [{v:>6}]  {k}")
        print(f"           e.g. {examples.get(k, '')!r}")
    print("\nremediation if anything fired:")
    print("  - move the code OUT of the URL: accept it in the POST body, not")
    print("    the path/query, so it never reaches request logs.")
    print("  - add a Logging exclusion / redaction on the vote path.")
    print("  - remove any application logging that echoes code/email/phone.")
    print("  - if REAL codes leaked to prod logs: treat as compromised, redact")
    print("    the sink, and rotate affected codes before they're used.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--service", default="referendum")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--known-codes", help="file of issued codes for verbatim match")
    ap.add_argument("--sample", type=int, default=0,
                    help="stop after N entries (quick spot-check)")
    args = ap.parse_args()
    run(args.project, args.service, args.hours, args.known_codes, args.sample or None)


if __name__ == "__main__":
    main()
