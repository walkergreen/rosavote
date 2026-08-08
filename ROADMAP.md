# RosaVote roadmap

**Goal: run one real, binding chapter election on RosaVote within four weeks.**
One pilot chapter, set up by hand.

This file is the honest status of the project. It's specific about what isn't
built, because the most common question about RosaVote is what it actually is.

---

## Where this is today

The software is feature-complete for the election lifecycle. Ballots (ranked,
score, STAR, yes/no, multi-select), Scottish and Meek STV, Score/STAR/STAR-PR,
diversity-quota constraints, delegate alternates, code-authenticated voting,
scoped admin, provisional adjudication, void-and-reissue, hash-chain
publication, and an in-browser verifier all exist and are covered by tests.

It's validated against published election records. DSA publishes the ballot
file for its NPC elections so members and observers can recount them. Run
against the published 2025 NPC At-Large ballots, this tabulator returns the
certified winner set, candidate for candidate. Those files are public record
and contain rankings only, never voter identities. Same for the 40-election
regression suite: every fixture is a published contest result, anonymized by
the BLT format itself.

It has never run a binding election. Everything on the live instance is a demo
or a replay of a count that was already certified. Nothing here has decided
anything.

There's no self-service. You can't sign up and run an election. Getting one
onto RosaVote today means emailing support@rosavote.org and having it set up
by hand.

It can't currently send voting codes. No email or SMS provider is configured
on production. That's the real gap between "the software works" and "a chapter
can vote on it."

---

## Scope for the next four weeks

In scope: one pilot chapter election, run concierge. The chapter supplies a
membership CSV, the election is configured by hand, codes go out by email,
results publish with a verifiable chain.

Out of scope, deliberately:

| Deferred | Why | What happens instead |
|---|---|---|
| Self-service signup | Real product surface, not needed to prove the system | Set up by hand via support@ |
| Warehouse roll import | The service account has no BigQuery role by design | CSV / GCS import |
| Independent penetration test | Can't procure and remediate one in four weeks | Say plainly that none has been done |
| WCAG conformance *claim* | Needs a human screen-reader pass | Do the pass, describe it, don't certify |
| Multi-org tenancy | One pilot doesn't need it | Single operator, per-poll scoping |

---

## P0: blockers before any real ballot exists

None of these are optional.

- [ ] **Revoke the public demo national-root token.** `DEMO-ADMIN-TOKEN-2026`
      is printed on the console sign-in page and currently resolves to
      `role: national, polls: (all)`. That's full admin over every poll on the
      instance, including the NPC replay archive. Anyone on the internet can
      open the console and delete or edit an election.
      Fix: `tools/set_demo_admin.py --role chapter --polls demo_sandbox --write`
- [ ] **Isolate demo data from real elections.** Once the demo token is
      chapter-scoped, confirm the sandbox is the only poll it reaches. Decide
      whether the NPC replay stays on the public instance at all.
- [ ] **Stand up email delivery.** Mailgun or SES sending domain
      `mg.rosavote.org`, SPF and DKIM in Cloudflare, credential in Secret
      Manager, wired through `--set-secrets`. Until this exists, zero codes go
      out.
- [ ] **Warm the sending domain.** A cold domain sending a few thousand
      first-contact emails is the likeliest way a real election fails. Send
      graduated volume for at least two weeks before the pilot and watch
      bounce and complaint rates.
- [ ] **Settle data governance in writing.** Production runs in a personal GCP
      project. A chapter's roster (names, emails, phones) would live there
      under one person's control. Before real member data lands: who is
      controller, who is processor, retention and deletion, breach
      notification, and what the chapter is agreeing to. This gates the pilot
      and it isn't a technical problem.

## P1: required for a credible pilot

- [ ] **Intake path.** A request form (chapter, election type, dates, roster
      size) and a written provisioning runbook, so setup is repeatable instead
      of improvised.
- [ ] **CSV roll import rehearsal.** The BigQuery path is dead by design.
      Prove the CSV/GCS path start to finish, from a chapter's export through
      code minting to the distribution manifest.
- [ ] **Restore scheduled close-out.** The Cloud Scheduler API isn't even
      enabled on the project. The auto-finalize job didn't survive the
      migration, so close-out is manual right now.
- [ ] **Budget alerts.** Re-create the spend alerting that existed before the
      migration.
- [ ] **Rate limiting at the edge.** `/admin` and the vote endpoints need L7
      limits. Constraint worth knowing: `app.rosavote.org` is DNS-only
      (grey-cloud) because Cloud Run issues its own certificate, so
      Cloudflare's WAF is not in front of it today. Either move to a load
      balancer with Cloud Armor, or restructure the edge.
- [ ] **Full dress rehearsal.** Fake chapter, real email delivery to real
      inboxes, real voting window, real close, real publish, real
      verification. Before a member ever votes for real.
- [ ] **Load test at chapter scale.** `tools/load_test.py` exists. Run it
      against production sizing.
- [ ] **SMS fallback** (Twilio) for members with no email on file.

## P2: after the pilot

- [ ] Self-service signup and multi-tenant onboarding
- [ ] Independent security review
- [ ] Human screen-reader pass (VoiceOver, NVDA, TalkBack)
- [ ] Staging environment on the new project
- [ ] Operator runbook, so someone other than the author can run an election

---

## Four-week plan

**Week 1. Make it safe, make it send.** Revoke demo root. Isolate demo data.
Stand up the Mailgun domain with SPF and DKIM and start warming it. Open the
data-governance conversation with the pilot chapter.

**Week 2. Make it operable.** Intake form and provisioning runbook. CSV roll
import rehearsed end to end. Scheduler and budget alerts restored. SMS
fallback configured.

**Week 3. Make it survive contact.** Edge rate limiting. Load test. Full dress
rehearsal with real delivery. Fix whatever the rehearsal breaks.

**Week 4. Pilot, with slack.** Run one real chapter election. Hold the back
half of the week as buffer. If Week 3 turns up something structural, the pilot
slips instead of shipping broken.

---

## Risks worth stating plainly

**Deliverability, not code, is the likeliest failure.** A brand new sending
domain plus a one-time bulk send to members who've never gotten mail from it
is a spam-filter magnet. Warming is the fix and it costs calendar time you
can't compress.

**Single operator.** One person holds the admin credentials, the cloud
account, and the knowledge. If that person is unavailable mid-election there's
no continuity plan. A second trained operator is worth more than any feature
on this list.

**Personal infrastructure holding member data.** See P0. Technically fine,
organizationally it needs an agreement before real rosters land.

**No independent security review.** The code is open and tested. Nobody
outside the project has tried to break it. Say that instead of implying
otherwise.

**The first binding election is the riskiest one.** Pick a low-stakes contest
for the pilot. A bylaws vote or a small officer race, not a contested
convention delegation.
