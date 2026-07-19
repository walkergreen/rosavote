# DSA Chapter Member Ballot (ballot-v2)

Code-authenticated, multi-chapter membership ballot for ~120k voters.
Flask + Firestore on Cloud Run (project `dsa-org-tools`, region `us-east1`,
service `member-ballot-v2`). Built by DSA Staff's Data & Tech Department.
Prototype status: deployed publicly for review; NOT a live election.

## Run / deploy / test

```sh
# deploy (from this directory's parent):
gcloud run deploy member-ballot-v2 --source ballot-v2 --region us-east1 \
  --allow-unauthenticated [--set-env-vars ADMIN_TOKEN=<hex>]

# full offline smoke test (no GCP credentials needed — stubs Firestore):
python3 tools/smoke_test.py       # use .venv/bin/python if flask isn't global

# local clickable dev server, stub Firestore, admin token "dev":
python3 tools/dev_server.py       # http://localhost:8080

# syntax-check the template's inline JS after editing it:
python3 -c "import re; open('/tmp/t.js','w').write(re.search(r'<script>(.*)</script>', open('ballot_template.html').read(), re.S).group(1).replace('__Q7_NAMES__','{}'))" && node --check /tmp/t.js
# (this machine has no node — loading the page in a browser and checking the
# console for errors is the fallback; same applies to admin_console.html)

# seed Firestore config / mint scoped admin tokens (needs GCP creds):
python3 tools/seed_config.py polls
python3 tools/seed_config.py admin --name "NYC admins" --role chapter --polls debs_endorsement__nyc
```

There is no build step. The Dockerfile serves `vote_service.py` + template via
gunicorn. Firestore Native mode already exists in `dsa-org-tools`.

## Files

- `vote_service.py` — the whole backend: routing (`/p/<poll_id>/...`), the
  branded splash at `/` (test codes + a 13-expander admin explainer),
  answer validation, the vote transaction, provisional ballots, and the whole
  admin surface: scoped auth (`require_admin`), election builder w/ Art. V
  validation (`/admin/api/polls*`), provisional adjudication queue
  (verify=promote+burn codes / reject), close-out, void-and-reissue
  (`POST /p/<poll_id>/admin/void`). Poll config comes from Firestore
  `config__polls` via `load_polls()` (60s cache); the in-code `CHAPTERS` dict
  is only the fallback seed until `tools/seed_config.py polls` has run.
- `admin_console.html` — single-file branded console served at `/admin/`
  (token sign-in, polls list, builder, adjudication, void). Static shell;
  every API it calls is token-gated.
- `ballot_template.html` — single-file branded ballot (all CSS/JS inline).
  Server injects: `__POLL_ID__ __CHAPTER_NAME__ __CODE__ __Q6_QUESTION__
  __Q8_QUESTION__ __Q7_OPTIONS__ __Q7_NOTE__ __Q7_SEATS__ __Q7_ALTS__
  __Q7_NAMES__ __HELP_SUBJECT__`.
- `tools/` — `generate_codes.py` (roll → hashed codes + delivery manifest,
  wired to `proj-tmc-mem-dsa.main.ak_primary_id` joined to
  `ak_user_fields_pivoted` for chapter; eligibility = is_primary + "Member in
  Good Standing"; fetches via the `bq` CLI so plain gcloud auth works —
  `--write` (Firestore code docs) still needs ADC; email-first channel
  ladder; PII only ever goes to output files, stdout is aggregate counts,
  tracebacks are diverted to a local log),
  `build_chain.py` (post-close tamper-evidence chain; includes voided ballots
  flagged), `make_blt.py` (BLT export; excludes voided; delegate rankings come
  from the separate secret collection), `verify.py`, `resend.py`,
  `load_test.py`, `log_leak_inspection.py`, `smoke_test.py`,
  `accessibility_results.md`, `verify_your_vote.md`,
  `referendum_handoff_spec.md` (the ORIGINAL spec — parts are now stale; this
  file supersedes it where they conflict).

## The ballot (8 questions, 3 sections, one page)

Section 1 "Chapter Poll" (red): Q1 Debs endorsement Y/N/A · Q2 campaign
structure (ranked STV) · Q3 the 1912 field (ranked STV) · Q4 pledges
(multi + exclusive Abstain) · Q5 free-text comment.
Section 2 "Convention Delegates" (black): delegate election, ranked STV.
Section 3 "Local Issues" (tan): two chapter-specific Y/N/A questions.

**CRITICAL KEY↔DISPLAY MAPPING** (keys are stable for tools; display moved):
`q1,q2,q3,pledges,text` = display Q1–Q5 · **`q7` = delegates = display Q6** ·
**`q6` = local issue 1 = display Q7** · **`q8` = local issue 2 = display Q8**.
Do not "fix" this by renaming keys — Firestore data and tools reference them.

## Data model (Firestore, per poll_id)

- `{poll}__codes` — key = SHA-256(code). `used`, `member_id`, `chapter`,
  optional `reissued_from`. Repeatable per-chapter TEST codes live in
  `CHAPTERS[..]["test_code"]`, handled in code, never stored, never recorded.
- `{poll}__ballots` — IDENTITY-LINKED (member_id, chapter, code_hash, comment)
  answers EXCEPT q7. Visible to admins + the voter's own chapter.
- `{poll}__delegate_ballots` — SECRET (Const. Art. V §5): q7 ranking, same
  receipt, code_hash only (admin troubleshooting trace). ADMIN-ONLY; chapters
  never get access. Both ballot docs carry receipt/nonce/record_hash and a
  `voided` flag (never delete; excluded from tallies, kept in chain).
- `{poll}__provisional` — sealed self-serve provisionals (name, all emails,
  phones, chapter, join date, alt names + full answers) pending adjudication.
- `{poll}__audit_log` — append-only admin actions (void_reissue,
  config_save, close_poll, provisional_verify/reject).
- `config__polls` — poll configs (source of truth once seeded; candidates
  stored as `[{id, name}]` because Firestore forbids nested arrays).
- `config__admins` — admin tokens, keyed by SHA-256(token): `{name, role:
  national|chapter, polls: [...], active}`. `ADMIN_TOKEN` env stays as the
  national break-glass; if unset AND no docs exist, admin surface is off.

## Decided policy (do not silently change)

- Visibility: Sections 1&3 recorded by name (admin + own chapter); delegate
  ranking secret; national does NOT publish chapter results — each chapter
  decides its own publication (never delegate rankings). Disclosed to voters
  above Q1.
- Votes final: no edit, no revote. Admin remedy = VOID-AND-REISSUE only
  (reasons whitelist: stolen_code, technical_failure,
  provisional_adjudication; open window only; audited; disclosed aggregate).
- Delegates: Scottish STV (SSI 2007/42), optional preferences (first choice
  required, never force full ranking), candidate order shuffled per page load,
  ranks beyond delegate seats display black (informational). Alternates via
  TWO-COUNT method: official count for seats; recount same ballots at
  seats+alternates; new winners = alternates in order of election.
  `make_blt.py` emits both counts.
- Art. V constraints: delegate elections ≥45 days & ≤4 months pre-convention,
  post-apportionment; eligibility = paid up at time of election. The combined
  page inherits the delegate window.
- Cost model on splash: SMS $0.0125/segment, email-first, two shrinking SMS
  reminder waves, postcards for no-email/no-phone tier. Realistic ~$2–5k.

## Gotchas

- Contest registries in `tools/make_blt.py` are overlaid from `config__polls`
  on `--from-firestore` runs; the built-ins are only an offline-CSV fallback
  (sync gotcha now limited to that path).
- Poll config edits force-reload only the serving instance's cache; other
  Cloud Run instances converge within 60s (CFG_TTL_SECONDS).
- Template is one file with inline JS; always node --check after edits.
- Splash HTML lives inside vote_service.py (SPLASH string).
- Test codes must match CODE_RE (`[A-Za-z0-9_-]{12,64}`).
- Local venv runs Python 3.14: needs google-cloud-firestore ≥2.28 /
  protobuf ≥6 (older protobuf crashes on import). The Firestore client in
  vote_service is lazy (`_LazyDB`) so imports work without GCP creds.
- A parallel Claude session has edited these files before — check `git status`
  /timestamps before large refactors.

## Related assets in the parent folder

`kiosk-voting-prototype.html` + `kiosk-voting-debs.html` (earlier OTP-flow
static prototypes, superseded), `kiosk-voting-backend.py` (OTP reference
backend, different auth model), `stv_tabulate.py` (standalone Scottish STV
tabulator — validates BLTs from make_blt), `Election-Integrity-Plan.docx`
(committee adoption draft), `referendum-staging/` (old static Cloud Run
deploys: services `member-ballot`, `referendum-prototype` — candidates for
deletion).

## Roadmap

1. ~~Self-service~~ DONE (2026-07-19): config in Firestore, admin console at
   `/admin/` (builder + Art. V validation, adjudication queue, void UI,
   close-out), scoped chapter-admin tokens. Still open from this item:
   roll picker + code gen in the console (blocked on roadmap #2).
2. ~~Wire generate_codes.py to the real roll~~ DONE (2026-07-19):
   `ak_primary_id` (124,596 eligible primaries at wiring time; ~100% email
   coverage) + `ak_user_fields_pivoted.chapter` (241 chapters; matched to
   polls by `roll_chapter`/`name`). Open: a real `--write` run needs ADC
   (`gcloud auth application-default login`, or run from a service account).
3. Cloud Scheduler close-out automation; Cloud Armor; min/max instances;
   Secret Manager; billing alerts.
4. Human screen-reader pass (VoiceOver/TalkBack/NVDA) before any public WCAG
   conformance claim — see tools/accessibility_results.md.
5. Resolve wgreen@dsausa.org "Gaia id not found" org-access issue (blocks
   real IAM admin identity).
