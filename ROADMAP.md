# RosaVote roadmap

Twelve weeks to a pilot chapter election, run by someone other than me, on a
system somebody outside the project has reviewed.

Most of that is waiting, not building. The engineering left is about two
weekends. Weeks below are wall-clock, not effort.

---

## Do these first

Two of them unblock everything else, and one is a live security hole.

- [ ] Revoke the public demo national-root token (5 min)
      `tools/set_demo_admin.py --role chapter --polls demo_sandbox --write`
- [ ] Create `mg.rosavote.org` in Mailgun, add SPF + DKIM in Cloudflare (2 hrs)
- [ ] Send the first warming batch, then ramp daily (starts the 2-week clock)

Nothing else on this list is time-critical. These three are.

---

## Status today

- [x] Ballots: ranked, score, STAR, yes/no, multi-select
- [x] Counts: Scottish + Meek STV, Score/STAR/Bloc STAR/STAR-PR, quota constraints, alternates
- [x] Code-authenticated voting, scoped admin, provisional adjudication, void-and-reissue
- [x] Hash-chain publication, per-voter receipts, in-browser verifier
- [x] 325 automated checks, 40-election regression over published contest results
- [x] Reproduces the certified 2025 NPC At-Large result from the published ballots
- [x] Live at rosavote.org and app.rosavote.org, independent infrastructure
- [ ] Can send a voting code (no email or SMS provider configured)
- [ ] Has run a binding election (everything so far is a demo or a replay)
- [ ] Reviewed by anyone outside the project
- [ ] Operable by anyone but me

---

## Work versus wait

Engineering left is three or four days. The twelve weeks is other people's
calendars.

| Waiting on | How long | Compressible |
|---|---|---|
| Email domain warming | 2 weeks | No |
| A chapter's own election date | Whatever it is | No |
| Independent security review | 2 to 4 weeks | Only by starting early |
| Data agreement signed | Depends who signs | Somewhat |
| Second operator trained | A few sessions | Somewhat |

The earliest responsible pilot is about four weeks out, and warming is the only
reason it isn't two.

Why warming matters: `mg.rosavote.org` has no reputation with Gmail or Outlook.
Send a few thousand cold emails on day one and a chunk land in spam. Members who
never see the ballot link don't vote. So the sending domain sets the pilot date,
not the code.

---

## Phase 0. Lock down (week 1)

The instance is publicly reachable and has a public admin path. Nothing else
matters until that's closed.

- [ ] Revoke demo national-root token (5 min)
- [ ] Verify demo token reaches `demo_sandbox` and nothing else (10 min)
- [ ] Decide whether the NPC replay archive stays on the public instance
- [ ] Enable Cloud Scheduler API (5 min)
- [ ] Recreate the close-out job (30 min)
- [ ] Recreate budget alerts at 50/90/100% (30 min)

**Done when:** no public path to national admin, demo data provably separated
from anything that could become real.

## Phase 1. Make it send (weeks 1 to 3)

- [ ] Mailgun (or SES) sending domain `mg.rosavote.org` (1 hr)
- [ ] SPF + DKIM records in Cloudflare (30 min)
- [ ] `mailgun-api-key` in Secret Manager, wired via `--set-secrets` (30 min)
- [ ] `MAILGUN_DOMAIN` / `MAILGUN_FROM` env on the service (10 min)
- [ ] Twilio credential for SMS fallback (30 min)
- [ ] Warming schedule started, volume ramped daily (2 weeks wall-clock)
- [ ] Seed-test inbox placement: Gmail, Outlook, Yahoo, one .edu (30 min/round)
- [ ] Deliverability logged daily (bounce, complaint, placement)

**Done when:** two weeks of warming behind us, bounce under 2%, complaints under
0.1%, seed tests landing in inbox everywhere.

## Phase 2. Make it operable (weeks 3 to 5)

- [ ] Intake form: chapter, election type, dates, roster size, contact (1 hr)
- [ ] Provisioning runbook, precise enough for a stranger to follow (half day)
- [ ] CSV roll import rehearsed against a real chapter export (2 hrs)
- [ ] GCS import path rehearsed for rosters over the inline cap (1 hr)
- [ ] Retention and deletion procedure written and tested (2 hrs)
- [ ] Data agreement drafted: controller, processor, retention, breach notice

**Done when:** an election gets set up by following the runbook instead of
remembering how, and someone else reads it without finding gaps.

## Phase 3. Prove it (weeks 5 to 7)

- [ ] Edge rate limiting on `/admin` and vote endpoints (half day to a day)
      Blocked on a decision: `app.rosavote.org` is DNS-only for the Cloud Run
      certificate, so Cloudflare's WAF isn't in front of it. Either move to a
      load balancer with Cloud Armor, or restructure the edge.
- [ ] Load test at realistic roster size, `tools/load_test.py` (1 hr)
- [ ] Dress rehearsal: fake chapter, real emails, real window, real close (1 day)
- [ ] Verify the published chain from the rehearsal as an outsider would (30 min)
- [ ] Fix rehearsal defects, then run it again (open-ended)

**Done when:** a complete fake election runs with zero manual interventions and
the published result verifies from the public chain.

## Phase 4. Pilot (weeks 7 to 9)

The pilot is a low-stakes contest. A bylaws vote or a small officer race, not a
contested convention delegation.

- [ ] Pilot chapter identified and data agreement signed
- [ ] Roster imported, codes minted, manifest checked
- [ ] Change freeze declared for the voting window
- [ ] Codes delivered, delivery monitored for the first hour
- [ ] Election closed, counted, published
- [ ] Retro written: what broke, what confused people, what they asked for

**Done when:** the chapter accepts a certified result, the chain verifies, and
the retro exists.

## Phase 5. Remove the single point of failure (weeks 9 to 11)

This is the difference between a project and one person's side project.

- [ ] Second operator identified
- [ ] Second operator has their own scoped credentials
- [ ] Second operator runs a full election on staging, solo, from the runbook
- [ ] Independent security review booked, scoped to admin surface and code path
- [ ] Security review completed, findings triaged
- [ ] Human screen-reader pass: VoiceOver, NVDA, TalkBack
- [ ] Staging environment on its own Firestore database
- [ ] Continuity plan written: who runs the election if I'm unavailable

**Done when:** someone other than me runs a full election on staging, solo.

## Phase 6. Open the door (weeks 11 to 13)

- [ ] Chapters two and three onboarded, friction points noted
- [ ] Security review results published, including anything unresolved
- [ ] Self-service signup, only if the concierge process is boring by now

**Done when:** a chapter onboards without me writing custom instructions.

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

## Not doing yet

| Deferred | Why | What happens instead |
|---|---|---|
| Self-service signup | Real product surface, not needed to prove the system | Set up by hand via support@ |
| Warehouse roll import | The service account has no BigQuery role by design | CSV / GCS import |
| Multi-org tenancy | Two chapters don't need it | Single operator, per-poll scoping |
| WCAG conformance *claim* | Needs a human screen-reader pass | Do the pass, describe it, don't certify |

---

## What we're worried about

**Deliverability, not code.** The most likely way a real election goes wrong,
and it looks the least like an engineering problem, so it's the one that gets
skipped.

**Single operator.** I hold the admin credentials, the cloud account, and the
knowledge. If I'm unavailable mid-election there's no continuity plan. Phase 5
exists to fix this, and it's the main reason this runs twelve weeks instead of
four.

**Personal infrastructure holding member data.** Production runs in a personal
GCP project. A chapter's roster would live there under one person's control.
Before real rosters land we settle who is controller, who is processor,
retention, deletion, and breach notification. Not a technical problem, and it
gates the pilot.

**No independent security review.** The code is open and tested. Nobody outside
the project has tried to break it. The site says so.

**The first binding election is the riskiest one.** Hence a bylaws vote for the
pilot.

---

## What stops us

Decided now, so they don't get argued about later under deadline pressure.

- Warming plateaus with bounce over 5%, or any spam-folder placement in seed
  tests. We fix deliverability or run the pilot on a different sending domain.
- The dress rehearsal needs a manual intervention to finish. Not ready, pilot
  moves.
- No data agreement by the end of Phase 2. We don't take a real roster without
  one.
- A member can't complete a ballot in the accessibility pass. Fixed before the
  pilot, not after.
