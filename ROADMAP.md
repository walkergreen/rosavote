# RosaVote — roadmap to chapter elections

**Goal:** be able to run a real, binding chapter election on RosaVote within
four weeks — for one pilot chapter, operated concierge-style.

This file is the honest status of the project. It is deliberately specific
about what is *not* built, because the most common question about RosaVote is
"what is this, exactly?"

---

## Where this is today

**The software is feature-complete for the election lifecycle.** Ballots
(ranked / score / STAR / yes-no / multi-select), Scottish and Meek STV,
Score/STAR/STAR-PR, diversity-quota constraints, delegate alternates,
code-authenticated voting, scoped admin, provisional adjudication,
void-and-reissue, hash-chain publication, and an in-browser verifier all
exist and are exercised by tests.

**It is validated against a real election.** The tabulator reproduces the
certified result of DSA's 2025 NPC At-Large count from the published ballots,
candidate for candidate. There are 284 automated checks and a 40-election BLT
regression suite covering real historical DSA/YDSA contests.

**It has never run a binding election.** Every election on the live instance
to date is a demo or a replay of an already-certified count. Nothing here has
decided anything.

**There is no self-service.** You cannot sign up and run an election. Getting
an election onto RosaVote today means emailing support@rosavote.org and having
it provisioned by hand.

**It cannot currently send voting codes.** No email or SMS provider is
configured on the production instance. This is the single largest gap between
"the software works" and "a chapter can vote on it."

---

## Scope for the next four weeks

**In scope:** one pilot chapter election, run concierge — the chapter supplies
a membership CSV, RosaVote is configured by hand, codes are delivered by
email, results are published with a verifiable chain.

**Explicitly out of scope** (deferred, not forgotten):

| Deferred | Why | Interim |
|---|---|---|
| Self-service signup | Real product surface; not needed to prove the system | Concierge onboarding via support@ |
| Warehouse (BigQuery) roll import | Service account has no BigQuery role by design | CSV / GCS import |
| Independent penetration test | Cannot be procured and remediated in 4 weeks | Disclose that none has been done |
| WCAG conformance *claim* | Requires a human screen-reader pass | Do the pass; describe, don't certify |
| Multi-organization tenancy | One pilot doesn't need it | Single operator, per-poll scoping |

---

## P0 — blockers before any real ballot exists

Nothing below is optional. Each is a correctness, security, or
"votes-cannot-happen" issue.

- [ ] **Revoke the public demo national-root token.** `DEMO-ADMIN-TOKEN-2026`
      is printed publicly on the console sign-in page and currently resolves to
      `role: national, polls: (all)` — full admin over every poll on the
      instance, including the real 2025 NPC ballot archive. Anyone on the
      internet can open the console and delete or edit elections.
      Fix: `tools/set_demo_admin.py --role chapter --polls demo_sandbox --write`.
- [ ] **Isolate demo data from real elections.** Once the demo token is
      chapter-scoped, confirm the demo sandbox is the *only* poll it can reach,
      and decide whether the NPC replay archive stays on the public instance at
      all.
- [ ] **Stand up email delivery.** Mailgun (or SES) sending domain
      `mg.rosavote.org`, SPF + DKIM records in Cloudflare, credential in Secret
      Manager, `MAILGUN_*` wired via `--set-secrets`. Until this exists, zero
      voting codes can be sent.
- [ ] **Warm the sending domain.** A cold domain blasting a few thousand
      first-contact emails is the most likely way a real election fails in
      practice. Send graduated volume for at least two weeks before the pilot;
      monitor bounce/complaint rates.
- [ ] **Settle data governance in writing.** The instance runs in a personal
      GCP project. A chapter's membership roster (names, emails, phones) would
      live there under one individual's control. Before accepting real member
      data: define who is controller vs processor, retention and deletion
      terms, breach notification, and what the chapter is agreeing to. This is
      a governance question, not a technical one, and it gates the pilot.

## P1 — required for a credible pilot

- [ ] **Chapter intake path.** A simple request form (name, chapter, election
      type, dates, roster size) → a written provisioning runbook so setup is
      repeatable rather than improvised.
- [ ] **End-to-end CSV roll import rehearsal.** The BigQuery path is
      intentionally dead; prove the CSV/GCS path from a chapter-supplied export
      through code minting to the distribution manifest.
- [ ] **Restore scheduled close-out.** Cloud Scheduler API is not even enabled
      on the project; the auto-finalize job did not survive the migration.
      Without it, close-out is manual.
- [ ] **Budget alerts.** Re-create the spend alerting that existed pre-migration.
- [ ] **Rate limiting at the edge.** `/admin` and vote endpoints need L7 limits.
      Note the constraint: `app.rosavote.org` is DNS-only (grey-cloud) because
      Cloud Run issues its own certificate, so Cloudflare's WAF is not in front
      of it today. Either move to a load balancer + Cloud Armor, or restructure
      the edge.
- [ ] **Full dress rehearsal.** A fake chapter, real email delivery to real
      inboxes, real voting window, real close, real publish, real verification
      — start to finish, before a member ever votes for real.
- [ ] **Load test at chapter scale.** `tools/load_test.py` exists; run it
      against production sizing.
- [ ] **SMS fallback** (Twilio credential) for members with no email on file.

## P2 — after the pilot

- [ ] Self-service signup and multi-tenant onboarding
- [ ] Independent security review / penetration test
- [ ] Human screen-reader pass (VoiceOver / NVDA / TalkBack)
- [ ] Staging environment on the new project
- [ ] Published operator runbook so someone other than the author can run an
      election

---

## Four-week plan

**Week 1 — make it safe, make it send.**
Revoke demo root; isolate demo data; stand up the Mailgun domain with SPF/DKIM
and begin warming; open the data-governance question with the pilot chapter.

**Week 2 — make it operable.**
Intake form and provisioning runbook; CSV roll import rehearsed end to end;
scheduler and budget alerts restored; SMS fallback configured.

**Week 3 — make it survive contact.**
Edge rate limiting; load test; full dress rehearsal including real delivery;
fix whatever the rehearsal breaks.

**Week 4 — pilot, with slack.**
Run one real chapter election. Reserve the back half of the week as buffer —
if the rehearsal in Week 3 surfaces anything structural, the pilot slips
rather than ships broken.

---

## Risks worth stating plainly

1. **Deliverability, not code, is the likeliest failure.** A brand-new sending
   domain plus a one-time bulk send to members who have never received mail
   from it is a spam-filter magnet. Domain warming is the mitigation and it
   takes calendar time that cannot be compressed.
2. **Single operator.** One person holds the admin credentials, the cloud
   account, and the knowledge. If that person is unavailable mid-election
   there is no continuity plan. A second trained operator is worth more than
   any feature on this list.
3. **Personal infrastructure holding member data.** See P0. Technically fine;
   organizationally it needs an explicit agreement before real rosters land.
4. **No independent security review.** The code is open and tested, and it has
   never been adversarially reviewed by anyone outside the project. Say so
   rather than implying otherwise.
5. **First binding election is inherently the riskiest one.** Prefer a
   low-stakes contest for the pilot — a bylaws vote or a small officer race,
   not a contested convention delegation.
