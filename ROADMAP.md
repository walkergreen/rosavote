# RosaVote roadmap

Twelve weeks to a pilot chapter election, run by someone other than me, on a
system somebody outside the project has reviewed.

Most of that is waiting, not building. The engineering left is about two
weekends. The schedule below is wall-clock, not effort.

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
anything yet.

There's no self-service. Nobody can sign up and run an election. Getting one
onto RosaVote today means emailing support@rosavote.org and I set it up by
hand.

It can't currently send voting codes. No email or SMS provider is configured
on production. That's the real gap between the software working and a chapter
being able to vote on it.

---

## Work versus wait

The build is nearly done. What's left is mostly other people's calendars.

Engineering remaining, three or four days total: revoke the demo token (one
command), isolate demo data, stand up the Mailgun domain and wire the secret,
restore the scheduler and budget alerts, intake form, provisioning runbook,
CSV import rehearsal, edge rate limiting, load test, SMS fallback, dress
rehearsal. Rate limiting is the fiddliest of these, because
`app.rosavote.org` is DNS-only for the Cloud Run certificate, so Cloudflare's
WAF isn't in front of it and that has to be restructured.

Then there's the part I don't control:

| Waiting on | How long | Compressible |
|---|---|---|
| Email domain warming | 2 weeks | No |
| A chapter's own election date | Whatever it is | No |
| Independent security review | 2 to 4 weeks | Only by starting early |
| Data agreement signed | Depends who signs | Somewhat |
| Second operator trained | A few sessions | Somewhat |

The earliest responsible pilot is about four weeks out, and warming is the
only reason it isn't two. Twelve weeks is how long until the single-operator
risk is gone and someone outside the project has tried to break it.

---

## The email domain goes up first

`mg.rosavote.org` doesn't exist yet, and new domains have no reputation with
Gmail or Outlook. Send a few thousand cold emails on day one and a chunk of
them land in spam. Members who never see the ballot link don't vote.

So the sending domain sets the pilot date, not the code. It goes up in week 1,
before anything else is ready, and ramps volume for two weeks while the rest
gets built.

---

## Chapters with an election sooner than this

We point them at OpaVote. It works today, it's proven, and a chapter-sized
election runs $10 or $20. RosaVote is for the cycle after, once the pilot has
shaken the bugs out on a vote with less riding on it.

That's not modesty. Nobody outside the project has tried to break the admin
surface, I hold every credential, and there's no staging, so any fix during an
election gets made on the system running it. For a bylaws vote that's
survivable. For a contested officer race or a delegate election, it isn't.

---

## The twelve weeks

Phases overlap. Phase 1 starts on day one no matter what else is unfinished,
because the warming clock is the long pole.

### Phase 0. Lock down (week 1)

The instance is publicly reachable and currently has a public admin path.
Nothing else matters until that's closed.

- Revoke the demo national-root token (`tools/set_demo_admin.py --role chapter
  --polls demo_sandbox --write`)
- Confirm the demo token reaches the sandbox poll and nothing else
- Decide whether the NPC replay archive stays on the public instance
- Enable Cloud Scheduler API, restore the close-out job, restore budget alerts

**Done when:** there's no public path to national admin, and demo data is
provably separated from anything that could become real.

### Phase 1. Make it send (weeks 1 to 3, overlaps Phase 0)

- Mailgun or SES sending domain `mg.rosavote.org`, SPF and DKIM in Cloudflare
- Credential in Secret Manager, wired via `--set-secrets`, never env vars
- Twilio for SMS fallback
- Warming starts day one, deliverability logged daily

**Done when:** two weeks of warming are behind us, bounce is under 2%,
complaints under 0.1%, and seed tests land in the inbox across Gmail, Outlook,
Yahoo, and at least one .edu.

### Phase 2. Make it operable (weeks 3 to 5)

- Intake form: chapter, election type, dates, roster size, contact
- Provisioning runbook, written precisely enough that a stranger could follow it
- CSV and GCS roll import rehearsed against a real chapter export
- Retention and deletion procedure, written and tested

**Done when:** an election gets set up by following the runbook instead of
remembering how, and someone else reads it without finding gaps.

### Phase 3. Prove it (weeks 5 to 7)

- Edge rate limiting on `/admin` and the vote endpoints, which means either
  moving to a load balancer with Cloud Armor or restructuring the edge
- Load test at realistic roster size with `tools/load_test.py`
- Full dress rehearsal, end to end, with real delivery
- Fix what the rehearsal breaks, then run it again

**Done when:** a complete fake election runs with zero manual interventions and
the published result verifies from the public chain.

### Phase 4. Pilot (weeks 7 to 9)

The pilot is a low-stakes contest. A bylaws vote or a small officer race, not a
contested convention delegation.

- Change freeze for the whole voting window
- Post-election retro written down: what broke, what confused people, what the
  chapter asked for

**Done when:** the chapter accepts a certified result, the chain verifies, and
the retro exists.

### Phase 5. Remove the single point of failure (weeks 9 to 11)

This is the difference between a project and one person's side project.

- Second operator trained on the runbook, with their own scoped credentials
- Independent security review, scoped to the admin surface and the code path
- Human screen-reader pass (VoiceOver, NVDA, TalkBack) before any
  accessibility claim
- Staging environment on its own Firestore database
- Continuity plan for who runs the election if I'm unavailable

**Done when:** someone other than me runs a full election on staging, solo,
from the runbook.

### Phase 6. Open the door (weeks 11 to 13)

- Chapters two and three onboard with less hand-holding, and we watch where
  they get stuck
- Security review results published, including anything unresolved
- Self-service signup, if and only if the concierge process is boring by then

**Done when:** a chapter onboards without me writing custom instructions.

---

## Not doing yet

| Deferred | Why | What happens instead |
|---|---|---|
| Self-service signup | Real product surface, not needed to prove the system | Set up by hand via support@ |
| Warehouse roll import | The service account has no BigQuery role by design | CSV / GCS import |
| Multi-org tenancy | Two chapters don't need it | Single operator, per-poll scoping |
| WCAG conformance *claim* | Needs a human screen-reader pass | Do the pass, describe it, don't certify |

---

## What we're worried about

**Deliverability, not code.** See the section on the email domain. It's the
most likely way a real election goes wrong, and it looks the least like an
engineering problem, so it's the one that gets skipped.

**Single operator.** I hold the admin credentials, the cloud account, and the
knowledge. If I'm unavailable mid-election there's no continuity plan. Phase 5
exists to fix this, and it's the main reason this runs twelve weeks instead of
four.

**Personal infrastructure holding member data.** Production runs in a personal
GCP project. A chapter's roster would live there under one person's control.
Before real rosters land we settle who is controller, who is processor,
retention, deletion, and breach notification. That's not a technical problem
and it gates the pilot.

**No independent security review.** The code is open and tested. Nobody
outside the project has tried to break it. The site says so.

**The first binding election is the riskiest one.** Hence a bylaws vote for the
pilot.

---

## What stops us

Decided now, so they don't get argued about later under deadline pressure.

- Warming plateaus with bounce over 5%, or any spam-folder placement in seed
  tests. We fix deliverability or run the pilot on a different sending domain.
- The dress rehearsal needs a manual intervention to finish. Not ready, and the
  pilot moves.
- No data agreement by the end of Phase 2. We don't take a real roster without
  one.
- A member can't complete a ballot in the accessibility pass. That gets fixed
  before the pilot, not after.
