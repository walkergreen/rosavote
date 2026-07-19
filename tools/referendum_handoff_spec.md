# DSA Referendum Voting System — Handoff Spec

A build spec for a code-authenticated, verifiable, multi-chapter membership
referendum system on Google Cloud. Paste this into a new chat to continue the
work. It covers the architecture, the decisions behind it, what's built, and
what's still open.

---

## Goal

Run a national membership referendum (~120k voters) — a universal Yes/No/Abstain
endorsement ballot — where each DSA chapter runs its own poll on its own
timeline, but everyone votes on identical questions. Priorities: low cost,
voter anonymity, verifiability, mobile accessibility, and resistance to abuse.
Chosen over OpaVote (cost at ~$9,600) and Helios (poor mobile/code fit).

## Stack

- **Cloud Run** (Python/Flask + gunicorn) — the vote service.
- **Firestore (Native mode)** — codes and ballots. Project `dsa-org-tools`,
  region `us-east1`.
- **BigQuery** — the membership roll (`proj-tmc-mem-dsa.main.clean_member_table`),
  a deduped primary-AKID table, and tabulation.
- **Solidarity Tech / Scale to Win** — SMS/email distribution (not built here;
  the app never sends messages itself).

---

## Core architecture

**Two Firestore collections per chapter poll, no join key between them** —
this is the anonymity guarantee:
- `{poll_id}__codes` — one doc per issued code, keyed by SHA-256 of the code.
  Holds `used: bool` and `member_id`/chapter for turnout accounting only.
- `{poll_id}__ballots` — one doc per ballot, keyed by its own content hash.
  Holds `receipt`, `choice`, `nonce`, `record_hash`. No code, no voter id, no
  precise timestamp.

**`poll_id` is chapter-scoped:** `debs_endorsement__<chapter>` (e.g. `__nyc`).
Same deployment serves all chapters; routing is `/p/<poll_id>/...`.

**The vote transaction touches ONE document** (the voter's own code doc):
check unused → mark used → write ballot as an independent, uniquely-keyed doc.
No shared hot document, so writes shard and scale horizontally (~thousands/sec).
Enforces one-code-one-vote atomically.

**No live hash chain.** The tamper-evidence hash chain is built AFTER the poll
closes, in a batch job (OpenSlides pattern). Building it live would have made
the chain head a single hot document and bottlenecked all writes. Post-close
batch avoids that and produces an identical, publishable chain.

**Ballot integrity bug fixed:** the verifier must RECOMPUTE `record_hash` from
ballot content (`receipt|choice|nonce`), not trust the stored hash column.
Trusting the stored column let a flipped `choice` pass verification. The
`nonce` is published so anyone can recompute. (This bug was caught by a
self-test during the build — keep the self-test.)

---

## Identity & code generation

- **Dedup on primary AKID**, not email or contact. One primary AKID = one
  member = one code. The deduped primary-AKID table merges members who have
  multiple AKIDs/emails.
- **Every member has a unique email (possibly several); members may share a
  phone.** So EMAIL is the reliable per-member identifier and primary send
  channel; SMS is fallback/reminder. Identity lives in the code, so two members
  sharing a phone each get a distinct code and each votes once.
- **Channel priority: email → SMS → mail.** Members with no email/phone but a
  physical address get a mailed postcard with the printed code + a QR encoding
  the one-tap link. Use a mail-friendly code format avoiding ambiguous
  characters (0/O, 1/l/I). Mail ships first (slow delivery); window stays open
  long enough to not disenfranchise.
- Generation is a one-time offline BigQuery job → writes hashed code docs to
  Firestore + a distribution manifest for the send platform + a private
  contact index (for resend). Only code HASHES are stored server-side;
  plaintext lives in the manifest (held by send platform / neutral committee).

## Delivery-failure recovery (the "can't find my code" problem)

- **Enumeration-safe, rate-limited resend:** the contact a user types is a
  LOOKUP KEY, never a destination. If it matches a member on the roll, resend
  that member's EXISTING code to their ON-RECORD contacts only. So an attacker
  can only ever trigger sends to real members already on the roll — no
  arbitrary-destination send, cost is bounded. Rate-limited per-contact,
  per-IP, plus a global circuit breaker. Response is identical whether or not
  the contact matched (anti-enumeration).
- **Mail voters are NOT in resend** (no email/phone to look up) — their
  fallback is Zendesk/email staff support.
- **Provisional path** for people who believe they're eligible but got no code:
  a SEALED ballot form (name/email/chapter), stored separately, NOT counted
  until staff verify membership. It's a form submission, not an on-demand send
  — so no OTP cost-abuse surface. This deliberately replaced an earlier
  self-service email/phone→OTP flow, which had an unbounded-send cost hole
  (a leaked link could be scripted to run up OTP costs). Assume the voting link
  WILL leak (members forward texts); rely on the code + rate limits, never on
  link secrecy.

---

## Abuse / scale / cost

- The vote app **sends nothing on demand**, so a leaked link can't drive
  messaging cost. Worst case from a flood is temporary unavailability.
- Hardening for production: Cloud Armor per-IP rate limiting, `max-instances`
  cap (converts a flood into slowness, not an unbounded bill), `min-instances`
  warm (no cold-start into a spike), billing alerts. Reject malformed codes
  cheaply before any Firestore access.
- **Wave rollout** smooths load: random cohorts (e.g. `MOD(FARM_FINGERPRINT
  (code_hash), N)`), staggered sends ~2h apart, COMMON close time for all
  cohorts (fairness). Email waves are ~free so you can wave purely for load.
- **No Ticketmaster-style queue needed** — after the hot-doc removal there's no
  contention to queue against; wave rollout controls arrival rate instead.
- **Realistic capacity:** backend handles far more than the election produces
  (~65 votes/sec even in a compressed scenario vs. thousands/sec capacity).
- **Cost:** infra is single-digit dollars (Firestore ops ~$1, Cloud Run a few
  $). Real spend is SMS. Email-primary makes the base send ~free; SMS on a
  subset + reminders dominates. Rough total range ~$400 (email-heavy, one light
  SMS round) to ~$12k (SMS-heavy, two rounds); realistic middle ~$1–5k. Biggest
  lever is the per-segment SMS rate from the send platform; second is number of
  reminder rounds.

---

## Verification / tabulation

- **Post-close pipeline per chapter:** `build_chain.py` emits `ballots.csv`,
  `used_codes.csv`, `chain_head.txt`; `make_blt.py` emits a BLT file for
  independent tabulation (OpaVote/OpenSTV). Publish these + `verify.py`.
- **What voters/observers can verify:** find their receipt in the published
  ballots (individual); recompute the chain to the anchored head (tamper-
  evidence); recompute the tally (universal); ballots == used codes (no
  stuffing); load the BLT into independent software (external reproduction).
- **Abstentions excluded from the Yes/No result** (may count toward quorum if
  rules require — report separately). `make_blt.py` drops abstentions from the
  contest.
- **Honest limit — cast-as-intended:** this is a trusted-server model with
  receipt-based individual verifiability (same tier as OpaVote). It does NOT
  cryptographically prove the server recorded what the voter selected; only
  Helios-style E2E (Benaloh challenge) does, which was traded away for mobile
  accessibility. The voter explainer states this honestly. The chain locks
  ballots at close; wide receipt-checking shrinks any undetected discrepancy.
- **Live tally:** admin-only, best done in Hex/BigQuery (auth + audit for free,
  no new attack surface), NOT a public endpoint. Keep public result sealed
  until close (avoid cross-chapter bandwagon effects on a shared question).

---

## Multi-chapter model

- One member belongs to one chapter → eligibility partitions cleanly, dedup is
  effectively national-then-partition, no cross-chapter double-vote path.
- Shared ballot definition in ONE place (question + choices); per-chapter config
  (name, `opens_at`, `closes_at`) separate. App enforces the open/close window
  per chapter before the vote transaction.
- Each chapter closes on its own schedule → run the close-out pipeline scoped to
  its `poll_id` → deliver that chapter's result package to that chapter's admin.
  National rollup by unioning chapter ballot collections in BigQuery, with each
  chapter's independently-published set as source of truth.

---

## Accessibility (WCAG 2.1 AA)

The branded ballot was audited and fixed to clear AA on the failing criteria:
- 1.4.4 — removed viewport zoom lock.
- 4.1.3 — error regions `role="alert"`; receipt is `aria-live`/`role="status"`.
- 2.1.1 — radio group (Yes/No/Abstain) has full arrow-key navigation + roving
  tabindex + Space/Enter select.
- 2.4.3 — focus moves to each new screen's heading on transition.
- 1.4.3 — darkened two small grey tokens that measured below 4.5:1.
Still required before sign-off: a REAL screen-reader run (VoiceOver + TalkBack/
NVDA) per the test script; confirm `:focus-visible` outlines aren't suppressed;
check the provisional form's tab order.

---

## Files built this session

- `vote_service_multichapter.py` — Flask app: `/p/<poll_id>/` routing, code
  vote, provisional, per-chapter open/close, serves the branded template.
- `ballot_template.html` — branded, code-gated, accessible ballot with injection
  points (`__POLL_ID__`, `__CHAPTER_NAME__`, `__CODE__`).
- `Dockerfile.multichapter` + `requirements.multichapter.txt` — deploy config
  (serves the Flask app WITH the template; not a static server).
- `generate_codes.py` — code generation + distribution manifest + contact index
  (dedup on member id/AKID, best-channel selection). Wire the BigQuery query to
  the real primary-AKID schema.
- `resend.py` — enumeration-safe, rate-limited resend endpoint.
- `build_chain.py` — post-close chain builder + published artifacts.
- `verify.py` — public verifier (recomputes record_hash + chain + tally).
- `make_blt.py` — BLT export for external tabulation (abstentions excluded).
- `load_test.py` — per-wave load test (distinct codes; +dupe-check for
  atomicity).
- `log_leak_inspection.py` — scans Cloud Run logs for leaked codes/PII.
- `overload.html` — voter-friendly 503/overload page.
- `verify_your_vote.md` — plain-language voter verification explainer (honest
  about the trust model).
- `accessibility_test_and_conformance.md` — SR test script + WCAG conformance
  note.

## Open / next steps

1. Load the `CHAPTERS` registry from a BigQuery chapter table (adding a chapter
   = data change, not code change). Currently hardcoded (nyc/chi/staging).
2. Wire `generate_codes.py`'s BigQuery query to the real primary-AKID + roll
   schema; run it to produce codes + manifest per chapter.
3. Build the per-chapter close-out automation (Cloud Scheduler → scoped
   build_chain + make_blt + tally + deliver to chapter admin) with a
   "publish nationally only after all chapters close" guard.
4. Staff adjudication view for provisional ballots (verify → counts, reject →
   stays sealed) — a Hex table or small admin surface.
5. Mail tier: address-only detection + print/mail merge manifest with QR link.
6. Production hardening: re-enable auth (staging is `--allow-unauthenticated`),
   Cloud Armor, min/max-instances, billing alerts.
7. Real screen-reader accessibility pass, captured for the election record.
8. Resolve the `wgreen@dsausa.org` "Gaia id not found" org-access issue with a
   GCP org admin (blocks authenticated identity; harmless only while public).

## Known environment gotchas

- Cloud Run URLs: use the exact "Service URL" from deploy output; the app has
  NO bare `/` route (routing is `/p/<poll_id>/`), so test that path.
- `--no-allow-unauthenticated` returns Google's branded 403/404 at the edge for
  unauthenticated requests — that's the auth gate, not an app bug.
- Deploying to an existing service name UPDATES it (new revision); "already
  exists" conflicts clear on a straight re-run.
- Seeding/verifying from Cloud Shell needs `pip3 install google-cloud-firestore`
  and `firestore.Client(project="dsa-org-tools")`.
