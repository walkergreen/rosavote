# RosaVote roadmap

Two tracks to the same place. The fast track runs one pilot chapter election
in about five weeks. The methodical track takes twelve and ends with a system
someone other than the author can operate.

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

## The one thing you can't compress

Email domain warming. `mg.rosavote.org` doesn't exist yet and has no sending
reputation. A brand new domain that sends a few thousand first-contact emails
in one burst gets filtered, and a filtered election is a failed election.

Two weeks of graduated volume is the floor. That constraint sets the earliest
possible pilot date no matter how fast everything else moves. Start it in
week 1 of either track, before anything else is ready, because it runs in the
background while other work happens.

---

## Fast track: pilot in five weeks

Take this if a chapter has a real election on the calendar and wants to use
RosaVote for it.

**What you get.** One low-stakes chapter election, set up by hand, email
delivery only, results published with a verifiable chain.

**What you accept.** No independent security review. No second operator, so
the author is a single point of failure for the whole election. No staging
environment, so changes go straight to the system running the vote. Rate
limiting is whatever the platform gives you.

**Week 1.** Revoke the public demo root token. Isolate demo data from
anything real. Stand up `mg.rosavote.org` with SPF and DKIM and begin warming
immediately. Open the data-governance conversation with the chapter.

**Week 2.** Provisioning runbook written down as you do it. CSV roll import
rehearsed with the chapter's actual export format. Scheduler and budget alerts
restored. Warming continues.

**Week 3.** Full dress rehearsal: fake chapter, real emails to real inboxes,
real window, real close, real publish, real verification. Load test at the
chapter's roster size. Warming continues and you now have two weeks of
reputation data.

**Week 4.** Fix everything the rehearsal broke. Re-run the parts that failed.
Freeze changes at the end of this week.

**Week 5.** Run the election. Nothing ships during the voting window.

**Go/no-go before week 5.** All P0 items closed. Dress rehearsal passed end to
end without a manual intervention. Bounce rate under 2% and no spam-folder
placement in seed tests. Data agreement signed. If any of those is false, the
pilot slips a week. That is the whole point of having a gate.

---

## Methodical track: twelve weeks

Take this if there's no election forcing the date. It ends somewhere the fast
track doesn't: with a second trained operator, a security review, and a
documented path for chapters three and four.

### Phase 0. Lock down (week 1)

The instance is publicly reachable and currently has a public admin path.
Nothing else matters until that's closed.

- Revoke the demo national-root token (`tools/set_demo_admin.py --role chapter
  --polls demo_sandbox --write`)
- Confirm the demo token reaches the sandbox poll and nothing else
- Decide whether the NPC replay archive stays on the public instance
- Enable Cloud Scheduler API, restore the close-out job, restore budget alerts

**Exit:** no public path to national admin. Demo data provably separated from
anything that could become real.

### Phase 1. Make it send (weeks 1 to 3, overlaps Phase 0)

- Mailgun or SES sending domain `mg.rosavote.org`, SPF and DKIM in Cloudflare
- Credential in Secret Manager, wired via `--set-secrets`, never env vars
- Twilio for SMS fallback
- Begin graduated warming on day one and log deliverability daily

**Exit:** two weeks of warming behind you, bounce under 2%, complaint rate
under 0.1%, seed tests landing in inbox across Gmail, Outlook, Yahoo, and at
least one .edu.

### Phase 2. Make it operable (weeks 3 to 5)

- Intake form: chapter, election type, dates, roster size, contact
- Written provisioning runbook, precise enough that a stranger could follow it
- CSV and GCS roll import rehearsed with a real chapter export
- Retention and deletion procedure, written and tested

**Exit:** you can set up an election by following the runbook instead of
remembering how. Someone reads it and finds no gaps.

### Phase 3. Prove it (weeks 5 to 7)

- Edge rate limiting on `/admin` and the vote endpoints. Note the constraint:
  `app.rosavote.org` is DNS-only because Cloud Run issues its own certificate,
  so Cloudflare's WAF isn't in front of it. Either move to a load balancer with
  Cloud Armor or restructure the edge.
- Load test at realistic roster size with `tools/load_test.py`
- Full dress rehearsal, end to end, with real delivery
- Fix rehearsal defects and re-run

**Exit:** a complete fake election with zero manual interventions, and a
published result that verifies from the public chain.

### Phase 4. Pilot (weeks 7 to 9)

- One low-stakes real election. Bylaws vote or a small officer race.
- Change freeze during the voting window
- Post-election review written down: what broke, what was confusing, what the
  chapter asked for

**Exit:** a certified result the chapter accepts, a chain that verifies, and a
written retro.

### Phase 5. Remove the single point of failure (weeks 9 to 11)

This is the phase the fast track skips, and it's the one that decides whether
RosaVote is a project or a person's side project.

- Second operator trained on the runbook, with their own scoped credentials
- Independent security review or penetration test, scoped to the admin surface
  and the code path
- Human screen-reader pass (VoiceOver, NVDA, TalkBack) before any
  accessibility claim
- Staging environment on its own Firestore database
- Continuity plan: who runs the election if the author is unavailable

**Exit:** someone other than the author runs a full election on staging,
solo, from the runbook.

### Phase 6. Open the door (weeks 11 to 13)

- Onboard chapters two and three with less hand-holding, measure where they
  get stuck
- Publish the security review results, including anything unresolved
- Self-service signup, if and only if the concierge process is boring by now

**Exit:** a chapter onboards without the author writing custom instructions.

---

## Scope discipline

Out of scope for both tracks until Phase 6, deliberately:

| Deferred | Why | What happens instead |
|---|---|---|
| Self-service signup | Real product surface, not needed to prove the system | Set up by hand via support@ |
| Warehouse roll import | The service account has no BigQuery role by design | CSV / GCS import |
| Multi-org tenancy | Two chapters don't need it | Single operator, per-poll scoping |
| WCAG conformance *claim* | Needs a human screen-reader pass | Do the pass, describe it, don't certify |

---

## Risks worth stating plainly

**Deliverability, not code, is the likeliest failure.** See the warming
section. This is the risk most likely to actually sink a real election, and
it's the one that looks least like an engineering problem.

**Single operator.** One person holds the admin credentials, the cloud
account, and the knowledge. If that person is unavailable mid-election there's
no continuity plan. The fast track accepts this risk. The methodical track
exists largely to retire it.

**Personal infrastructure holding member data.** Production runs in a personal
GCP project. A chapter's roster would live there under one person's control.
Before real rosters land: who is controller, who is processor, retention,
deletion, breach notification. This isn't a technical problem and it gates
either track.

**No independent security review.** The code is open and tested. Nobody
outside the project has tried to break it. Say that instead of implying
otherwise.

**The first binding election is the riskiest one.** Pick a low-stakes contest
for the pilot. A bylaws vote or a small officer race, not a contested
convention delegation.

---

## When to stop and reconsider

Abort criteria, decided in advance so they're not argued about under pressure:

- Warming plateaus with bounce over 5% or any spam-folder placement in seed
  tests. Fix deliverability or run the pilot on a different sending domain.
- The dress rehearsal needs a manual intervention to complete. Not ready.
- No data agreement by the end of Phase 2. Don't accept a real roster without
  one.
- A member can't complete a ballot in the accessibility pass. Fix before pilot,
  not after.
