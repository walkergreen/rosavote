"""
Export five-question ballots to BLT files for external verification.

Reads the `answers` JSON produced by the vote service / build_chain.py:
    {"q1": "YES|NO|ABSTAIN",
     "q2": ["IE","COORD",...] or ["ABSTAIN"],     # ranked, Scottish STV
     "q3": ["DEBS","WILSON",...] or ["ABSTAIN"],  # ranked, Scottish STV
     "pledges": [...]}                            # not a contest; not exported

Emits one BLT per contest so any independent tabulator (OpaVote, OpenSTV,
Droop, stv_tabulate.py) can reproduce the published result:
    <prefix>_q1.blt  - Yes/No contest (abstentions excluded, reported separately)
    <prefix>_q2.blt  - campaign structure, Scottish STV, 1 seat
    <prefix>_q3.blt  - the 1912 field, Scottish STV, 1 seat

Ranked-question abstentions are written as EMPTY ballots ("1 0") - they count
toward turnout in the file but exhaust immediately under STV, matching the
adopted rule that abstention affects quorum, never the quota or result.

Usage:
    python make_blt.py --from-csv ballots.csv --out-prefix chapter_nyc
    python make_blt.py --from-firestore --project dsa-org-tools \
        --poll-id debs_endorsement__nyc --out-prefix chapter_nyc
"""

import argparse
import csv
import json

Q1 = {
    "candidates": ["YES", "NO"],
    "labels": {"YES": "Yes", "NO": "No"},
    "seats": 1,
    "title": "DSA Chapter Member Ballot - Q1 - Endorse Eugene V. Debs?",
}
Q2 = {
    "candidates": ["IE", "COORD", "NOEND", "NOTA"],
    "labels": {
        "IE": "Independent expenditure", "COORD": "Coordinated campaign",
        "NOEND": "No endorsement", "NOTA": "None of the above",
    },
    "seats": 1,
    "title": "DSA Chapter Member Ballot - Q2 - Campaign structure (Scottish STV)",
}
Q3 = {
    "candidates": ["DEBS", "WILSON", "ROOSEVELT", "TAFT", "NOTA2"],
    "labels": {
        "DEBS": "Eugene V. Debs", "WILSON": "Woodrow Wilson",
        "ROOSEVELT": "Theodore Roosevelt", "TAFT": "William Howard Taft",
        "NOTA2": "None of the above",
    },
    "seats": 1,
    "title": "DSA Chapter Member Ballot - Q3 - The 1912 field (Scottish STV)",
}


# Per-chapter contests — built-in FALLBACK only. When run with
# --from-firestore, refresh_registry_from_firestore() overlays these from
# config__polls (the same source of truth the vote service reads), so the
# old keep-in-sync-with-CHAPTERS gotcha only applies to offline CSV runs.
Q6_TITLES = {
    "debs_endorsement__nyc": "NYC - Charter a fourth branch in the Bronx?",
    "debs_endorsement__chi": "Chicago - Permanent chapter office in Pilsen?",
    "debs_endorsement__dc": "Metro DC - Fund childcare at general meetings?",
    "debs_endorsement__la": "DSA-LA - Launch a tenant-organizing school?",
    "debs_endorsement__atlarge": "At-Large - Charter a national At-Large Organizing Committee?",
}
Q8_TITLES = {
    "debs_endorsement__nyc": "NYC - Block the Brooklyn waterfront data center?",
    "debs_endorsement__chi": "Chicago - Fund a Starbucks Workers United solidarity committee?",
    "debs_endorsement__dc": "Metro DC - Campaign to make Metrobus fare-free?",
    "debs_endorsement__la": "DSA-LA - Campaign for a citywide rent freeze?",
    "debs_endorsement__atlarge": "At-Large - National campaign against data-center utility rate hikes?",
}
Q7_CONTESTS = {
    "debs_endorsement__nyc": {"seats": 4, "alternates": 1,
        "candidates": ["LONDON", "FLYNN", "GOLDMAN", "JONES", "DELEON", "SCHNEIDERMAN", "RANDOLPH"],
        "labels": {"LONDON": "Meyer London", "FLYNN": "Elizabeth Gurley Flynn", "GOLDMAN": "Emma Goldman",
                   "JONES": "Claudia Jones", "DELEON": "Daniel De Leon",
                   "SCHNEIDERMAN": "Rose Schneiderman", "RANDOLPH": "A. Philip Randolph"}},
    "debs_endorsement__chi": {"seats": 2, "alternates": 1,
        "candidates": ["LPARSONS", "APARSONS", "MJONES", "FLETCHER", "STARR"],
        "labels": {"LPARSONS": "Lucy Parsons", "APARSONS": "Albert Parsons", "MJONES": "Mother Jones",
                   "FLETCHER": "Ben Fletcher", "STARR": "Vicky Starr"}},
    "debs_endorsement__dc": {"seats": 3, "alternates": 1,
        "candidates": ["RUSTIN", "DUBOIS", "MURRAY", "ROBESON", "KELLER", "THOMAS"],
        "labels": {"RUSTIN": "Bayard Rustin", "DUBOIS": "W. E. B. Du Bois", "MURRAY": "Pauli Murray",
                   "ROBESON": "Paul Robeson", "KELLER": "Helen Keller", "THOMAS": "Norman Thomas"}},
    "debs_endorsement__la": {"seats": 3, "alternates": 1,
        "candidates": ["SINCLAIR", "HEALEY", "PESOTTA", "MORENO", "MCWILLIAMS", "BRIDGES"],
        "labels": {"SINCLAIR": "Upton Sinclair", "HEALEY": "Dorothy Healey", "PESOTTA": "Rose Pesotta",
                   "MORENO": "Luisa Moreno", "MCWILLIAMS": "Carey McWilliams", "BRIDGES": "Harry Bridges"}},
    "debs_endorsement__atlarge": {"seats": 2, "alternates": 1,
        "candidates": ["OHARE", "BERGER", "BLOOR", "HARRISON", "LITTLE"],
        "labels": {"OHARE": "Kate Richards O'Hare", "BERGER": "Victor Berger", "BLOOR": "Ella Reeve Bloor",
                   "HARRISON": "Hubert Harrison", "LITTLE": "Frank Little"}},
}


def refresh_registry_from_firestore(project):
    """Overlay the contest registries from config__polls so BLT titles,
    slates, and seat counts always match what the vote service served."""
    from google.cloud import firestore
    db = firestore.Client(project=project)
    for doc in db.collection("config__polls").stream():
        d = doc.to_dict() or {}
        pid, name = doc.id, d.get("name", doc.id)
        if d.get("q6"):
            Q6_TITLES[pid] = f"{name} - {d['q6']}"
        if d.get("q8"):
            Q8_TITLES[pid] = f"{name} - {d['q8']}"
        q7 = d.get("q7") or {}
        cands = [(c["id"], c["name"]) if isinstance(c, dict) else (c[0], c[1])
                 for c in (q7.get("candidates") or [])]
        if cands:
            Q7_CONTESTS[pid] = {
                "seats": int(q7.get("seats", 1)),
                "alternates": int(q7.get("alternates", 0)),
                "candidates": [cid for cid, _ in cands],
                "labels": dict(cands),
            }


def q8_contest(poll_id):
    return {"candidates": ["YES", "NO"], "labels": {"YES": "Yes", "NO": "No"}, "seats": 1,
            "title": "DSA Chapter Member Ballot - Q8 (local issue) - " + Q8_TITLES.get(poll_id, poll_id)}


def q6_contest(poll_id):
    return {"candidates": ["YES", "NO"], "labels": {"YES": "Yes", "NO": "No"}, "seats": 1,
            "title": "DSA Chapter Member Ballot - Q6 (chapter) - " + Q6_TITLES.get(poll_id, poll_id)}


def q7_contests(poll_id):
    """TWO-COUNT METHOD for alternates:
      count 1 (delegates): Scottish STV for `seats` — its winners ARE the delegates.
      count 2 (alternates recount): same ballots, `seats + alternates` seats —
        anyone elected here who is not already a delegate becomes an alternate,
        in order of election (first N by order of election if more than N).
    There is no meaningful '31st place' in a stopped count; the recount is the
    principled way to extend the result, and both counts are reproducible from
    the published BLT."""
    c = Q7_CONTESTS[poll_id]
    total = c["seats"] + c["alternates"]
    base = {"candidates": c["candidates"], "labels": c["labels"]}
    return (
        dict(base, seats=c["seats"],
             title=f"DSA Chapter Member Ballot - Q7 - Convention DELEGATES ({poll_id}, "
                   f"{c['seats']} seats, Scottish STV)"),
        dict(base, seats=total,
             title=f"DSA Chapter Member Ballot - Q7 - ALTERNATES RECOUNT ({poll_id}, "
                   f"{total} seats; alternates = winners here not among the delegates)"),
    )


def write_yesno(all_answers, key, contest, out_path):
    idx = {c: i + 1 for i, c in enumerate(contest["candidates"])}
    counts = {c: 0 for c in contest["candidates"]}
    abstained = unknown = 0
    lines = []
    for a in all_answers:
        ch = str(a.get(key, "")).upper()
        if ch == "ABSTAIN":
            abstained += 1
        elif ch in idx:
            counts[ch] += 1
            lines.append(f"1 {idx[ch]} 0")
        else:
            unknown += 1
    _write(out_path, contest, lines)
    total = sum(counts.values())
    print(f"wrote {out_path}: {total} contest ballots, {abstained} abstentions excluded")
    for c in contest["candidates"]:
        print(f"    {contest['labels'][c]}: {counts[c]}")


def load_answers_from_csv(path):
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("voided", "").lower() in ("true", "1"):
                continue  # voided ballots are in the chain but never in tallies
            yield json.loads(row["answers"])


def load_answers_from_firestore(project, poll_id):
    from google.cloud import firestore
    db = firestore.Client(project=project)
    for doc in db.collection(f"{poll_id}__ballots").stream():
        d = doc.to_dict()
        if d.get("voided"):
            continue
        yield d["answers"]


def load_delegate_rankings_from_firestore(project, poll_id):
    """Delegate rankings live in the SEPARATE secret-ballot collection
    ({poll}__delegate_ballots) — no identity fields, admin-only access."""
    from google.cloud import firestore
    db = firestore.Client(project=project)
    for doc in db.collection(f"{poll_id}__delegate_ballots").stream():
        d = doc.to_dict()
        if d.get("voided"):
            continue
        yield {"q7": d.get("q7") or []}


def load_delegate_rankings_from_csv(path):
    """CSV export of the delegate-ballots collection (columns include 'q7' JSON)."""
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("voided", "").lower() in ("true", "1"):
                continue
            yield {"q7": json.loads(row["q7"])}


def write_q1(all_answers, out_path):
    idx = {c: i + 1 for i, c in enumerate(Q1["candidates"])}
    counts = {c: 0 for c in Q1["candidates"]}
    abstained = unknown = 0
    lines = []
    for a in all_answers:
        ch = str(a.get("q1", "")).upper()
        if ch == "ABSTAIN":
            abstained += 1
        elif ch in idx:
            counts[ch] += 1
            lines.append(f"1 {idx[ch]} 0")
        else:
            unknown += 1
    _write(out_path, Q1, lines)
    total = sum(counts.values())
    print(f"wrote {out_path}: {total} contest ballots, {abstained} abstentions excluded"
          + (f", {unknown} unknown skipped" if unknown else ""))
    for c in Q1["candidates"]:
        pct = counts[c] / total * 100 if total else 0
        print(f"    {Q1['labels'][c]}: {counts[c]} ({pct:.2f}%)")
    if total:
        verdict = ("PASSES" if counts["YES"] > counts["NO"]
                   else "TIES" if counts["YES"] == counts["NO"] else "FAILS")
        print(f"    result (abstentions excluded): {verdict}")


def write_ranked(all_answers, key, contest, out_path):
    idx = {c: i + 1 for i, c in enumerate(contest["candidates"])}
    first_pref = {c: 0 for c in contest["candidates"]}
    empty = unknown = 0
    lines = []
    for a in all_answers:
        ranking = [str(x).upper() for x in (a.get(key) or [])]
        if not ranking or ranking == ["ABSTAIN"]:
            empty += 1
            lines.append("1 0")  # empty ballot: turnout yes, exhausts immediately
            continue
        if not set(ranking) <= set(idx):
            unknown += 1
            continue
        first_pref[ranking[0]] += 1
        lines.append("1 " + " ".join(str(idx[c]) for c in ranking) + " 0")
    _write(out_path, contest, lines)
    print(f"wrote {out_path}: {len(lines)} ballots ({empty} abstain/empty)"
          + (f", {unknown} unknown skipped" if unknown else ""))
    for c in contest["candidates"]:
        print(f"    1st prefs - {contest['labels'][c]}: {first_pref[c]}")


def _write(out_path, contest, lines):
    with open(out_path, "w") as f:
        f.write(f"{len(contest['candidates'])} {contest['seats']}\n")
        for ln in lines:
            f.write(ln + "\n")
        f.write("0\n")
        for c in contest["candidates"]:
            f.write(f'"{contest["labels"][c]}"\n')
        f.write(f'"{contest["title"]}"\n')


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-csv")
    src.add_argument("--from-firestore", action="store_true")
    ap.add_argument("--delegates-csv", help="delegate-ballots CSV (q7 column) when using --from-csv")
    ap.add_argument("--project", default="dsa-org-tools")
    ap.add_argument("--poll-id", default="debs_endorsement__atlarge")
    ap.add_argument("--out-prefix", default="ballot")
    args = ap.parse_args()

    if args.from_csv:
        all_answers = list(load_answers_from_csv(args.from_csv))
        if args.delegates_csv:
            delegate_rankings = list(load_delegate_rankings_from_csv(args.delegates_csv))
        else:  # legacy single-file exports that still carry q7 inline
            delegate_rankings = [a for a in all_answers if a.get("q7")]
    else:
        try:
            refresh_registry_from_firestore(args.project)
        except Exception as e:
            print(f"note: using built-in contest registry (config__polls unavailable: {e})")
        all_answers = list(load_answers_from_firestore(args.project, args.poll_id))
        delegate_rankings = list(load_delegate_rankings_from_firestore(args.project, args.poll_id))

    write_q1(all_answers, f"{args.out_prefix}_q1.blt")
    write_ranked(all_answers, "q2", Q2, f"{args.out_prefix}_q2.blt")
    write_ranked(all_answers, "q3", Q3, f"{args.out_prefix}_q3.blt")
    write_yesno(all_answers, "q6", q6_contest(args.poll_id), f"{args.out_prefix}_q6.blt")
    write_yesno(all_answers, "q8", q8_contest(args.poll_id), f"{args.out_prefix}_q8.blt")
    dele, alt = q7_contests(args.poll_id)
    write_ranked(delegate_rankings, "q7", dele, f"{args.out_prefix}_q7_delegates.blt")
    write_ranked(delegate_rankings, "q7", alt, f"{args.out_prefix}_q7_alternates_recount.blt")


if __name__ == "__main__":
    main()
