#!/usr/bin/env python3
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


def count(blt_text: str):
    n_cands, seats, withdrawn, raw_ballots, names, title = parse_blt(blt_text)

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

    def declare_elected():
        newly = [c for c in totals if state[c] == "continuing" and totals[c] >= quota]
        for c in sorted(newly, key=lambda c: -totals[c]):
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
            surplus = totals[c] - quota
            transfer(c, surplus)
            declare_elected()
            snapshot(f"Surplus of {names[c - 1]} ({fmt(surplus)}) transferred")
            continue
        # otherwise exclude the lowest (rules 50, 51)
        lo = min(totals[c] for c in continuing)
        c = tiebreak([x for x in continuing if totals[x] == lo], want_high=False)
        state[c] = "excluded"
        transfer(c, None)
        declare_elected()
        snapshot(f"{names[c - 1]} excluded, papers transferred at received value")

    return {
        "title": title,
        "method": "Scottish STV (SSI 2007/42)",
        "seats": seats,
        "valid_ballots": valid,
        "quota": quota / SCALE,
        "winners": [names[c - 1] for c in elected_order[:seats]],
        "stages": stages,
    }


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


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    files = [a for a in args if not a.startswith("--")]
    if not files:
        sys.exit("usage: stv_tabulate.py ballots.blt [--json]")
    result = count(open(files[0], encoding="utf-8").read())
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print_table(result)
