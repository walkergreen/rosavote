# Accessibility Test Results — Chapter Member Ballot (ballot-v2)

Tested: July 15, 2026 · Pages: chapter ballot (all 8 questions) + splash
Target: WCAG 2.1 AA

## Automated results

**axe-core 4.x (semantic rules, run against the rendered DOM):
0 violations on both pages** (28 rule groups passing on the ballot, 19 on the
splash; contrast rule excluded from this run and verified mathematically
below, because the headless DOM used for the scan cannot compute styles).

**Contrast (1.4.3), computed from the design tokens — all 13 pairs pass:**

| Pair | Ratio | Requirement |
|---|---|---|
| Ink on cream / white / tan | 19.4 / 21.0 / 16.7 | 4.5:1 |
| DSA red (#dd1111) on cream / white | 4.67 / 5.04 | 4.5:1 |
| Cream / white text on red bands & buttons | 4.67 / 5.04 | 4.5:1 |
| Grey text tokens (α .82/.72/.62/.60/.55 over white/cream) | 13.6 / 8.9 / 6.2 / 5.7 / 4.68 | 4.5:1 |

The tightest pairs (red-on-cream 4.67, footer grey 4.68) pass with little
margin — do not lighten these tokens.

## Criterion-by-criterion status

- **1.4.4 Resize text** — PASS: no viewport zoom lock on any page.
- **1.4.3 Contrast** — PASS (table above).
- **2.1.1 Keyboard** — PASS by construction: every control is a native
  <button>/<input>/<textarea>; Yes/No/Abstain radio groups (Q1, Q4, Q7 local)
  have roving tabindex + arrow-key navigation; ranked and pledge groups are
  plain toggle buttons (Tab + Enter/Space). Needs human confirmation.
- **2.4.3 Focus order** — PASS by construction: focus moves to each screen's
  heading on transition. Needs human confirmation.
- **4.1.2 Name/Role/Value** — FIXED THIS ROUND: ranked-choice buttons now
  expose aria-pressed and an accessible name that carries the rank
  ("Emma Goldman, ranked 2"); previously rank state lived only in an
  aria-hidden visual chip and screen readers heard nothing.
- **4.1.3 Status messages** — PASS: errors are role="alert"; receipt is
  role="status"; ranking summary lines are now aria-live="polite" so rank
  changes are announced.
- **2.5.5-adjacent (target size)** — all tap targets ≥40px min-height.

## What automation CANNOT certify (required before sign-off)

1. **Real screen-reader run** — VoiceOver (iOS Safari), TalkBack (Android
   Chrome), NVDA (Windows Firefox/Chrome), following the script in
   accessibility_test_and_conformance.md, extended with: rank three
   candidates on Q8 and confirm each tap announces name + rank; clear
   rankings and confirm the announcement.
2. **Keyboard-only full vote** — complete the entire 8-question flow without
   a pointer; confirm focus is always visible and never trapped.
3. **200% zoom / 320px reflow (1.4.10)** — vote at 200% browser zoom and on a
   320px-wide viewport; confirm no two-dimensional scrolling or clipped
   controls.
4. **Provisional form tab order** (flagged in the original audit, still open).

## How to independently verify (no trust in this report required)

- **Automated, in any browser:** open the deployed ballot → Lighthouse
  (Chrome DevTools → Lighthouse → Accessibility) and/or the free axe DevTools
  extension → run on the ballot page mid-flow (after entering a test code, so
  the question screens are in the DOM). Expect 0 violations.
- **WAVE:** wave.webaim.org against the deployed URL (splash) and the browser
  extension for the code-gated pages.
- **Contrast:** webaim.org/resources/contrastchecker with the token pairs
  above (colors are in the page CSS, verifiable in DevTools).
- **Keyboard:** unplug the mouse; Tab/Shift-Tab/arrows/Enter through a full
  test vote using any chapter's repeatable test code.
- **Screen reader:** iPhone → Settings → Accessibility → VoiceOver → run the
  test-code flow; every question, state change, error, and the receipt should
  be announced.

## Honest scope statement

Automated tools cover roughly 30-40% of WCAG criteria. This ballot passes
everything automation and static analysis can check, and the interactive
patterns were built to AA. The claim "WCAG 2.1 AA conformant" should only be
made publicly after the human tests above are completed and recorded — that
run belongs in the election record per the Election Integrity Plan.
