# Ballot Accessibility — Test Script & WCAG 2.1 AA Conformance Note

For: DSA National Referendum ballot (branded, code-gated, multi-chapter).
Purpose: (1) a repeatable manual screen-reader test any tester can run, and
(2) a conformance record for the election file listing which WCAG 2.1 AA
success criteria the ballot meets and how.

---

## Part 1 — Manual screen-reader test script

Run the full flow with a screen reader ON, using a real assistive-tech setup.
Test on at least two of: VoiceOver (iOS Safari), TalkBack (Android Chrome),
NVDA (Windows Firefox/Chrome). Mobile matters most — most members vote on phones.

Setup:
- Open a chapter ballot URL, e.g. `/p/debs_endorsement__staging/`.
- Seed a valid test code first so you can complete a real vote.
- Turn the screen reader on before loading the page.

### A. Landing & orientation
1. Load the page. **Expect:** the screen reader announces the page title
   (chapter name + "DSA National Referendum") and you can navigate by landmark
   to banner, main, contentinfo.
2. Navigate by heading. **Expect:** you reach "Enter Your Code" (h2) and can
   move by heading level through the flow.

### B. Code entry
3. Tab to the code field. **Expect:** its label "Voting code" is announced.
4. Type an invalid code, activate "Open my ballot." **Expect:** the error
   ("That voting code was not recognized.") is **announced immediately**
   without moving focus manually — it's a live alert region.
5. Enter a valid code, activate. **Expect:** focus moves to the next screen and
   the "Your Ballot" heading is announced (you know the view changed).

### C. Ballot — the critical radio-group test
6. With the ballot showing, Tab to the choices. **Expect:** focus lands on the
   radio group; the first option ("Yes — Endorse") is announced as a radio,
   not selected.
7. Press **Down / Right arrow**. **Expect:** focus moves to "No," announced as
   a radio; selection follows focus (aria-checked updates).
8. Press **Up / Left arrow**. **Expect:** focus moves back; wraps correctly at
   the ends (Down from Abstain → Yes).
9. Press **Space or Enter** on a choice. **Expect:** it's announced as checked.
10. Tab past the group. **Expect:** you reach "Review my vote," and it is only
    enabled once a choice is selected.

### D. Review & cast
11. Activate "Review my vote." **Expect:** focus moves to the "Review" heading;
    your selected choice is announced.
12. Activate "Cast." **Expect:** focus moves to the receipt screen heading
    ("Vote Recorded"), and the **receipt code is announced** (live region).
13. Confirm you can navigate to and read the receipt code character by
    character (important — voters must capture it).

### E. Provisional path (no code)
14. From code entry, open "Can't find your code?" → activate "Cast a
    provisional ballot." **Expect:** focus moves to "Provisional Ballot"
    heading; the sealed-until-verified notice is reachable.
15. Submit with a missing field. **Expect:** the validation error is announced.
16. Complete the form → vote → **Expect:** "Provisional Ballot Received"
    announced with receipt.

### F. Zoom & reflow
17. Pinch-zoom (mobile) or browser zoom to 200%. **Expect:** the page zooms
    (no lock), text reflows, no content clipped or lost, buttons still usable.

### Pass criteria
Every "Expect" above is met on each tested screen reader. Log any step that
fails with device/SR/browser + what was announced vs. expected.

---

## Part 2 — WCAG 2.1 AA conformance note

How the ballot meets the success criteria most relevant to a voting interface.
"Verified" = confirmed in code and by the manual script above.

### Perceivable
- **1.1.1 Non-text Content (A):** decorative ballot-bubble SVGs are
  `aria-hidden="true"`; selection state is conveyed via `aria-checked`, not the
  graphic. Meets.
- **1.3.1 Info & Relationships (A):** semantic `header/main/footer/section`,
  real headings, `<label for>` on inputs, `role="radiogroup"`/`role="radio"`
  for the choices. Meets.
- **1.4.3 Contrast (Minimum) (AA):** body and display text pass 4.5:1 (large
  display type exceeds by a wide margin). Two small grey tokens that measured
  below 4.5:1 (turnout sub-text, receipt label) were darkened to pass. Red
  #dd1111 on cream measures 4.67:1 for small text. Meets.
- **1.4.4 Resize Text (AA):** zoom lock removed (`maximum-scale`/
  `user-scalable=no` deleted); text resizes to 200% without loss. Meets.
- **1.4.10 Reflow (AA):** single-column mobile-first layout (max-width 430px)
  reflows without horizontal scrolling at 320px CSS width. Meets.

### Operable
- **2.1.1 Keyboard (A):** all controls keyboard-operable. The choice group
  implements the radio pattern — roving `tabindex`, Arrow keys move+select,
  Space/Enter select. Buttons and links are native and focusable. Meets.
- **2.1.2 No Keyboard Trap (A):** no focus traps; Tab moves through and out of
  every screen. Meets (confirm in manual test).
- **2.4.3 Focus Order (A):** on each screen change, focus moves to the new
  screen's heading, so SR users are oriented and order is logical. Meets.
- **2.4.7 Focus Visible (AA):** native focus outlines retained; inputs show a
  visible focus ring (`:focus` outline in CSS). Meets (verify no outline is
  suppressed by a reset).
- **2.5.5 Target Size (best practice / 2.2 AAA):** primary buttons ≥ 52px min
  height; options are full-width. Exceeds AA.

### Understandable
- **3.2.2 On Input (A):** no context change happens automatically on input;
  actions require an explicit button. Meets.
- **3.3.1 Error Identification (A):** validation errors are shown in text and
  announced via `role="alert"`. Meets.
- **3.3.2 Labels or Instructions (A):** every input has a visible label and the
  code field has instructional lede text. Meets.

### Robust
- **4.1.2 Name, Role, Value (A):** custom radio controls expose role
  (`radio`/`radiogroup`), state (`aria-checked`), and accessible names via
  their text. Meets.
- **4.1.3 Status Messages (AA):** errors use `role="alert"`; the receipt uses
  `aria-live="polite"`/`role="status"` so it's announced without stealing
  focus. Meets.

### Known items to confirm before sign-off
- Run the Part 1 script on real VoiceOver + one of TalkBack/NVDA. Code-level
  conformance is necessary but not sufficient; a human pass is required for AA
  sign-off.
- Confirm the CSS reset doesn't suppress `:focus-visible` outlines on the
  `.vk-opt` buttons; if it does, add a visible focus style.
- Provisional form: confirm tab order and that each field error announces.
- If any third-party font fails to load, confirm text remains legible (system
  fallback) — fonts are declared with serif/sans fallbacks.

---

*Prepared as part of the election accessibility record. Attach the completed
Part 1 results (device/SR/browser + pass/fail per step) for the file.*
