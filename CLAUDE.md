# DSA Chapter Member Ballot (ballot-v2)

Code-authenticated, multi-chapter membership ballot for ~120k voters.
Flask + Firestore on Cloud Run (project `dsa-org-tools`, region `us-east1`,
service `member-ballot-v2`). Built by DSA Staff's Data & Tech Department.
Prototype status: deployed publicly for review; NOT a live election.

## Run / deploy / test

```sh
# deploy (from this directory's parent) — current service is member-ballot-v3:
gcloud run deploy member-ballot-v3 --source ballot-v2 --region us-east1 \
  --allow-unauthenticated --min-instances 0 --max-instances 20 \
  --set-secrets ADMIN_TOKEN=ballot-admin-token:latest
# live: https://member-ballot-v3-62155002849.us-east1.run.app
# (member-ballot-v2 + older services are previous prototypes — deletion candidates)

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
  (token sign-in, polls list + create-election, builder w/ questions JSON +
  presets — Art. V convention/apportionment fields appear only when a
  delegate question exists, VOTERS tab: per-voter turnout +
  received-verification + integrity counter + row void + national-only roll
  imports (browser CSV ≤20k · GCS CSV — required header `member_id`,
  optional `chapter`, extras ignored, manifest written back to the bucket ·
  BigQuery eligible-roll by chapter), RESULTS tab: overview of all polls +
  full tallies on FINALIZED polls only (in-service Scottish STV via the
  bundled `stv_tabulate.py`, two-count alternates, text counted-not-shown),
  adjudication, void). Root extras: LIVE tally on open polls
  (`?live=1`, national-only, every view audit-logged) and ballot lookup by
  member_id/receipt (identity-linked answers per policy; secret questions
  show recorded-yes/no only, never content; audit-logged). Voters can
  self-check publicly: `GET /p/<pid>/verify?receipt=X` -> found/status only
  (linked from the ballot's done screen). Static shell; every API it calls
  is token-gated. Voters view never exposes answers.
- `stv_tabulate.py` — copy of the parent folder's tabulator, bundled so
  `/admin/api/polls/<pid>/results` can count in-service. Keep in sync.
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

## Ballots are SCHEMA-DRIVEN (any election shape)

A poll's config may carry a `questions` list — ordered, any mix of types:
`yesno` (Y/N + optional Abstain) · `ranked` (Scottish STV; options, seats,
alternates>0 ⇒ two-count method + over-seat black styling; `secret` ⇒ stored
in the admin-only secret collection; `delegate` ⇒ additionally Art. V rules;
`shuffle` ⇒ random option order per load) · `multi` (optional multi-select,
exclusive Abstain) · `text` (free text; stored as identity-linked comment,
never in canonical answers). Rendering (`_question_html`), validation
(`validate_answers`), storage split (`_write_ballot_docs`), the console
builder (JSON editor + presets), and `make_blt.py --from-firestore` are all
driven by this schema. Standalone referendums and officer elections need no
convention date; Art. V validation applies only when a `delegate:true`
question exists.

Configs WITHOUT `questions` (the CHAPTERS seed, early docs) get
`demo_questions()`: the original combined 8-question ballot expressed in the
schema — Section 1 "Chapter Poll" (q1 Debs Y/N/A, q2/q3 ranked, pledges
multi, text comment), Section 2 "Convention Delegates" (q7, secret+delegate),
Section 3 "Local Issues" (q6, q8).

**LEGACY KEY↔DISPLAY MAPPING** (demo ballot only; keys stable for tools):
`q1,q2,q3,pledges,text` = display Q1–Q5 · **`q7` = delegates = display Q6** ·
**`q6` = local issue 1 = display Q7** · **`q8` = local issue 2 = display Q8**.
Do not "fix" this by renaming keys — Firestore data and tools reference them.

Ranked questions may carry QUOTA CONSTRAINTS (leadership elections):
`options[].tags` (e.g. cis_man, marginalized) + question `constraints`
(`[{tag, max|min, label}]`, NPC-style: max 13 cis_man / min 8 marginalized).
`stv_tabulate.count(constraints=, cand_tags=)` runs the guarded/doomed
constrained count; results echo per-constraint elected tallies; stage log
names quota exclusions/guards. Any chapter can set its own via config.
Quota contests REQUIRE a tags key on every candidate ([] = collected, none
apply). FULL-BODY quotas: poll-level `quota_groups: {name: [constraints]}`
+ `quota_group: name` on member questions — contests count in document
order with pre-elected carry-in and later-seat/supply feasibility
(`pre_elected/later_seats/later_supply` params). Constrained results also
carry `unconstrained` (winners/stages) — console toggle + results.txt show
both outcomes.

Ranked questions choose their counting method: `method: scottish` (default)
or `meek` (official Meek STV — required for YDSA delegate elections; full
stage log; constraints/groups supported). Published results FREEZE into
`{poll}__published/results` (JSON blob) at publish time + in-process cache —
public page + admin results serve the frozen copy (`?fresh=1` recomputes).
Demo console carries `npc_atlarge_2025` — the real 2025 NPC At-Large
election replayed from the official OpaVote export as a frozen result.
License: AGPL-3.0, © 2026 Walker Green (LICENSE + README).

Windows: `timezone` (IANA, default America/New_York) — builder datetimes are
poll-local; Art. V date math uses the poll tz. `finalized` polls reject votes
regardless of window and refuse builder edits without an explicit
`unfinalize:true` (audited).

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

## Vote weights

Per-voter integer ballot weights (1–1000, default 1) live on the CODE docs:
provisioned via any import (optional `weight` column/field), edited any time
via `POST /admin/api/polls/<pid>/voters/weight` (national-only, audited) or
the Voters tab. Tallies resolve the CURRENT weight at count time — edits
before/during/after the election reflow live + final results and the
exported BLTs (weights ride the BLT weight column, so independent
tabulators reproduce weighted outcomes). Promoted provisionals stamp their
weight on the ballot doc (adjudicator can pass `weight` on verify).
BLT export: `GET /admin/api/polls/<pid>/blt/<qkey>[?recount=1][&live=1]`
(same gating as results); console Results tab has downloads + independent-
verification instructions.

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
3. ~~Ops hardening~~ MOSTLY DONE (2026-07-19): deployed member-ballot-v3
   (max-instances 20, ADMIN_TOKEN via Secret Manager secret
   `ballot-admin-token`), Cloud Scheduler job `ballot-closeout` (every 15
   min → /admin/api/cron/closeout), $25/mo budget w/ 50/90/100% alerts.
   Still open: Cloud Armor needs an external HTTPS LB + domain
   (vote.dsausa.org) — decision pending; heavy close-out export (chain/BLT
   to GCS) still manual via tools/.
4. Human screen-reader pass (VoiceOver/TalkBack/NVDA) before any public WCAG
   conformance claim — see tools/accessibility_results.md.
5. Resolve wgreen@dsausa.org "Gaia id not found" org-access issue (blocks
   real IAM admin identity).
