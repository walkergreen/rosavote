# How to Verify Your Vote

This referendum is run so that you — and anyone else — can check the result
for yourself after voting closes. This page explains what you can verify, how
to do it, and, just as importantly, what these checks do and don't prove.

## Your receipt

When you cast your ballot, you received an 8-character **receipt code** (for
example, `192B4DA8`). Save it — take a screenshot. It's how you check your own
vote after the results are published.

Your receipt is not linked to your name or your voting code anywhere in the
published data. It identifies your ballot without identifying you.

## What you can check after voting closes

After the poll closes, we publish four things:

1. **The ballot list** — every ballot, shown only as its receipt code and the
   choice recorded (Yes / No / Abstain). No names, no contact information.
2. **The used-code list** — the anonymized list of voting codes that were used,
   so the number of ballots can be checked against the number of voters.
3. **A verification file** — a fingerprint (hash) of the complete ballot list.
4. **A verification program** — a short script anyone can run to check the
   published data is internally consistent and unaltered.

With these, you can confirm three things:

**1. Your own vote was recorded as you were shown it.**
Find your receipt code in the published ballot list and check the choice next
to it matches what your confirmation screen showed.

**2. No ballots were added, removed, or changed after the fact.**
The verification program recomputes the fingerprint of the whole ballot list
and compares it to the one we published (and shared with observers) right after
voting closed. If a single ballot were altered, the fingerprints wouldn't match.

**3. The count is correct.**
Anyone can add up the published ballots and get the same totals we report.
Abstentions are excluded from the Yes/No result. Independent tabulation
software can also re-run the count from a published ballot file, so the result
doesn't depend on trusting our software.

The number of ballots also has to equal the number of used voting codes — this
is what shows no extra ballots were stuffed in and none went missing.

## What these checks do — and don't — prove

We want to be straight with you about the limits, because a vote is only
trustworthy if its guarantees are described honestly.

**These checks prove:** that the published set of ballots was not tampered with
after voting closed, that the totals are computed correctly, and that the number
of ballots matches the number of voters. Your receipt lets you confirm your
ballot appears with the choice you were shown.

**These checks do not prove**, by cryptographic means alone, that the voting
website recorded the selection you made at the exact moment you tapped it. In
principle a faulty or dishonest server could show one choice on screen and store
another. Two things guard against this: the recorded ballots are locked by the
published fingerprint the instant voting closes (so nothing can be quietly
changed afterward), and **the more voters who check their receipts, the smaller
any discrepancy could be without being caught.** If your receipt ever shows a
choice you didn't make, report it immediately using the contact below — every
report matters.

This is the same trust model used by widely used online-voting services for
organizations and unions: strong protection against tampering and miscounting,
with recording integrity backed by receipt-checking rather than by cryptographic
proof of each individual selection. It is the deliberate trade we made to keep
voting simple and accessible from any phone. Systems that cryptographically
prove cast-as-intended exist, but require a more complex voting process; we
judged broad, easy participation to be the right priority for this referendum.

## If something looks wrong

If your receipt doesn't appear, shows the wrong choice, or anything else seems
off, contact the elections team at [ELECTIONS CONTACT] with your receipt code.
Reports are taken seriously and investigated.

## For the technically inclined

The published verification program and ballot files let you independently:
recompute the ballot-list fingerprint and confirm it matches the one anchored at
close; confirm every used code appears in the code list committed before voting
opened; recompute the Yes/No/Abstain totals; and load the ballot file into
independent tabulation software to reproduce the result. Instructions are
included with the published files.
