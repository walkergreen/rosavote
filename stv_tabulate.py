#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Scottish STV tabulator — implements the rules OpaVote uses (Scottish Local
Government Elections Order 2007, rules 45-52), with round-by-round output.

Usage:
    python3 stv_tabulate.py ballots.blt            # text table per stage
    python3 stv_tabulate.py ballots.blt --json     # JSON for a results page

The BLT format is OpaVote's (see https://opavote.com/help/overview):
    first line:  <n_candidates> <n_seats>
    ballots:     <weight> <rank1> <rank2> ... 0     ("1 0" = empty/abstain)
    "0" line ends ballots; then quoted candidate names; then quoted title.

Key statutory mechanics implemented:
  * Quota (rule 46):  floor(valid_ballots / (seats + 1)) + 1
  * Elected when votes >= quota at end of any stage (rule 47)
  * Surplus transfer (rule 48): every paper of the elected candidate moves to
    its next available preference at value  surplus * paper_value / total,
    truncated to 5 decimal places (we use exact integer math scaled by 1e5)
  * Highest surplus transfers first; ties broken by earlier-stage totals,
    then by lot (rules 49, 51 — "lot" here = documented deterministic pick)
  * Exclusion (rule 50): lowest candidate excluded, papers move at the value
    they were received
  * Last vacancies (rule 52): continuing == remaining seats -> all elected
  * Empty ballots (abstentions) are excluded from the valid total, so they
    never affect the quota — matches OpaVote's handling.
"""

from __future__ import annotations
import json
import re
import sys
from functools import cmp_to_key

SCALE = 100_000  # values held as integers scaled by 1e5 => exact truncation


def parse_blt(text: str):
    # strip comments, blanks
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    n_cands, n_seats = map(int, lines[0].split()[:2])
    i = 1
    withdrawn: set[int] = set()
    if lines[i].lstrip().startswith("-"):
        withdrawn = {abs(int(x)) for x in lines[i].split()}
        i += 1
    ballots = []  # (weight, [candidate indices 1-based])
    while lines[i] != "0":
        parts = lines[i].split()
        weight = int(parts[0])
        ranks = []
        for tok in parts[1:]:
            if tok == "0":
                break
            if tok == "-" or "=" in tok:   # skipped ranking / overvote: ignore
                continue
            ranks.append(int(tok))
        ballots.append((weight, ranks))
        i += 1
    i += 1
    quoted = re.findall(r'"([^"]*)"', "\n".join(lines[i:]))
    names, title = quoted[:n_cands], (quoted[n_cands] if len(quoted) > n_cands else "Untitled")
    return n_cands, n_seats, withdrawn, ballots, names, title


def fmt(v: int) -> str:
    return f"{v / SCALE:.5f}".rstrip("0").rstrip(".")


def count(blt_text: str, constraints=None, cand_tags=None,
          pre_elected=None, later_seats=0, later_supply=None):
    """Scottish STV, optionally CONSTRAINED for leadership diversity quotas.

    constraints: [{"tag": str, "max": int, "label": str?} |
                  {"tag": str, "min": int, "label": str?}]
    cand_tags:   {candidate_number(1-based): [tag, ...]}

    Constrained mechanics (the standard guarded/doomed method used for
    quota-bound union and party elections):
      * a candidate is DOOMED — excluded regardless of votes — when electing
        them could no longer lead to a winner set satisfying every
        constraint (e.g. the cis-men maximum is already reached);
      * a candidate is GUARDED — immune from exclusion — when every
        remaining member of a group is needed to reach that group's minimum
        (or every non-member is needed to stay under a maximum).
    Each constraint is checked independently; with heavily overlapping
    tags, review the stage log.

    FULL-BODY quotas spanning several contests (e.g. co-chairs + at-large
    seats counted toward one requirement) chain via:
      pre_elected:  {tag: n} — winners already elected in earlier contests
      later_seats:  seats still to be filled by later contests in the group
      later_supply: {tag: n} — tagged candidates available in those contests
    Feasibility then reasons about the whole body, so a minimum isn't
    force-fed into an early small contest that later contests can satisfy."""
    n_cands, seats, withdrawn, raw_ballots, names, title = parse_blt(blt_text)
    constraints = constraints or []
    cand_tags = {int(k): set(v) for k, v in (cand_tags or {}).items()}
    pre = {k: int(v) for k, v in (pre_elected or {}).items()}
    later_supply = {k: int(v) for k, v in (later_supply or {}).items()}
    later_seats = int(later_seats or 0)

    def has(c, tag):
        return tag in cand_tags.get(c, ())

    # a "paper" is [value, [remaining prefs]]; expand weights
    papers: dict[int, list] = {c: [] for c in range(1, n_cands + 1)}
    valid = 0
    for weight, ranks in raw_ballots:
        ranks = [r for r in ranks if r not in withdrawn]
        if not ranks:
            continue  # empty / abstain: not a valid paper, not in quota
        valid += weight
        for _ in range(weight):
            papers[ranks[0]].append([SCALE, ranks[1:]])

    quota = (valid // (seats + 1) + 1) * SCALE
    totals = {c: sum(p[0] for p in papers[c]) for c in papers}
    for c in withdrawn:
        totals.pop(c, None)
    state = {c: "continuing" for c in totals}
    nontransferable = 0
    history: list[dict] = []   # per-stage totals for tie-breaking
    stages: list[dict] = []
    elected_order: list[int] = []

    def snapshot(action: str):
        stages.append({
            "stage": len(stages) + 1,
            "action": action,
            "quota": quota / SCALE,
            "totals": {names[c - 1]: round(totals[c] / SCALE, 5) for c in sorted(totals)},
            "status": {names[c - 1]: state[c] for c in sorted(totals)},
            "nontransferable": round(nontransferable / SCALE, 5),
            "elected_so_far": [names[c - 1] for c in elected_order],
        })
        history.append(dict(totals))

    def elected_count(tag):
        return pre.get(tag, 0) + sum(1 for c in elected_order if has(c, tag))

    def can_elect(c):
        """Would electing c still allow a constraint-satisfying BODY?
        Seats/supply in later group contests count toward feasibility — EXCEPT
        for constraints marked ``local`` (e.g. 'at least one co-chair a non-cis
        man'), which must be satisfied WITHIN this contest and get no later
        relief."""
        for con in constraints:
            t = con["tag"]
            loc = con.get("local")
            s_rem = seats - len(elected_order) - 1 + (0 if loc else later_seats)
            if "max" in con and has(c, t) and elected_count(t) + 1 > con["max"]:
                return False
            if "min" in con:
                e = elected_count(t) + (1 if has(c, t) else 0)
                need = max(0, con["min"] - e)
                supply = sum(1 for x in totals
                             if state[x] == "continuing" and x != c and has(x, t)) \
                    + (0 if loc else later_supply.get(t, 0))
                if need > s_rem or need > supply:
                    return False
        return True

    def guarded_set():
        g = set()
        s_rem = seats - len(elected_order)
        continuing = [x for x in totals if state[x] == "continuing"]
        for con in constraints:
            t = con["tag"]
            if "min" in con:
                # later contests can cover part of the need; guard local
                # members only for the residue they alone can supply
                later_cap = 0 if con.get("local") else min(later_supply.get(t, 0), later_seats)
                need = max(0, con["min"] - elected_count(t) - later_cap)
                members = [x for x in continuing if has(x, t)]
                if need > 0 and need >= len(members):
                    g |= set(members)
            if "max" in con:
                room = con["max"] - elected_count(t)
                nonmembers = [x for x in continuing if not has(x, t)]
                if nonmembers and s_rem - room >= len(nonmembers):
                    g |= set(nonmembers)
        return g

    def con_label(c):
        for con in constraints:
            t = con["tag"]
            if "max" in con and has(c, t) and elected_count(t) + 1 > con["max"]:
                return con.get("label") or f"max {con['max']} {t}"
        return "; ".join(con.get("label") or con["tag"] for con in constraints)

    def declare_elected():
        newly = [c for c in totals if state[c] == "continuing" and totals[c] >= quota]
        for c in sorted(newly, key=lambda c: -totals[c]):
            if constraints and not can_elect(c):
                continue        # over quota but barred — doomed handling excludes it
            state[c] = "elected"
            elected_order.append(c)

    def tiebreak(cands: list[int], want_high: bool) -> int:
        """Rules 49(2)/51(2): earlier-stage totals; equal at all stages -> lot."""
        for past in reversed(history):
            vals = {c: past[c] for c in cands}
            best = max(vals.values()) if want_high else min(vals.values())
            tied = [c for c in cands if vals[c] == best]
            if len(tied) == 1:
                return tied[0]
            cands = tied
        return sorted(cands)[0]  # "by lot" — deterministic, documented

    def transfer(from_c: int, surplus: int | None):
        """surplus=None means exclusion (papers move at received value)."""
        nonlocal nontransferable
        total_c = totals[from_c]
        for value, prefs in papers[from_c]:
            if surplus is not None:
                value = (surplus * value) // total_c  # rule 48(3), truncated
            nxt = next((p for p in prefs if state.get(p) == "continuing"), None)
            if nxt is None:
                nontransferable += value
            else:
                k = prefs.index(nxt)
                papers[nxt].append([value, prefs[k + 1:]])
                totals[nxt] += value
        papers[from_c] = []
        totals[from_c] = quota if surplus is not None else 0
        if surplus is None:
            totals.pop(from_c)

    # ---- stage 1: first preferences (rule 45) ----
    declare_elected()
    snapshot("First preferences")

    while len(elected_order) < seats:
        continuing = [c for c in totals if state[c] == "continuing"]
        if not continuing:
            snapshot("No continuing candidates remain — seats left unfilled "
                     "under the stated constraints")
            break
        # constrained: exclude DOOMED candidates before anything else
        if constraints:
            doomed = [c for c in continuing if not can_elect(c)]
            if doomed:
                c = min(doomed, key=lambda x: totals[x])
                state[c] = "excluded"
                transfer(c, None)
                declare_elected()
                snapshot(f"{names[c - 1]} excluded — electing them would violate "
                         f"the quota requirement ({con_label(c)}); papers "
                         "transferred at received value")
                continue
        # rule 52: last vacancies
        if len(continuing) <= seats - len(elected_order):
            for c in sorted(continuing, key=lambda c: -totals[c]):
                state[c] = "elected"
                elected_order.append(c)
            snapshot("Remaining candidates elected to fill last vacancies (rule 52)")
            break
        # surpluses first, highest first (rule 49)
        surpluses = [c for c in totals if state[c] == "elected" and totals[c] > quota]
        if surpluses:
            hi = max(totals[c] for c in surpluses)
            c = tiebreak([x for x in surpluses if totals[x] == hi], want_high=True)
            total_before = totals[c]
            surplus = total_before - quota
            transfer(c, surplus)
            declare_elected()
            snapshot(f"Surplus votes of {names[c - 1]} transferred at value "
                     f"{fmt(surplus)}/{fmt(total_before)}")
            continue
        # otherwise exclude the lowest (rules 50, 51) — never a GUARDED candidate
        guarded = guarded_set() if constraints else set()
        excludable = [c for c in continuing if c not in guarded]
        if not excludable:
            # every continuing candidate is needed to satisfy the quotas
            for c in sorted(continuing, key=lambda c: -totals[c])[:seats - len(elected_order)]:
                state[c] = "elected"
                elected_order.append(c)
            snapshot("All continuing candidates are guarded by quota requirements "
                     "and are elected to the remaining seats")
            break
        lo = min(totals[c] for c in excludable)
        c = tiebreak([x for x in excludable if totals[x] == lo], want_high=False)
        state[c] = "excluded"
        transfer(c, None)
        declare_elected()
        guard_note = " (quota-guarded candidates were passed over)" \
            if guarded and any(totals[g] <= lo for g in guarded) else ""
        snapshot(f"{names[c - 1]} excluded ({fmt(lo)} votes); papers transferred "
                 f"at the value they were received{guard_note}")

    out = {
        "title": title,
        "method": "Scottish STV (SSI 2007/42)" + (" — constrained" if constraints else ""),
        "seats": seats,
        "valid_ballots": valid,
        "quota": quota / SCALE,
        "winners": [names[c - 1] for c in elected_order[:seats]],
        "stages": stages,
    }
    if constraints:
        out["constraints"] = [dict(con, elected=pre.get(con["tag"], 0) + sum(
            1 for c in elected_order[:seats] if has(c, con["tag"]))) for con in constraints]
    return out


ALT_METHODS = ("plurality", "approval", "borda", "irv", "meek", "mntv")
# Advanced/niche methods a few chapters use; surfaced separately so they're
# available for comparison without being encouraged as defaults.
ADVANCED_ALT_METHODS = ("schulze", "spav", "approval_stv")


def count_meek(blt_text: str, constraints=None, cand_tags=None,
               pre_elected=None, later_seats=0, later_supply=None):
    """Meek STV (the method used for New Zealand STV local elections and
    required for YDSA delegate elections): every candidate holds a 'keep
    factor' in [0,1]; each ballot flows down its rankings, each candidate
    keeping weight×factor and passing the rest on. Elected candidates'
    factors shrink iteratively until each holds exactly one (shrinking)
    quota — surpluses transfer continuously, and the quota falls as ballots
    exhaust. Returns the same result shape as count(), including a stage
    log. Supports the same quota-constraint and full-body-group parameters.

    Floating-point implementation with 1e-9 tolerances; exported BLTs can be
    cross-checked in OpaVote's Meek STV."""
    n_cands, seats, withdrawn, ballots, names, title = parse_blt(blt_text)
    active = [c for c in range(1, n_cands + 1) if c not in withdrawn]
    constraints = constraints or []
    cand_tags = {int(k): set(v) for k, v in (cand_tags or {}).items()}
    pre = {k: int(v) for k, v in (pre_elected or {}).items()}
    later_supply = {k: int(v) for k, v in (later_supply or {}).items()}
    later_seats = int(later_seats or 0)

    keep = {c: 1.0 for c in active}
    status = {c: "hopeful" for c in active}
    elected_order = []
    stages = []
    valid = sum(w for w, ranks in ballots if any(r in keep for r in ranks))
    votes, quota, excess = {c: 0.0 for c in active}, 0.0, 0.0

    def has(c, tag):
        return tag in cand_tags.get(c, ())

    def elected_count(tag):
        return pre.get(tag, 0) + sum(1 for c in elected_order if has(c, tag))

    def hopefuls():
        return [c for c in active if status[c] == "hopeful"]

    def can_elect(c):
        for con in constraints:
            t = con["tag"]
            loc = con.get("local")
            s_rem = seats - len(elected_order) - 1 + (0 if loc else later_seats)
            if "max" in con and has(c, t) and elected_count(t) + 1 > con["max"]:
                return False
            if "min" in con:
                e = elected_count(t) + (1 if has(c, t) else 0)
                need = max(0, con["min"] - e)
                supply = sum(1 for x in hopefuls() if x != c and has(x, t)) \
                    + (0 if loc else later_supply.get(t, 0))
                if need > s_rem or need > supply:
                    return False
        return True

    def guarded_set():
        g = set()
        s_rem = seats - len(elected_order)
        for con in constraints:
            t = con["tag"]
            if "min" in con:
                later_cap = 0 if con.get("local") else min(later_supply.get(t, 0), later_seats)
                need = max(0, con["min"] - elected_count(t) - later_cap)
                members = [x for x in hopefuls() if has(x, t)]
                if need > 0 and need >= len(members):
                    g |= set(members)
            if "max" in con:
                room = con["max"] - elected_count(t)
                nonmembers = [x for x in hopefuls() if not has(x, t)]
                if nonmembers and s_rem - room >= len(nonmembers):
                    g |= set(nonmembers)
        return g

    def snapshot(action):
        stages.append({
            "stage": len(stages) + 1,
            "action": action,
            "quota": round(quota, 5),
            "totals": {names[c - 1]: round(votes[c], 5) for c in active
                       if status[c] != "excluded"},
            "status": {names[c - 1]: ("elected" if status[c] == "elected"
                                      else "continuing") for c in active
                       if status[c] != "excluded"},
            "nontransferable": round(excess, 5),
            "elected_so_far": [names[c - 1] for c in elected_order],
        })

    def converge():
        nonlocal votes, quota, excess
        newly = []
        for _ in range(1000):
            votes = {c: 0.0 for c in active}
            excess = 0.0
            for w, ranks in ballots:
                weight = float(w)
                rs = [r for r in ranks if r in votes and status[r] != "excluded"]
                for r in rs:
                    kept = weight * keep[r]
                    votes[r] += kept
                    weight -= kept
                    if weight <= 1e-12:
                        break
                if rs:
                    excess += weight
            quota = max((valid - excess) / (seats + 1), 1e-9)
            changed = False
            for c in sorted(active, key=lambda c: -votes[c]):
                if status[c] == "hopeful" and votes[c] >= quota - 1e-9 \
                        and len(elected_order) < seats and can_elect(c):
                    status[c] = "elected"
                    elected_order.append(c)
                    newly.append(c)
                    changed = True
            for c in elected_order:
                if votes[c] > quota and votes[c] > 0:
                    nk = keep[c] * quota / votes[c]
                    if abs(nk - keep[c]) > 1e-10:
                        keep[c] = nk
                        changed = True
            if not changed:
                break
        return newly

    guard = 0
    while guard < 400:
        guard += 1
        newly = converge()
        if newly:
            snapshot("Keep factors converged; "
                     + ", ".join(names[c - 1] for c in newly)
                     + " reached the quota and "
                     + ("is" if len(newly) == 1 else "are") + " elected")
        h = hopefuls()
        if len(elected_order) >= seats or not h:
            break
        doomed = [c for c in h if not can_elect(c)]
        if doomed:
            c = min(doomed, key=lambda x: (votes[x], x))
            status[c] = "excluded"
            snapshot(f"{names[c - 1]} excluded — electing them would violate a "
                     "quota requirement; their ballots flow onward")
            continue
        if len(elected_order) + len(h) <= seats:
            for c in sorted(h, key=lambda c: -votes[c]):
                status[c] = "elected"
                elected_order.append(c)
            snapshot("Remaining candidates elected to fill the last vacancies")
            break
        guarded = guarded_set()
        excludable = [c for c in h if c not in guarded]
        if not excludable:
            for c in sorted(h, key=lambda c: -votes[c])[:seats - len(elected_order)]:
                status[c] = "elected"
                elected_order.append(c)
            snapshot("All continuing candidates are guarded by quota requirements "
                     "and are elected to the remaining seats")
            break
        c = min(excludable, key=lambda x: (votes[x], x))
        status[c] = "excluded"
        snapshot(f"{names[c - 1]} excluded (lowest, {votes[c]:.5f} votes); "
                 "their ballots flow to each voter's next choice")

    out = {
        "title": title,
        "method": "Meek STV (NZ rules)" + (" — constrained" if constraints else ""),
        "seats": seats,
        "valid_ballots": valid,
        "quota": round(quota, 5),
        "winners": [names[c - 1] for c in elected_order[:seats]],
        "stages": stages,
    }
    if constraints:
        out["constraints"] = [dict(con, elected=pre.get(con["tag"], 0) + sum(
            1 for c in elected_order[:seats] if has(c, con["tag"])))
            for con in constraints]
    return out


SCORE_METHODS = ("score", "star", "star_pr")


def parse_scores(text: str, max_score: int | None = None):
    """Parse a SCORE ballot file. Same envelope as BLT but each ballot lists
    ``candidate:score`` pairs instead of a ranking:

        <n_candidates> <n_seats> [max_score]      (max_score default 2)
        <weight> 1:2 2:0 3:1 ... 0                 (unlisted candidate => 0)
        ...
        0
        "Name 1"
        ...
        "Title"

    Returns (n_cands, n_seats, max_score, ballots, names, title) where each
    ballot is (weight, {cand_index_1based: score})."""
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    head = lines[0].split()
    n_cands, n_seats = int(head[0]), int(head[1])
    file_max = int(head[2]) if len(head) > 2 else 2
    max_s = int(max_score) if max_score else file_max
    i = 1
    ballots = []
    while lines[i] != "0":
        parts = lines[i].split()
        weight = int(parts[0])
        scores = {}
        for tok in parts[1:]:
            if tok == "0":
                break
            if ":" not in tok:
                continue
            c, s = tok.split(":", 1)
            ci, si = int(c), max(0, min(max_s, int(s)))
            if 1 <= ci <= n_cands:
                scores[ci] = si
        ballots.append((weight, scores))
        i += 1
    i += 1
    quoted = re.findall(r'"([^"]*)"', "\n".join(lines[i:]))
    names = quoted[:n_cands]
    title = quoted[n_cands] if len(quoted) > n_cands else "Untitled"
    return n_cands, n_seats, max_s, ballots, names, title


def _score_totals(ballots, n_cands, max_s):
    """Weighted per-candidate (sum of scores, number of top-score ballots)."""
    totals = {c: 0 for c in range(1, n_cands + 1)}
    tops = {c: 0 for c in range(1, n_cands + 1)}
    valid = 0
    for w, scores in ballots:
        if not scores:
            continue
        valid += w
        for c, s in scores.items():
            totals[c] += w * s
            if s == max_s:
                tops[c] += w
    return totals, tops, valid


def _score_select(order, seats, constraints, cand_tags,
                  pre_elected, later_seats, later_supply):
    """Greedy selection down a pre-sorted ``order`` of candidate indices,
    honouring diversity quotas exactly as the DSA at-large score rules
    describe: '…the eligible candidate with the next highest score shall be
    declared elected' when a max would be violated, and seats are reserved so
    a minimum can never be stranded. Same guarded/doomed feasibility as the
    STV counts. Returns (elected_list, skipped[(cand, reason)])."""
    pre = {k: int(v) for k, v in (pre_elected or {}).items()}
    later_supply = {k: int(v) for k, v in (later_supply or {}).items()}
    later_seats = int(later_seats or 0)
    tags = {int(k): set(v) for k, v in (cand_tags or {}).items()}
    cons = constraints or []

    def has(c, t):
        return t in tags.get(c, ())

    elected = []
    skipped = []

    def elected_count(t):
        return pre.get(t, 0) + sum(1 for c in elected if has(c, t))

    def can_elect(c):
        s_rem = seats - len(elected) - 1 + later_seats
        avail = [x for x in order if x != c and x not in elected]
        for con in cons:
            t = con["tag"]
            if "max" in con and has(c, t) and elected_count(t) + 1 > con["max"]:
                return False, (con.get("label") or f"max {con['max']} {t}")
            if "min" in con:
                e = elected_count(t) + (1 if has(c, t) else 0)
                need = max(0, con["min"] - e)
                supply = sum(1 for x in avail if has(x, t)) + later_supply.get(t, 0)
                if need > s_rem or need > supply:
                    return False, (con.get("label") or f"reserved for min {con['min']} {t}")
        return True, ""

    changed = True
    while len(elected) < seats and changed:
        changed = False
        for c in order:
            if c in elected:
                continue
            ok, why = can_elect(c)
            if ok:
                elected.append(c)
                changed = True
                if len(elected) >= seats:
                    break
            elif not any(s[0] == c for s in skipped):
                skipped.append((c, why))
    return elected, skipped


def count_score(text: str, constraints=None, cand_tags=None, max_score=None,
                pre_elected=None, later_seats=0, later_supply=None):
    """Score voting (a.k.a. Scored / Range voting): each voter gives every
    candidate a score 0..max_score; the highest total scores win. Used by DSA
    for at-large convention-delegate elections (0=disapprove, 1=neutral,
    2=approve). Supports the same diversity-quota parameters as the STV
    counts, and the statutory tie-breaks: ties are broken by the number of
    top-score ('approve') ballots, then by lot. Returns a result shape close
    to the STV counts, with a per-candidate score table and a single-stage
    log so it renders on the same results page."""
    n_cands, seats, max_s, ballots, names, title = parse_scores(text, max_score)
    totals, tops, valid = _score_totals(ballots, n_cands, max_s)
    # sort: highest total, then most top-scores, then by lot (deterministic id)
    order = sorted(range(1, n_cands + 1), key=lambda c: (-totals[c], -tops[c], c))
    elected, skipped = _score_select(order, seats, constraints, cand_tags,
                                     pre_elected, later_seats, later_supply)
    tags = {int(k): set(v) for k, v in (cand_tags or {}).items()}

    def has(c, t):
        return t in tags.get(c, ())

    action = "Scores totalled; highest totals elected"
    if skipped:
        action += " (candidates barred by a quota were passed over for the next highest score)"
    stage = {
        "stage": 1, "action": action, "quota": None,
        "totals": {names[c - 1]: totals[c] for c in order},
        "tops": {names[c - 1]: tops[c] for c in order},
        "status": {names[c - 1]: ("elected" if c in elected else "continuing")
                   for c in order},
        "elected_so_far": [names[c - 1] for c in elected],
    }
    out = {
        "title": title,
        "method": f"Score voting (0–{max_s})" + (" — constrained" if constraints else ""),
        "seats": seats, "max_score": max_s, "valid_ballots": valid, "quota": None,
        "winners": [names[c - 1] for c in elected[:seats]],
        "scores": {names[c - 1]: totals[c] for c in range(1, n_cands + 1)},
        "top_scores": {names[c - 1]: tops[c] for c in range(1, n_cands + 1)},
        "stages": [stage],
    }
    if constraints:
        out["constraints"] = [dict(con, elected=(pre_elected or {}).get(con["tag"], 0) + sum(
            1 for c in elected[:seats] if has(c, con["tag"]))) for con in (constraints or [])]
    return out


def _pairwise(ballots, a, b):
    """Voters preferring a over b, and b over a (equal scores = no preference)."""
    pa = pb = 0
    for w, scores in ballots:
        sa, sb = scores.get(a, 0), scores.get(b, 0)
        if sa > sb:
            pa += w
        elif sb > sa:
            pb += w
    return pa, pb


def count_star(text: str, constraints=None, cand_tags=None, max_score=None,
               pre_elected=None, later_seats=0, later_supply=None):
    """STAR voting — Score Then Automatic Runoff (starvoting.org). Round 1
    scores every candidate; the two highest advance to an automatic runoff
    where each ballot supports whichever finalist it scored higher (equal
    scores express no preference). The runoff winner is elected. For more
    than one seat this runs as BLOC STAR — the winner is removed from every
    ballot and the process repeats — which is majoritarian, NOT proportional;
    use STAR-PR (count_star_pr, the Allocated Score method) or STV/Meek for a
    proportional multi-winner contest.
    Honours the same diversity-quota parameters (feasible finalists only)."""
    n_cands, seats, max_s, ballots, names, title = parse_scores(text, max_score)
    tags = {int(k): set(v) for k, v in (cand_tags or {}).items()}
    pre = {k: int(v) for k, v in (pre_elected or {}).items()}
    later_supply = {k: int(v) for k, v in (later_supply or {}).items()}
    later_seats = int(later_seats or 0)
    cons = constraints or []

    def has(c, t):
        return t in tags.get(c, ())

    elected = []
    remaining = set(range(1, n_cands + 1))
    _, _, valid = _score_totals(ballots, n_cands, max_s)
    stages = []

    def elected_count(t):
        return pre.get(t, 0) + sum(1 for c in elected if has(c, t))

    def can_elect(c, pool):
        s_rem = seats - len(elected) - 1 + later_seats
        avail = [x for x in pool if x != c]
        for con in cons:
            t = con["tag"]
            if "max" in con and has(c, t) and elected_count(t) + 1 > con["max"]:
                return False
            if "min" in con:
                e = elected_count(t) + (1 if has(c, t) else 0)
                need = max(0, con["min"] - e)
                supply = sum(1 for x in avail if has(x, t)) + later_supply.get(t, 0)
                if need > s_rem or need > supply:
                    return False
        return True

    while len(elected) < seats and remaining:
        totals = {c: 0 for c in remaining}
        tops = {c: 0 for c in remaining}
        for w, scores in ballots:
            for c in remaining:
                s = scores.get(c, 0)
                totals[c] += w * s
                if s == max_s:
                    tops[c] += w
        # Official STAR Tiebreaker Protocol (starvoting.org / Equal Vote):
        # SCORING ties (who advances) break by head-to-head preference, then by
        # more max-score ballots, then by lot. RUNOFF ties (equal preference)
        # break by higher score, then more max-score ballots, then by lot.
        def scoring_cmp(a, b):
            if totals[a] != totals[b]:
                return -1 if totals[a] > totals[b] else 1
            pa, pb = _pairwise(ballots, a, b)          # preferred head-to-head
            if pa != pb:
                return -1 if pa > pb else 1
            if tops[a] != tops[b]:                     # more five-star ballots
                return -1 if tops[a] > tops[b] else 1
            return -1 if a < b else (1 if a > b else 0)  # documented lot
        order = sorted(remaining, key=cmp_to_key(scoring_cmp))
        feasible = [c for c in order if can_elect(c, remaining)] or order
        finalists = feasible[:2]
        tie_note = ""
        if len(finalists) == 1:
            win = finalists[0]
            runoff = {win: valid, "info": "only one candidate remained"}
        else:
            a, b = finalists[0], finalists[1]
            pa, pb = _pairwise(ballots, a, b)
            if pa > pb:
                win = a
            elif pb > pa:
                win = b
            else:                                       # runoff tie
                if totals[a] != totals[b]:
                    win = a if totals[a] > totals[b] else b
                    tie_note = " (runoff tie broken by higher score)"
                elif tops[a] != tops[b]:
                    win = a if tops[a] > tops[b] else b
                    tie_note = " (runoff tie broken by more max-score ballots)"
                else:
                    win = a if a < b else b
                    tie_note = " (true tie broken by documented lot)"
            runoff = {names[a - 1]: pa, names[b - 1]: pb}
        elected.append(win)
        remaining.discard(win)
        stages.append({
            "stage": len(stages) + 1,
            "action": f"Score round → runoff; {names[win - 1]} elected" + tie_note,
            "quota": None,
            "totals": {names[c - 1]: totals[c] for c in order},
            "tops": {names[c - 1]: tops[c] for c in order},
            "runoff": {(k if k == "info" else k): v for k, v in runoff.items()},
            "finalists": [names[c - 1] for c in finalists],
            "status": {names[c - 1]: ("elected" if c == win else "continuing")
                       for c in order},
            "elected_so_far": [names[c - 1] for c in elected],
        })

    out = {
        "title": title,
        "method": "STAR voting (Score Then Automatic Runoff)"
                  + (" — Bloc STAR" if seats > 1 else "")
                  + (" — constrained" if constraints else ""),
        "seats": seats, "max_score": max_s, "valid_ballots": valid, "quota": None,
        "winners": [names[c - 1] for c in elected[:seats]],
        "scores": stages[0]["totals"] if stages else {},
        "stages": stages,
    }
    if constraints:
        out["constraints"] = [dict(con, elected=pre.get(con["tag"], 0) + sum(
            1 for c in elected[:seats] if has(c, con["tag"]))) for con in cons]
    return out


def count_star_pr(text: str, constraints=None, cand_tags=None, max_score=None,
                  pre_elected=None, later_seats=0, later_supply=None):
    """STAR-PR — Proportional STAR via the ALLOCATED SCORE method (the
    proportional multi-winner method used by the Equal Vote Coalition and
    implemented as the default multi-winner mode in larryhastings/starvote).
    Unlike Bloc STAR (majoritarian), this gives a cohesive minority its
    proportional share of seats:

      quota = total ballot weight / seats  (Hare)
      each round:
        * elect the highest total-score candidate (skipping any barred by a
          diversity quota);
        * 'spend' exactly one quota of ballot weight, taken from the ballots
          that scored the winner highest — full tiers above the split score
          are zeroed, the boundary tier is deweighted proportionally so
          exactly a quota is removed;
      repeat until every seat is filled.

    Ballots that never scored a winner keep full weight, so distinct factions
    each elect their own candidates. Returns the same result shape as the
    other counts (single stage log entry per seat)."""
    n_cands, seats, max_s, ballots, names, title = parse_scores(text, max_score)
    tags = {int(k): set(v) for k, v in (cand_tags or {}).items()}
    pre = {k: int(v) for k, v in (pre_elected or {}).items()}
    later_supply = {k: int(v) for k, v in (later_supply or {}).items()}
    later_seats = int(later_seats or 0)
    cons = constraints or []

    def has(c, t):
        return t in tags.get(c, ())

    bl = [[float(w), scores] for w, scores in ballots if scores]  # running weights
    valid = sum(w for w, scores in ballots if scores)
    total_w = sum(b[0] for b in bl)
    quota = total_w / seats if seats else 0.0
    elected, stages = [], []
    remaining = set(range(1, n_cands + 1))

    def elected_count(t):
        return pre.get(t, 0) + sum(1 for c in elected if has(c, t))

    def can_elect(c):
        s_rem = seats - len(elected) - 1 + later_seats
        avail = [x for x in remaining if x != c]
        for con in cons:
            t = con["tag"]
            if "max" in con and has(c, t) and elected_count(t) + 1 > con["max"]:
                return False
            if "min" in con:
                e = elected_count(t) + (1 if has(c, t) else 0)
                need = max(0, con["min"] - e)
                supply = sum(1 for x in avail if has(x, t)) + later_supply.get(t, 0)
                if need > s_rem or need > supply:
                    return False
        return True

    while len(elected) < seats and remaining:
        totals = {c: 0.0 for c in remaining}
        for w, scores in bl:
            if w <= 0:
                continue
            for c in remaining:
                totals[c] += w * scores.get(c, 0)
        order = sorted(remaining, key=lambda c: (-totals[c], c))
        feasible = [c for c in order if can_elect(c)] or order
        win = feasible[0]
        elected.append(win)
        remaining.discard(win)
        # spend one quota from ballots that scored the winner, highest first
        scored = [b for b in bl if b[0] > 0 and b[1].get(win, 0) > 0]
        tier_w = {}
        for b in scored:
            sv = b[1][win]
            tier_w[sv] = tier_w.get(sv, 0.0) + b[0]
        cum, split = 0.0, None
        for sv in sorted(tier_w, reverse=True):
            if cum + tier_w[sv] >= quota:
                split = sv
                break
            cum += tier_w[sv]
        if split is None:                      # fewer than a quota scored win
            for b in scored:
                b[0] = 0.0
            spent = cum
        else:
            need = quota - cum
            frac = (need / tier_w[split]) if tier_w[split] > 0 else 0.0
            for b in scored:
                sv = b[1][win]
                if sv > split:
                    b[0] = 0.0
                elif sv == split:
                    b[0] = b[0] * (1.0 - frac)
            spent = quota
        stages.append({
            "stage": len(stages) + 1,
            "action": f"{names[win - 1]} elected (highest score); one quota "
                      f"({quota:.2f}) of supporting ballot weight spent",
            "quota": round(quota, 5),
            "totals": {names[c - 1]: round(totals[c], 5) for c in order},
            "status": {names[c - 1]: ("elected" if c == win else "continuing")
                       for c in order},
            "spent": round(spent, 5),
            "elected_so_far": [names[c - 1] for c in elected],
        })

    out = {
        "title": title,
        "method": "STAR-PR (Allocated Score, proportional)"
                  + (" — constrained" if constraints else ""),
        "seats": seats, "max_score": max_s, "valid_ballots": valid,
        "quota": round(quota, 5),
        "winners": [names[c - 1] for c in elected[:seats]],
        "scores": stages[0]["totals"] if stages else {},
        "stages": stages,
    }
    if constraints:
        out["constraints"] = [dict(con, elected=pre.get(con["tag"], 0) + sum(
            1 for c in elected[:seats] if has(c, con["tag"]))) for con in cons]
    return out


def count_alternative(blt_text: str, method: str):
    """PREVIEW counts under other rules — for comparing how an election
    would have turned out, never for certification:
      plurality — SNTV: each ballot counts once, for its first choice
      approval  — every candidate ranked anywhere on a ballot is 'approved'
      borda     — descending points by rank (m-1, m-2, …, 0)
      irv       — sequential instant-runoff (San Francisco-style RCV for one
                  seat; for multi-seat contests winners are elected one at a
                  time and removed — NOT proportional like STV)"""
    n_cands, seats, withdrawn, ballots, names, title = parse_blt(blt_text)
    active = [c for c in range(1, n_cands + 1) if c not in withdrawn]
    scores = {c: 0 for c in active}

    if method == "plurality":
        for w, ranks in ballots:
            rs = [r for r in ranks if r in scores]
            if rs:
                scores[rs[0]] += w
        note = ("Plurality / SNTV: each ballot counts only for its first choice; "
                "the top vote-getters win. No transfers, not proportional.")
        winners = sorted(active, key=lambda c: (-scores[c], c))[:seats]
    elif method == "approval":
        for w, ranks in ballots:
            for r in set(r for r in ranks if r in scores):
                scores[r] += w
        note = ("Approval preview: every candidate a voter ranked (at any position) "
                "counts as approved. Ranking order is ignored.")
        winners = sorted(active, key=lambda c: (-scores[c], c))[:seats]
    elif method == "borda":
        m = len(active)
        for w, ranks in ballots:
            rs = [r for r in ranks if r in scores]
            for i, r in enumerate(rs):
                scores[r] += w * (m - 1 - i)
        note = (f"Borda count: a first choice is worth {m - 1} points, each lower "
                "rank one point less; unranked candidates score 0.")
        winners = sorted(active, key=lambda c: (-scores[c], c))[:seats]
    elif method == "mntv":
        # Multiple Non-Transferable Vote (block plurality / bloc voting):
        # each voter casts up to `seats` equal votes — here their top `seats`
        # ranked candidates — and the `seats` highest vote-getters win.
        for w, ranks in ballots:
            rs = [r for r in ranks if r in scores][:seats]
            for r in rs:
                scores[r] += w
        note = ("MNTV / block plurality: each voter casts up to as many equal votes "
                "as there are seats (their top preferences), and the highest "
                "vote-getters fill the seats. Simple and familiar, but majoritarian — "
                "a coordinated majority can sweep every seat. Not proportional.")
        winners = sorted(active, key=lambda c: (-scores[c], c))[:seats]
    elif method == "irv":
        for w, ranks in ballots:            # display scores = first preferences
            rs = [r for r in ranks if r in scores]
            if rs:
                scores[rs[0]] += w
        winners = []
        remaining = set(active)
        guard = 0
        while len(winners) < seats and remaining and guard < 200:
            cont = set(remaining)
            while guard < 200:
                guard += 1
                tallies = {c: 0 for c in cont}
                total = 0
                for w, ranks in ballots:
                    rs = [r for r in ranks if r in cont]
                    if rs:
                        tallies[rs[0]] += w
                        total += w
                if not total:
                    break
                top = max(cont, key=lambda c: (tallies[c], -c))
                if tallies[top] * 2 > total or len(cont) == 1:
                    winners.append(top)
                    remaining.discard(top)
                    break
                cont.discard(min(cont, key=lambda c: (tallies[c], c)))
            else:
                break
            if not total:
                break
        note = ("Sequential instant-runoff (San Francisco-style RCV): a winner needs a "
                "majority of continuing ballots, lowest candidates are eliminated one at "
                "a time; for multiple seats, each winner is removed and the runoff "
                "repeats. Majoritarian — NOT proportional like STV.")
    elif method == "meek":
        res = count_meek(blt_text)
        final = res["stages"][-1]["totals"] if res["stages"] else {}
        name_idx = {names[c - 1]: c for c in active}
        note = ("Meek STV (New Zealand rules): iterative keep factors let surpluses "
                "flow continuously and the quota shrink as ballots exhaust. Available "
                "as an OFFICIAL counting method per contest (\"method\": \"meek\") — "
                f"required for YDSA delegate elections. Final quota {res['quota']}.")
        scores = {name_idx[n]: v for n, v in final.items() if n in name_idx}
        for c in active:
            scores.setdefault(c, 0)
        winners = [name_idx[w] for w in res["winners"] if w in name_idx]
    elif method == "schulze":
        # Schulze (a Condorcet method). Ranked ballots become pairwise
        # preferences; unranked candidates sit below every ranked one and tie
        # with each other. Strongest beatpaths (Floyd-Warshall) then order the
        # candidates; the top `seats` win. Single-winner Condorcet by design —
        # offered only for comparison, NOT proportional.
        idx = {c: i for i, c in enumerate(active)}
        m = len(active)
        d = [[0] * m for _ in range(m)]
        for w, ranks in ballots:
            rs = [r for r in ranks if r in scores]
            pos = {r: p for p, r in enumerate(rs)}          # lower pos = preferred
            for a in active:
                for b in active:
                    if a == b:
                        continue
                    pa = pos.get(a); pb = pos.get(b)
                    # a preferred to b if ranked higher, or ranked while b isn't
                    if pa is not None and (pb is None or pa < pb):
                        d[idx[a]][idx[b]] += w
        p = [[0] * m for _ in range(m)]
        for i in range(m):
            for j in range(m):
                if i != j and d[i][j] > d[j][i]:
                    p[i][j] = d[i][j]
        for i in range(m):
            for j in range(m):
                if i == j:
                    continue
                for k in range(m):
                    if i == k or j == k:
                        continue
                    p[j][k] = max(p[j][k], min(p[j][i], p[i][k]))
        wins = {c: sum(1 for c2 in active
                       if c2 != c and p[idx[c]][idx[c2]] > p[idx[c2]][idx[c]])
                for c in active}
        scores = wins
        winners = sorted(active, key=lambda c: (-wins[c], c))[:seats]
        note = ("Schulze (Condorcet): pairwise beatpaths pick the candidate who "
                "would beat every other head-to-head. A single-winner method — "
                "for multiple seats it just repeats the ordering and is NOT "
                "proportional. Advanced / comparison only.")
    elif method == "spav":
        # Sequential Proportional Approval Voting. Approval ballots (every
        # ranked candidate = approved); each round a ballot's weight is
        # w / (1 + already-elected approvals on it), so groups that already won
        # seats have diminishing pull. Proportional over approvals, but ignores
        # ranking order.
        approvals = [(w, set(r for r in ranks if r in scores)) for w, ranks in ballots]
        raw = {c: 0 for c in active}
        for w, appr in approvals:
            for c in appr:
                raw[c] += w
        scores = raw
        winners = []
        elected = set()
        while len(winners) < seats and len(winners) < len(active):
            tally = {c: 0.0 for c in active if c not in elected}
            for w, appr in approvals:
                nwon = len(appr & elected)
                wt = w / (1.0 + nwon)
                for c in appr:
                    if c not in elected:
                        tally[c] += wt
            if not any(tally.values()):
                break
            pick = max(tally, key=lambda c: (tally[c], -c))
            winners.append(pick)
            elected.add(pick)
        note = ("Sequential Proportional Approval (SPAV / Thiele): treats each "
                "ranked candidate as an approval; after each seat, ballots that "
                "already elected someone are down-weighted by 1/(1+wins). "
                "Proportional over approvals but blind to ranking order. "
                "Advanced / comparison only.")
    elif method == "approval_stv":
        # Approval with a Droop threshold. Approval counts stand in for votes;
        # any candidate reaching the Droop quota is elected outright, then the
        # highest remaining approvals fill the rest. A blunt, non-transferable
        # approximation of STV's quota logic.
        total = sum(w for w, _ in ballots)
        for w, ranks in ballots:
            for r in set(r for r in ranks if r in scores):
                scores[r] += w
        quota = (total // (seats + 1)) + 1 if seats else 0
        by_appr = sorted(active, key=lambda c: (-scores[c], c))
        met = [c for c in by_appr if scores[c] >= quota]
        winners = met[:seats]
        for c in by_appr:
            if len(winners) >= seats:
                break
            if c not in winners:
                winners.append(c)
        note = (f"Approval-STV threshold: approvals count as votes and the Droop "
                f"quota is {quota}; candidates meeting it are elected, then the "
                "highest approvals fill any remaining seats. No vote transfers, "
                "so it is only a rough approximation of STV. Advanced / "
                "comparison only.")
    else:
        raise ValueError(f"unknown method {method!r} (use one of "
                         f"{ALT_METHODS + ADVANCED_ALT_METHODS})")

    return {"title": title, "method": method, "note": note, "seats": seats,
            "winners": [names[c - 1] for c in winners],
            "scores": {names[c - 1]: scores[c] for c in active}}


def print_table(result: dict):
    print(f"\n{result['title']}")
    print(f"Method: {result['method']}  ·  Seats: {result['seats']}  ·  "
          f"Valid ballots: {result['valid_ballots']}  ·  Quota: {result['quota']:g}\n")
    cands = list(result["stages"][0]["totals"].keys())
    w = max(len(c) for c in cands + ["(non-transferable)"]) + 2
    for st in result["stages"]:
        print(f"Stage {st['stage']}: {st['action']}")
        for c in cands:
            if c not in st["totals"] and all(c not in s["totals"] for s in result["stages"][st["stage"] - 1:]):
                continue
            v = st["totals"].get(c)
            mark = {"elected": " ✓ ELECTED", "excluded": " ✗ excluded"}.get(st["status"].get(c, ""), "")
            print(f"  {c:<{w}} {('—' if v is None else f'{v:g}'):>12}{mark}")
        print(f"  {'(non-transferable)':<{w}} {st['nontransferable']:>12g}\n")
    print("WINNERS (in order elected): " + ", ".join(result["winners"]))


def _is_score_file(text: str) -> bool:
    """A score file carries ``candidate:score`` tokens; BLT never does."""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line[0] in "0-" or line.startswith('"'):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():   # header "<n> <seats>"
            continue
        return any(":" in tok for tok in parts[1:])
    return False


def print_score_table(result: dict):
    print(f"\n{result['title']}")
    print(f"Method: {result['method']}  ·  Seats: {result['seats']}  ·  "
          f"Valid ballots: {result['valid_ballots']}\n")
    st = result["stages"][-1] if result["stages"] else {}
    totals = st.get("totals", result.get("scores", {}))
    tops = st.get("tops", result.get("top_scores", {}))
    w = max([len(c) for c in totals] + [4]) + 2
    for name, tot in sorted(totals.items(), key=lambda kv: -kv[1]):
        mark = " ✓ ELECTED" if name in result["winners"] else ""
        t2 = f"  ({tops.get(name, 0)} top)" if tops else ""
        print(f"  {name:<{w}} {tot:>10}{t2}{mark}")
    print("\nWINNERS (in order elected): " + ", ".join(result["winners"]))


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    star = "--star" in args
    starpr = "--starpr" in args
    files = [a for a in args if not a.startswith("--")]
    if not files:
        sys.exit("usage: stv_tabulate.py ballots.blt [--json] [--star|--starpr]")
    text = open(files[0], encoding="utf-8").read()
    if _is_score_file(text):
        result = (count_star_pr(text) if starpr
                  else count_star(text) if star else count_score(text))
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print_score_table(result)
    else:
        result = count(text)
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print_table(result)
