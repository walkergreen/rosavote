# RosaVote roadmap

Twelve weeks to a pilot chapter election, run by someone other than the
author, on a system somebody outside the project has reviewed.

Most of that is waiting, not building. The engineering left is about two
weekends of work. See the next section before reading the schedule as effort.

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

## Start the email domain in week 1

`mg.rosavote.org` doesn't exist yet, and new domains have no reputation with
Gmail or Outlook. Send a few thousand cold emails on day one and a chunk of
them land in spam. Members who never see the ballot link don't vote.

So the sending domain sets the pilot date, not the code. Give it two weeks of
ramping volume. Set it up in week 1 and let it warm while everything else gets
built.

---

## Work versus wait

The build is nearly done. What's left is mostly other people's calendars, and
conflating the two is how roadmaps end up dishonest.

**Engineering left, roughly three or four days total:** revoke the demo token
(one command), isolate demo data, stand up the Mailgun domain and wire the
secret, restore the scheduler and budget alerts, intake form, provisioning
runbook, CSV import rehearsal, edge rate limiting, load test, SMS fallback,
dress rehearsal. The rate limiting is the fiddliest item because
`app.rosavote.org` is DNS-only for the Cloud Run certificate, so Cloudflare's
WAF isn't in front of it and that has to be restructured.

**Calendar you don't control:**

| Waiting on | How long | Can you compress it |
|---|---|---|
| Email domain warming | 2 weeks | No |
| A chapter's own election date | Whatever it is | No |
| Independent security review | 2 to 4 weeks | Only by starting early |
| Data agreement signed | Depends who signs | Somewhat |
| Second operator trained and available | A few sessions | Somewhat |

So the earliest responsible pilot is about four weeks out, and warming is the
only reason it isn't two. Twelve weeks isn't how long the work takes. It's how
long until the single-operator risk is gone and someone outside the project
has tried to break it.

---

## If a chapter has an election coming up sooner

Run it on OpaVote. It works today, it's proven, and it costs a few hundred
dollars. Use RosaVote for the cycle after, once the pilot has shaken the bugs
out on a vote with less riding on it.

That's not modesty. Nobody outside the project has tried to break the admin
surface, one person holds every credential, and there's no staging, so any fix
during an election gets made on the system running it. For a bylaws vote
that's survivable. For a contested officer race or a delegate election, it
isn't.

---

## The twelve weeks

Phases overlap. Weeks are wall-clock, not effort. Start Phase 1 on day one
regardless of what else is unfinished, because the warming clock is the long
pole.

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

This is the difference between a project and one person's side project.

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

**Deliverability, not code, is the likeliest failure.** See the section on
starting the email domain in week 1. It's the most likely way a real election goes wrong, and
it looks the least like an engineering problem, so it gets skipped.

**Single operator.** One person holds the admin credentials, the cloud
account, and the knowledge. If that person is unavailable mid-election there's
no continuity plan. Phase 5 exists to retire this, and it's the main reason
the schedule runs twelve weeks instead of four.

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
