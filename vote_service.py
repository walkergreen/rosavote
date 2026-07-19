"""
Multi-chapter referendum vote service.

One universal ballot (shared question + choices), many chapter poll instances.
Each chapter has its own poll_id, its own open/close window, its own code
universe, and its own codes/ballots collections. Routing is by /p/<poll_id>/...

Routes:
  GET  /p/<poll_id>/            branded ballot page for that chapter
  GET  /p/<poll_id>/v/<code>    same page, code embedded (one-tap link)
  GET  /p/<poll_id>/voted       has this code voted? (pre-ballot check)
  POST /p/<poll_id>/vote        cast a code-authenticated ballot
  POST /p/<poll_id>/provisional cast a sealed provisional ballot (no code)
  GET  /healthz

Shared ballot definition lives in ONE place (BALLOT), so every chapter votes
on identical wording. Per-chapter config (name, window) lives in CHAPTERS.
"""

import calendar
import hashlib
import json
import os
import random
import re
import secrets
import time
from datetime import date, timedelta

from flask import Flask, request, jsonify, Response
from google.cloud import firestore
from google.api_core import exceptions as gcloud_exc

app = Flask(__name__)


class _LazyDB:
    """Defers firestore.Client() to first use so the module imports (and
    /healthz serves) without credentials — tools that only need the CHAPTERS
    seed, and local shells without ADC, can import vote_service freely."""
    _client = None

    def __getattr__(self, name):
        if _LazyDB._client is None:
            _LazyDB._client = firestore.Client()
        return getattr(_LazyDB._client, name)


db = _LazyDB()

# ---- shared ballot definition (identical across all chapters) -------------
# Five questions. Ranked questions (q2, q3) are Scottish STV; an ["ABSTAIN"]
# ranking abstains on that question. Pledges are optional multi-select.
# The free-text comment is NOT part of the ballot record — it is stored in a
# separate, unlinked collection (admin-only, never published), so the
# publishable ballot file can't leak identifying prose.
BALLOT = {
    "q1": {"YES", "NO", "ABSTAIN"},
    "q2": {"IE", "COORD", "NOEND", "NOTA"},
    "q3": {"DEBS", "WILSON", "ROOSEVELT", "TAFT", "NOTA2"},
    "pledges": {"DONATE", "VOLUNTEER", "CANVASS", "PHONEBANK", "TEXTBANK", "HOST"},
    "text_max": 1000,
}


def validate_answers(data, cfg) -> tuple[dict, str]:
    """Validate the submitted answers. Returns (answers, comment_text).
    Raises ValueError on anything malformed."""
    if not isinstance(data, dict):
        raise ValueError("answers")
    q1 = str(data.get("q1", "")).upper()
    if q1 not in BALLOT["q1"]:
        raise ValueError("q1")

    def ranked(key):
        v = data.get(key)
        if not isinstance(v, list) or not v:
            raise ValueError(key)
        v = [str(x).upper() for x in v]
        if v == ["ABSTAIN"]:
            return v
        if len(set(v)) != len(v) or not set(v) <= BALLOT[key]:
            raise ValueError(key)
        return v

    q2, q3 = ranked("q2"), ranked("q3")
    p = data.get("pledges") or []
    if not isinstance(p, list):
        raise ValueError("pledges")
    p = [str(x).upper() for x in p]
    if p != ["ABSTAIN"] and (len(set(p)) != len(p) or not set(p) <= BALLOT["pledges"]):
        raise ValueError("pledges")
    q6 = str(data.get("q6", "")).upper()
    if q6 not in BALLOT["q1"]:          # same YES/NO/ABSTAIN set
        raise ValueError("q6")
    q7 = data.get("q7")
    q7_ids = {cid for cid, _ in cfg["q7"]["candidates"]}
    if not isinstance(q7, list) or not q7:
        raise ValueError("q7")
    q7 = [str(x).upper() for x in q7]
    if q7 != ["ABSTAIN"] and (len(set(q7)) != len(q7) or not set(q7) <= q7_ids):
        raise ValueError("q7")
    q8 = str(data.get("q8", "")).upper()
    if q8 not in BALLOT["q1"]:
        raise ValueError("q8")
    text = str(data.get("text", "") or "")[: BALLOT["text_max"]]
    return {"q1": q1, "q2": q2, "q3": q3, "pledges": sorted(p), "q6": q6, "q7": q7, "q8": q8}, text


def canon_answers(a: dict) -> str:
    """Deterministic serialization — MUST match tools/verify.py + build_chain.py."""
    return json.dumps(a, separators=(",", ":"), sort_keys=True)

# ---- chapter registry (FALLBACK SEED) -------------------------------------
# Served only until config__polls is seeded in Firestore (tools/seed_config.py);
# after that, load_polls() below is the source of truth. poll_id -> config.
# opens_at / closes_at are unix seconds; None means "no window enforced".
CHAPTERS = {
    # Per chapter: display name, window, a REPEATABLE test code (never recorded),
    # the chapter-unique question ("q6"), the local-issue question ("q8"), and
    # the convention-delegate contest ("q7", shown LAST on the ballot).
    # Delegate math: 2025 apportionment (1:60) — NYC 151 / LA 64 / DC 46 /
    # Chicago 38 / At-Large+OCs 90. Prototype elects a scaled-down test slate of
    # historical figures.
    "debs_endorsement__nyc": {
        "name": "New York City", "opens_at": None, "closes_at": None,
        "test_code": "TEST-NYC-2026-DEMO",
        "q6": "Shall NYC-DSA charter a fourth branch in the Bronx?",
        "q8": "Shall NYC-DSA campaign to block the proposed Brooklyn waterfront data center?",
        "q7": {"seats": 4, "alternates": 1, "real_delegates": 151, "real_alternates": 15, "candidates": [
            ("LONDON", "Meyer London"), ("FLYNN", "Elizabeth Gurley Flynn"),
            ("GOLDMAN", "Emma Goldman"), ("JONES", "Claudia Jones"),
            ("DELEON", "Daniel De Leon"), ("SCHNEIDERMAN", "Rose Schneiderman"),
            ("RANDOLPH", "A. Philip Randolph"),
        ]},
    },
    "debs_endorsement__chi": {
        "name": "Chicago", "opens_at": None, "closes_at": None,
        "test_code": "TEST-CHI-2026-DEMO",
        "q6": "Shall Chicago DSA open a permanent chapter office in Pilsen?",
        "q8": "Shall Chicago DSA fund a Starbucks Workers United solidarity committee?",
        "q7": {"seats": 2, "alternates": 1, "real_delegates": 38, "real_alternates": 4, "candidates": [
            ("LPARSONS", "Lucy Parsons"), ("APARSONS", "Albert Parsons"),
            ("MJONES", "Mother Jones"), ("FLETCHER", "Ben Fletcher"),
            ("STARR", "Vicky Starr"),
        ]},
    },
    "debs_endorsement__dc": {
        "name": "Metro DC", "opens_at": None, "closes_at": None,
        "test_code": "TEST-DC-2026-DEMO0",
        "q6": "Shall Metro DC DSA fund childcare at all general meetings?",
        "q8": "Shall Metro DC DSA campaign to make Metrobus fare-free?",
        "q7": {"seats": 3, "alternates": 1, "real_delegates": 46, "real_alternates": 5, "candidates": [
            ("RUSTIN", "Bayard Rustin"), ("DUBOIS", "W. E. B. Du Bois"),
            ("MURRAY", "Pauli Murray"), ("ROBESON", "Paul Robeson"),
            ("KELLER", "Helen Keller"), ("THOMAS", "Norman Thomas"),
        ]},
    },
    "debs_endorsement__la": {
        "name": "Los Angeles", "opens_at": None, "closes_at": None,
        "test_code": "TEST-LA-2026-DEMO0",
        "q6": "Shall DSA-LA launch a chapter tenant-organizing school?",
        "q8": "Shall DSA-LA campaign for a citywide rent freeze?",
        "q7": {"seats": 3, "alternates": 1, "real_delegates": 64, "real_alternates": 6, "candidates": [
            ("SINCLAIR", "Upton Sinclair"), ("HEALEY", "Dorothy Healey"),
            ("PESOTTA", "Rose Pesotta"), ("MORENO", "Luisa Moreno"),
            ("MCWILLIAMS", "Carey McWilliams"), ("BRIDGES", "Harry Bridges"),
        ]},
    },
    "debs_endorsement__atlarge": {
        "name": "At-Large", "opens_at": None, "closes_at": None,
        "test_code": "TEST-ATLARGE-2026-DEMO",
        "q6": "Shall at-large members charter a national At-Large Organizing Committee?",
        "q8": "Shall DSA launch a national campaign against data-center-driven utility rate hikes?",
        "q7": {"seats": 2, "alternates": 1, "real_delegates": 90, "real_alternates": 9, "candidates": [
            ("OHARE", "Kate Richards O'Hare"), ("BERGER", "Victor Berger"),
            ("BLOOR", "Ella Reeve Bloor"), ("HARRISON", "Hubert Harrison"),
            ("LITTLE", "Frank Little"),
        ]},
    },
}

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
TEMPLATE = open(os.path.join(os.path.dirname(__file__), "ballot_template.html")).read()


def code_hash(code_plaintext: str) -> str:
    return hashlib.sha256(code_plaintext.encode()).hexdigest()


def make_record_hash(receipt: str, answers_canon: str, nonce: str) -> str:
    return hashlib.sha256(f"{receipt}|{answers_canon}|{nonce}".encode()).hexdigest()


# ---- poll config: Firestore-backed, CHAPTERS is the fallback seed ----------
# config__polls holds one doc per poll_id. Because Firestore forbids nested
# arrays, q7.candidates is stored as [{"id": ..., "name": ...}]; the loader
# normalizes back to the (id, name) tuples the rest of the code expects.
# tools/seed_config.py pushes the CHAPTERS seed into the collection.
CONFIG_COLL = "config__polls"
CFG_TTL_SECONDS = 60.0
_cfg_cache = {"at": 0.0, "polls": None}


def _normalize_cfg(d: dict) -> dict:
    cfg = dict(d)
    cfg.setdefault("opens_at", None)
    cfg.setdefault("closes_at", None)
    q7 = dict(cfg.get("q7") or {})
    q7["candidates"] = [
        (c["id"], c["name"]) if isinstance(c, dict) else (c[0], c[1])
        for c in (q7.get("candidates") or [])
    ]
    q7.setdefault("alternates", 0)
    q7.setdefault("real_delegates", 0)
    q7.setdefault("real_alternates", 0)
    cfg["q7"] = q7
    return cfg


def cfg_to_doc(cfg: dict) -> dict:
    """Inverse of _normalize_cfg: make a config Firestore-storable."""
    doc = dict(cfg)
    q7 = dict(doc.get("q7") or {})
    q7["candidates"] = [
        c if isinstance(c, dict) else {"id": c[0], "name": c[1]}
        for c in (q7.get("candidates") or [])
    ]
    doc["q7"] = q7
    return doc


def load_polls(force: bool = False) -> dict:
    """All active poll configs, from Firestore when seeded, else the CHAPTERS
    seed. Cached per instance for CFG_TTL_SECONDS; admin edits force-reload
    their own instance and other instances converge within the TTL."""
    now = time.time()
    if not force and _cfg_cache["polls"] is not None and now - _cfg_cache["at"] < CFG_TTL_SECONDS:
        return _cfg_cache["polls"]
    try:
        polls = {}
        for snap in db.collection(CONFIG_COLL).stream():
            d = snap.to_dict() or {}
            if d.get("archived"):
                continue
            polls[snap.id] = _normalize_cfg(d)
        if not polls:
            polls = CHAPTERS
    except Exception:
        polls = CHAPTERS  # Firestore unreachable/unseeded — serve the built-in seed
    _cfg_cache.update(at=now, polls=polls)
    return polls


def chapter_or_none(poll_id: str):
    return load_polls().get(poll_id)


def window_state(cfg):
    now = time.time()
    if cfg.get("opens_at") and now < cfg["opens_at"]:
        return "not_open"
    if cfg.get("closes_at") and now > cfg["closes_at"]:
        return "closed"
    return "open"


def _q7_option_html(cid: str, label: str) -> str:
    return (
        '<button type="button" class="vk-opt" data-choice="' + cid + '" style="font-size:1.35rem;">'
        '<span class="vk-rankn" aria-hidden="true"></span>'
        '<span>' + label + '</span></button>'
    )


def render_ballot(poll_id: str, cfg: dict, code: str = "") -> str:
    q7 = cfg["q7"]
    shuffled = list(q7["candidates"])
    random.shuffle(shuffled)          # random order per page load — no alphabet advantage
    opts = "".join(_q7_option_html(cid, name) for cid, name in shuffled)
    opts += _q7_option_html("ABSTAIN", "Abstain<small>Skip this question</small>")
    real = (f"At the national convention {cfg['name']} elects {q7['real_delegates']} "
            f"delegates and {q7['real_alternates']} alternates (2025 apportionment, "
            f"1:60 and 1:10). ") if q7["real_delegates"] else ""
    total = q7["seats"] + q7["alternates"]
    q7_note = (real + f"<b>How this is counted (two-count method):</b> delegates are "
               f"decided by a Scottish STV count for {q7['seats']} seats. The same "
               f"ballots are then recounted for {total} seats \u2014 anyone elected in "
               "the recount who is not already a delegate becomes an alternate, in "
               "order of election. Every preference you rank can matter in both "
               "counts, so rank as many candidates as you like (a first choice is "
               "required) \u2014 or tap Abstain to skip. Candidates appear in random "
               "order, freshly shuffled for each voter. Ranks past the first "
               f"{q7['seats']} turn black \u2014 those preferences still count and "
               "help decide the alternates.")
    names = {cid: name for cid, name in q7["candidates"]}
    names["ABSTAIN"] = "Abstain"
    html = TEMPLATE.replace("__POLL_ID__", poll_id)
    html = html.replace("__CHAPTER_NAME__", cfg["name"])
    from urllib.parse import quote
    html = html.replace("__HELP_SUBJECT__", quote(f"[BALLOT26] {cfg['name']} — Can't find my code"))
    html = html.replace("__Q6_QUESTION__", cfg["q6"])
    html = html.replace("__Q8_QUESTION__", cfg["q8"])
    html = html.replace("__Q7_OPTIONS__", opts)
    html = html.replace("__Q7_NOTE__", q7_note)
    html = html.replace("__Q7_SEATS__", str(q7["seats"]))
    alts = q7["alternates"]
    html = html.replace("__Q7_SEATS_N__", str(q7["seats"]))
    html = html.replace("__Q7_ALTS__", f"{alts} alternate" + ("" if alts == 1 else "s"))
    html = html.replace("__Q7_NAMES__", json.dumps(names))
    html = html.replace("__CODE__", code if CODE_RE.match(code or "") else "")
    return html


# ---- vote transaction (single-doc: no hot document) -----------------------
@firestore.transactional
def _claim_code(txn, codes_coll, ch: str):
    """Claims the code atomically. Returns the code doc's voter fields
    (member_id, chapter) on success, None if already used."""
    ref = codes_coll.document(ch)
    snap = ref.get(transaction=txn)
    if not snap.exists:
        raise LookupError("unknown code")
    d = snap.to_dict() or {}
    if d.get("used"):
        return None
    txn.update(ref, {"used": True})
    return {"member_id": d.get("member_id"), "chapter": d.get("chapter")}


def cast_vote(poll_id: str, code_plaintext: str, answers: dict, comment: str) -> dict:
    """HYBRID VISIBILITY MODEL:
    * Poll questions + local issues ({poll}__ballots): identity-linked
      (member_id, chapter, code_hash) — visible to election administrators and
      the voter's chapter. Results not published publicly.
    * DELEGATE ranking ({poll}__delegate_ballots): SECRET BALLOT per Const.
      Art. V §5 — stored separately with NO member_id and NO chapter identity.
      The collection is ADMIN-ONLY; it retains the code_hash solely so an
      election administrator can trace a specific ballot for troubleshooting
      (via the codes collection). Chapters never get access to this collection
      or the code mapping. Admin access must be IAM-restricted + audit-logged.
    Voters are told all of this on the ballot before voting."""
    codes = db.collection(f"{poll_id}__codes")
    ballots = db.collection(f"{poll_id}__ballots")
    ch = code_hash(code_plaintext)
    txn = db.transaction()
    claim = _claim_code(txn, codes, ch)
    if claim is None:
        return {"status": "already_voted"}
    receipt = secrets.token_hex(4).upper()

    # identity-linked record: everything EXCEPT the delegate ranking
    main_answers = {k: v for k, v in answers.items() if k != "q7"}
    nonce = secrets.token_hex(8)
    ac = canon_answers(main_answers)
    rh = make_record_hash(receipt, ac, nonce)
    ballots.document(rh).set({
        "receipt": receipt, "answers": main_answers, "answers_canon": ac,
        "nonce": nonce, "record_hash": rh,
        "member_id": claim.get("member_id"),
        "chapter": claim.get("chapter"),
        "code_hash": ch,
        "comment": comment,
        "day_bucket": firestore.SERVER_TIMESTAMP,
    })

    # secret delegate ballot: same receipt (voter verification), no identity
    dq = {"q7": answers.get("q7") or []}
    dnonce = secrets.token_hex(8)
    dcanon = canon_answers(dq)
    drh = make_record_hash(receipt, dcanon, dnonce)
    db.collection(f"{poll_id}__delegate_ballots").document(drh).set({
        "receipt": receipt, "q7": dq["q7"], "answers_canon": dcanon,
        "nonce": dnonce, "record_hash": drh,
        "code_hash": ch,   # ADMIN-ONLY troubleshooting trace — never chapter-visible
        "day_bucket": firestore.SERVER_TIMESTAMP,
    })
    return {"status": "recorded", "receipt": receipt}


def cast_provisional(poll_id: str, answers: dict, comment: str, info: dict) -> dict:
    """Sealed provisional ballot: stored separately, NOT counted, no code used.
    Adjudicated by staff against membership after the fact. (Identity-linked
    by design until adjudication, so the comment lives with it.)"""
    prov = db.collection(f"{poll_id}__provisional")
    receipt = "P" + secrets.token_hex(4).upper()[:7]
    prov.document(receipt).set({
        "receipt": receipt,
        "answers": answers, "comment": comment,   # sealed; not in tally until verified
        "first": info.get("first", ""), "last": info.get("last", ""),
        "emails": (info.get("emails", "") or "").lower(),   # all emails, free text
        "phones": info.get("phones", ""),                    # all phones, free text
        "chapter": info.get("chapter", ""),
        "join_date": info.get("joined", ""),                 # optional
        "alt_names": info.get("alt_names", ""),              # optional — maiden/previous names

        "status": "pending",            # pending -> verified/rejected by staff
        "submitted": firestore.SERVER_TIMESTAMP,
    })
    return {"status": "provisional_recorded", "receipt": receipt}


# ---- routes ---------------------------------------------------------------
@app.get("/p/<poll_id>/")
def page(poll_id):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return "Unknown poll.", 404
    return Response(render_ballot(poll_id, cfg), mimetype="text/html")


@app.get("/p/<poll_id>/v/<code>")
def page_with_code(poll_id, code):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return "Unknown poll.", 404
    safe = code if CODE_RE.match(code) else ""
    return Response(render_ballot(poll_id, cfg, safe), mimetype="text/html")


@app.get("/p/<poll_id>/voted")
def voted(poll_id):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    code = request.args.get("code", "")
    if not CODE_RE.match(code):
        return jsonify({"error": "invalid_code_format"}), 400
    if code == cfg.get("test_code"):
        return jsonify({"voted": False}), 200   # repeatable test code: never "used"
    snap = db.collection(f"{poll_id}__codes").document(code_hash(code)).get()
    if not snap.exists:
        return jsonify({"error": "invalid_code"}), 404
    return jsonify({"voted": bool(snap.get("used"))}), 200


@app.post("/p/<poll_id>/vote")
def vote(poll_id):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    st = window_state(cfg)
    if st == "not_open":
        return jsonify({"error": "not_open", "message": f"Voting has not opened for {cfg['name']}."}), 403
    if st == "closed":
        return jsonify({"error": "closed", "message": f"Voting has closed for {cfg['name']}."}), 403

    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not CODE_RE.match(code):
        return jsonify({"error": "invalid_code_format"}), 400
    try:
        answers, comment = validate_answers(data.get("answers"), cfg)
    except ValueError as bad:
        return jsonify({"error": "invalid_answers", "field": str(bad)}), 400
    if code == cfg.get("test_code"):
        # repeatable test vote: full UX (receipt + confirmation), nothing stored
        return jsonify({"status": "recorded", "receipt": "TEST-" + secrets.token_hex(3).upper()}), 200
    try:
        result = cast_vote(poll_id, code, answers, comment)
    except LookupError:
        return jsonify({"error": "invalid_code"}), 404
    except gcloud_exc.Aborted:
        return jsonify({"error": "try_again"}), 503
    return jsonify(result), 200


@app.post("/p/<poll_id>/provisional")
def provisional(poll_id):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    st = window_state(cfg)
    if st != "open":
        return jsonify({"error": st, "message": f"Voting is not open for {cfg['name']}."}), 403

    data = request.get_json(silent=True) or {}
    info = data.get("info", {}) or {}
    try:
        answers, comment = validate_answers(data.get("answers"), cfg)
    except ValueError as bad:
        return jsonify({"error": "invalid_answers", "field": str(bad)}), 400
    if not info.get("first") or not info.get("last") or not info.get("chapter"):
        return jsonify({"error": "missing_info"}), 400
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", info.get("emails", "")):
        return jsonify({"error": "missing_info", "field": "emails"}), 400
    return jsonify(cast_provisional(poll_id, answers, comment, info)), 200


SPLASH = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>DSA Chapter Member Ballot</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Alegreya:ital,wght@0,400;0,700;1,400&family=Barlow+Condensed:wght@700;800;900&family=Barlow:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
  :root{--dsa:#dd1111;--cream:#fff5e5;--tan:#ffe1b2;--ink:#000;
    --disp:"Barlow Condensed","Arial Narrow",sans-serif;--body:"Alegreya",Georgia,serif;--ui:"Barlow",system-ui,sans-serif;}
  *{box-sizing:border-box}html,body{margin:0;padding:0}
  body{font-family:var(--body);background:var(--cream);color:var(--ink);-webkit-font-smoothing:antialiased;min-height:100dvh;display:flex;flex-direction:column}
  .band{background:var(--dsa);color:var(--cream);padding:14px 16px 12px}
  .band-in{max-width:430px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px}
  .wm{font-family:var(--disp);font-weight:900;text-transform:uppercase;font-size:1.9rem;line-height:.88}
  .wm small{display:block;font-size:.92rem;font-weight:800;opacity:.88;letter-spacing:.02em;margin-top:2px}
  .rose{font-family:var(--disp);font-weight:900;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;border:2px solid var(--cream);padding:5px 8px 4px;text-align:center;line-height:1.05;flex:none}
  main{flex:1;width:100%;max-width:430px;margin:0 auto;padding:18px 14px 40px}
  .marks{height:7px;background-image:repeating-linear-gradient(to right,#dd1111 0 7px,transparent 7px 18px);background-size:100% 7px;background-repeat:repeat-x}
  .card{background:#fff;border:1px solid var(--ink);box-shadow:6px 6px 0 0 var(--ink);margin-top:14px}
  .card-band{background:var(--dsa);color:#fff;padding:10px 14px;font-family:var(--disp);font-weight:800;text-transform:uppercase;font-size:1.45rem}
  .card-body{padding:16px 14px 18px}
  .kicker{font-family:var(--ui);font-weight:700;font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;color:var(--dsa);margin:0 0 6px}
  p{font-size:1rem;line-height:1.45;margin:0 0 12px}
  code.tc{font-family:ui-monospace,Menlo,monospace;font-weight:700;font-size:1.05rem;letter-spacing:.08em;background:var(--cream);border:1px dashed var(--ink);padding:4px 10px;display:inline-block}
  .btn{display:flex;align-items:center;justify-content:center;min-height:48px;margin-top:12px;padding:10px 16px;font-family:var(--disp);font-weight:800;font-size:1.2rem;letter-spacing:.04em;text-transform:uppercase;color:var(--cream);background:var(--dsa);border:1px solid var(--ink);box-shadow:4px 4px 0 0 var(--ink);text-decoration:none}
  .btn:hover{background:#ef3232}
  ul{list-style:none;padding:0;margin:0}
  li a{display:block;padding:12px 13px;margin:0 0 10px;background:#fff;border:1px solid var(--ink);font-family:var(--disp);font-weight:800;font-size:1.4rem;text-transform:uppercase;color:var(--ink);text-decoration:none}
  li a:hover{background:var(--tan);box-shadow:4px 4px 0 0 var(--dsa);border-color:var(--dsa)}
  .fine{font-family:var(--ui);font-size:.78rem;color:rgba(0,0,0,.62);line-height:1.45;margin:10px 0 0}
  details{border:1px solid rgba(0,0,0,.25);background:var(--cream);margin:0 0 10px}
  details summary{cursor:pointer;list-style:none;padding:10px 12px;font-family:var(--ui);font-weight:700;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dsa)}
  details summary::-webkit-details-marker{display:none}
  details summary::after{content:" ▾"}
  details[open] summary::after{content:" ▴"}
  details .dbody{padding:0 12px 12px;font-family:var(--ui);font-size:.82rem;line-height:1.5}
  details .dbody p{font-size:.82rem;margin:0 0 8px}
  details .dbody b{color:var(--dsa)}
  footer{font-family:var(--ui);font-size:.68rem;color:rgba(0,0,0,.55);text-align:center;padding:14px 16px 22px;line-height:1.5}
  footer b{color:var(--dsa)}
</style></head><body>
<header class="band"><div class="band-in">
  <div class="wm">Chapter Member Ballot<small>Democratic Socialists of America</small></div>
  <a class="rose" href="/" style="color:inherit;text-decoration:none;display:block;">DSA<br/>Vote</a>
</div></header>
<main>
  <div class="marks"></div>
  <div class="card">
    <div class="card-band">Try It &mdash; Test Codes</div>
    <div class="card-body">
      <p class="kicker">Testing only &middot; not a live election</p>
      <p>Each chapter has a <b>repeatable</b> test code &mdash; vote as many times as
      you like; test votes are never recorded or counted.</p>
      __TEST_ROWS__
    </div>
  </div>
  <div class="card">
    <div class="card-band">Chapter Ballots</div>
    <div class="card-body">
      <p>Voting members: use the link and code you were sent. Chapter ballots on this service:</p>
      <ul>__CHAPTER_LINKS__</ul>
    </div>
  </div>
  <div class="card">
    <div class="card-band" style="background:#000">For Administrators &mdash; How This Works</div>
    <div class="card-body">

      <details>
        <summary>What's on each ballot</summary>
        <div class="dbody">
        <p>Every chapter ballot has <b>three sections</b> on one page:
        <b>Section 1 &middot; Chapter Poll</b> &mdash; five shared questions polled identically in
        every chapter (endorsement, campaign structure, a ranked test field, pledges, open
        comment), reported per chapter and aggregated;
        <b>Section 2 &middot; Convention Delegates</b> &mdash; the chapter's delegate election by
        secret ballot, ranked choice; and
        <b>Section 3 &middot; Local Issues</b> &mdash; two ballot issues unique to that chapter.</p>
        </div>
      </details>

      <details>
        <summary>Delegate election timing (Const. Art. V &sect;5)</summary>
        <div class="dbody">
        <p>Unlike chapter polls and local issues &mdash; which chapters may schedule
        freely &mdash; the <b>delegate election is constitutionally time-boxed</b>: it may
        be held <b>no more than four months and no less than forty-five days</b> before
        the National Convention opens, and <b>only after delegates have been
        apportioned</b> (apportionment uses membership as of four months prior). Art. V
        &sect;5 also requires the delegate election be by <b>secret ballot</b> (enforced
        here by construction) and permits proportional representation within the Bylaws
        &mdash; cite Scottish STV and the two-count alternates rule in the chapter's
        adopted election rules. <b>Practical implication:</b> because this ballot combines
        all three sections on one page, the whole ballot inherits the delegate window
        &mdash; set each chapter's <code>opens_at</code>/<code>closes_at</code> inside it,
        or run the poll/local sections as a separate ballot if a chapter wants them
        outside that window.</p>
        </div>
      </details>

      <details>
        <summary>Technical setup</summary>
        <div class="dbody">
        <p>A small Flask service on <b>Cloud Run</b> (auto-scaling, scales to zero) with
        <b>Firestore</b> for codes and ballots and <b>BigQuery</b> as the membership source.
        Each chapter is its own poll (<b>/p/&lt;poll_id&gt;/</b>) with its own voting window and
        code universe, on one shared deployment. The vote transaction touches only the
        voter's own code document, so writes scale horizontally &mdash; capacity is thousands
        of votes per second; the election needs a small fraction of that.</p>
        </div>
      </details>

      <details>
        <summary>PII &amp; vote visibility (not a secret ballot)</summary>
        <div class="dbody">
        <p><b>Hybrid visibility, disclosed to voters on the ballot:</b> poll questions and
        local issues (Sections 1 &amp; 3) are recorded <b>by name</b> &mdash; election
        administrators see all ballots; each chapter sees its own members' votes.
        <b>The delegate election (Section 2) is a secret ballot</b> per Const. Art. V
        &sect;5: rankings are stored in a separate admin-only collection with no name or
        chapter attached. It retains only a code hash so an election administrator can
        trace one specific ballot when troubleshooting &mdash; that access must be
        IAM-restricted and audit-logged, and chapters never get it. <b>National does not
        publish any chapter's results.</b> Each chapter receives its own results package
        and decides for itself whether to publish them &mdash; including named votes on
        Sections 1 &amp; 3, but never delegate rankings. Voting codes are stored only as
        SHA-256 hashes.</p>
        </div>
      </details>

      <details>
        <summary>Security &amp; abuse resistance</summary>
        <div class="dbody">
        <p>The app <b>sends nothing on demand</b>, so a leaked link cannot run up messaging
        costs &mdash; assume links leak; only codes gate voting. Malformed codes are rejected
        before any database access. Code resends are <b>enumeration-safe</b>: a typed contact
        is a lookup key, never a destination, so sends only ever go to on-record contacts
        of real members, rate-limited per contact and IP. Production hardening: Cloud Armor
        rate limits, a max-instances cap (floods become slowness, not bills), and billing
        alerts. One code = one vote, enforced atomically.</p>
        </div>
      </details>

      <details>
        <summary>Vote storage, tabulation &amp; alternates</summary>
        <div class="dbody">
        <p>Each ballot records the answers, a receipt code, a nonce, and a content hash.
        After close, a batch job builds a <b>tamper-evident hash chain</b> over all ballots
        and publishes ballots.csv, used-code counts, and the chain head. Ranked questions
        are counted by <b>Scottish STV</b> (SSI 2007/42). Delegates use the <b>two-count
        method</b>: an STV count for the delegate seats decides the delegation; the same
        ballots are recounted with delegate+alternate seats, and anyone elected there who
        is not already a delegate becomes an alternate, in order of election. Candidate
        order is randomly shuffled per voter.</p>
        </div>
      </details>

      <details>
        <summary>Verification (internal &mdash; results not public)</summary>
        <div class="dbody">
        <p>After close, the export, hash chain, BLT files, and verifier run per chapter,
        and each chapter's admins receive their own results package. National does not
        publish chapter results; <b>each chapter decides whether and how to publish its
        own</b> &mdash; full results, named votes on Sections 1 &amp; 3, or nothing &mdash;
        except delegate rankings, which stay secret regardless. Voters can confirm their
        receipt with the administrator; chapters can recount their own ballots in
        independent software (OpaVote, OpenSTV, the shipped tabulator) and check ballots
        against used codes.</p>
        </div>
      </details>

      <details>
        <summary>Accessibility (WCAG 2.1 AA)</summary>
        <div class="dbody">
        <p>Automated audit (axe-core): <b>0 violations</b>. All 13 color pairs pass AA
        contrast. Full keyboard operation including arrow-key radio groups; screen readers
        announce rank positions and status changes; no zoom lock. Status: passes everything
        automation can check; a recorded human screen-reader run (VoiceOver / TalkBack /
        NVDA) is required before claiming conformance publicly. Test report ships with the
        code.</p>
        </div>
      </details>

      <details>
        <summary>How ballots reach voters &amp; reminders</summary>
        <div class="dbody">
        <p>Channel ladder per member: <b>email first</b> (near-free); <b>SMS</b> if no email
        is on file; <b>printed postcard</b> with the code and a QR one-tap link if neither
        exists (mailed first, since it's slowest). Non-voters then get <b>email and SMS
        reminders</b> before close. All sends go through the distribution platform from a
        generated manifest &mdash; this app never sends messages itself.</p>
        <p><b>Provisional ballots save support time.</b> Voters who can't find a code
        self-serve from the ballot page: they cast a sealed provisional ballot with
        everything adjudicators need to match them &mdash; all emails and phones that
        might be on their membership, chapter, join date, and alternate names. Instead of
        staff live-troubleshooting each "I never got a code" ticket during the election,
        those cases queue for asynchronous batch adjudication, and the voter has already
        voted &mdash; no follow-up round-trip to get their ballot in.</p>
        </div>
      </details>

      <details>
        <summary>Estimated all-in cost</summary>
        <div class="dbody">
        <p>Infrastructure is <b>single-digit dollars</b> (Firestore ~$1, Cloud Run a few
        dollars); email is roughly free. The real spend is SMS at <b>$0.0125 per
        segment</b>. Plan: ballots go by email first; members who haven't voted by email
        get an SMS, then one more, smaller SMS reminder to remaining non-voters. Worked
        example at 120k members: wave 1 &asymp; 72k texts (non-voters after email), wave 2
        &asymp; 45k &mdash; ~117k segments &asymp; <b>$1,460</b> if the message fits one
        segment (keep it under 160 characters: code + short link), roughly double if it
        spills to two. Postcards for the no-email/no-phone tier (~2&ndash;3k members) add
        <b>~$2&ndash;3k</b> printed and mailed. Realistic all-in: <b>~$2&ndash;5k</b>,
        driven almost entirely by SMS volume, segment count, and postcards. Staff time for
        provisional adjudication is the real hidden cost.</p>
        </div>
      </details>

      <details>
        <summary>How this compares to OpaVote</summary>
        <div class="dbody">
        <p><b>Cost.</b> OpaVote prices per voter/ballot &mdash; quoted <b>~$9,600</b> for a
        ~120k-voter election, and each chapter running on its own schedule means separate
        elections, multiplying fees. This app's infrastructure is ~$10; total spend is
        message delivery (<b>~$2&ndash;5k</b> with SMS + postcards), which OpaVote doesn't
        cover anyway &mdash; it emails ballot links only, so SMS/postcard tiers and
        reminder waves would be an extra system in either case.</p>
        <p><b>Tabulation.</b> Effectively identical rules: OpaVote's Scottish STV descends
        from OpenSTV, validated against eSTV &mdash; the same statute this app's shipped
        tabulator implements. Both export/consume BLT, so either can recount the other.
        OpaVote gives you a polished results page with per-round charts and a public
        recount link for free; here you generate results packages per chapter with the
        two-count delegate/alternates method built in. On OpaVote the alternates recount
        is a <b>separately purchased count</b> &mdash; counts are priced on ballots cast,
        so it raises cost (not necessarily double, since turnout is a fraction of the
        electorate), and it's one more paid count per chapter, per rerun.</p>
        <p><b>Security &amp; trust model.</b> OpaVote is a neutral, battle-tested third
        party &mdash; strong optics, zero code to maintain, but closed operations, emailed
        magic links as the only auth, and <b>data deleted ~12 weeks after start</b> unless
        extended. This app is code-gated (hashed codes, enumeration-safe resend, atomic
        one-code-one-vote), fully auditable source, data retained on staff&rsquo;s terms &mdash;
        but staff own the security burden: IAM, audit logs, hardening, and a staff
        administrator who is accountable. On visibility, OpaVote does offer anonymity
        settings &mdash; elections can be configured non-anonymous so the election manager
        sees how voters voted &mdash; but it's one setting for the whole election:
        per-section rules like this ballot's (named poll votes and a secret delegate
        ballot on the same page, with chapter-level access to their own members' votes)
        aren't configurable there.</p>
        <p><b>Operations &amp; troubleshooting.</b> On OpaVote, every count after close is
        a paid count, so verification passes, alternate recounts, and "let's just check
        that again" all meter &mdash; troubleshooting has a price tag per attempt. There's
        also no mid-election intervention: you cannot make manual adjustments while voting
        is open, and you cannot verify whether or how a specific member voted
        mid-election. Here, an administrator can trace a specific ballot during voting
        (via the audited code mapping) and rerun tallies unlimited times at no cost.</p>
        <p><b>Bottom line.</b> OpaVote: pay ~2&ndash;4&times; more for a trusted-brand,
        low-effort, email-only, secret-ballot-only election with expiring data and metered
        recounts. This app is ~10&times; cheaper at scale and, more importantly, is
        <b>built around our own membership</b>: codes are generated directly from the
        deduplicated membership roll in the data warehouse &mdash; no list uploads, no
        stale voter files &mdash; and the ballot itself is <b>fully customizable</b> in
        ways no hosted product matches: three styled sections on one page, chapter-unique
        questions and local issues, per-chapter delegate slates with randomized order and
        the two-count alternates rule, pledge checkboxes, a free-form comment field, and
        per-section visibility rules (named poll votes, secret delegate ballots). The
        trade is operating and defending it ourselves &mdash; and unlimited free recounts
        make that defense cheaper here than metered counts there.</p>
        </div>
      </details>

      <details>
        <summary>How this compares to OpenSlides</summary>
        <div class="dbody">
        <p><b>Different jobs.</b> OpenSlides is a live assembly system &mdash; agenda,
        motions, amendments, speakers list, projector, and floor votes for delegates who
        are present (in the hall or logged in) during a meeting. This app is an
        <b>asynchronous ballot</b>: members vote on their own phones over days or weeks.
        They're complements, not substitutes &mdash; OpenSlides runs the convention floor;
        this runs the pre-convention delegate elections and chapter polls that Art. V
        requires chapters to conduct themselves.</p>
        <p><b>Architecture &amp; scale.</b> Live assembly voting is one of the hardest
        real-time problems there is: every participant holds a persistent connection,
        and the whole room votes in the same ninety seconds on venue WiFi. This app
        sidesteps that entire class of challenge by being asynchronous and stateless:
        plain request/response, no persistent connections to manage, Cloud Run
        autoscaling, and voters spread over days instead of seconds &mdash; so 120,000
        members is a lighter load than a single packed hall. Using each tool for the
        job it's built for keeps both at their best: OpenSlides where the room is live,
        this app where the electorate is distributed.</p>
        <p><b>Voting features.</b> OpenSlides is capable here too: it supports multi-day
        polls and some ranked-choice voting, alongside its core strengths &mdash; motions,
        amendments, quorum tracking, and projector-ready floor process this app doesn't
        attempt. Where this app pulls ahead is <b>flexibility built for exactly this
        election</b>: the Scottish STV <b>two-count alternates recount</b> as a built-in
        rule, code-gated voting with no accounts or logins to provision for 120k members,
        codes generated straight from the deduplicated membership roll, SMS and postcard
        delivery tiers with reminder waves, self-serve provisional ballots, per-section
        visibility (named poll votes beside a secret delegate ballot), chapter-unique
        questions and local issues on one styled page, randomized candidate order per
        voter, a free-form comment field, and per-chapter voting windows on a single
        deployment. Each of those would be a customization project elsewhere; here
        they're the design.</p>
        <p><b>Cost &amp; ops.</b> Running a live assembly well takes real event
        engineering &mdash; hosting, load testing, venue network planning &mdash; because
        everything must work in the moment, in the room. Asynchronous voting is far more
        forgiving: this app is ~$10 of infrastructure plus message delivery, and if a
        voter's request hiccups they simply tap again &mdash; a resilience that comes free
        with the multi-day format.</p>
        </div>
      </details>

      <details>
        <summary>Admin FAQ</summary>
        <div class="dbody">
        <p><b>Can test votes pollute results?</b> No &mdash; the repeatable test codes above
        return a full voting experience but write nothing.</p>
        <p><b>Can a user vote more than once?</b> No. One code = one vote, enforced by an
        atomic transaction on the voter's code document &mdash; even two simultaneous
        submissions can't both succeed; the second gets "already voted." A member's email
        and phone lead to the same single code (codes are issued per deduplicated member,
        not per contact), and provisional ballots are checked against the member's code
        status at adjudication, so provisional + coded can't both count.</p>
        <p><b>Can a user change their vote?</b> No. Votes are final once cast &mdash; the
        ballot says so at review before casting. There is no edit or revote path in the
        software, administrators have no ballot-editing capability, and the post-close
        hash chain would make any altered or substituted ballot record evident.</p>
        <p><b>What if a ballot is compromised (stolen code, technical failure)?</b>
        Administrators can <b>void and reissue</b> &mdash; never edit. The cast ballot is
        flagged voided (kept forever in the record and the hash chain, excluded from
        tallies), an append-only audit entry records who/why/when, and the member gets a
        fresh code to vote again. Only while the poll is open, only for pre-committed
        reasons (stolen code, technical failure, provisional adjudication &mdash; never
        "changed my mind"), and aggregate void counts are disclosed at certification.
        Administrators control <i>whether</i> a ballot counts, with a paper trail &mdash;
        never <i>what it says</i>.</p>
        <p><b>Who can see how someone voted?</b> Poll questions &amp; local issues:
        election administrators and the voter's chapter. Delegate rankings: no one by
        name &mdash; administrators can trace a specific delegate ballot only via the
        code mapping, for troubleshooting, under audit. Disclosed to voters on the
        ballot. National does not publish chapter results; each chapter decides whether
        to publish its own.</p>
        <p><b>Can results be seen early?</b> Live tallies are admin-only via the data
        warehouse (audited); nothing public until every chapter closes.</p>
        <p><b>What if a voter never got a code?</b> Resend to their on-record contact, or
        they cast a sealed provisional ballot with identifying details for staff
        verification.</p>
        <p><b>What happens at close?</b> Each chapter's poll closes on its own schedule;
        the close-out job exports, chains, hashes, and publishes that chapter's package;
        national results aggregate after all chapters close.</p>
        </div>
      </details>

    </div>
  </div>
  <div class="marks" style="margin-top:18px"></div>
</main>
<footer><b>Democratic Socialists of America</b> &middot; Chapter Member Ballot &middot; Prototype build &mdash; not a live election.<br/>Questions? Email <b>orgtools@dsausa.org</b><br/>Built by DSA Staff&rsquo;s Data &amp; Tech Department</footer>
</body></html>"""


@app.get("/")
def index():
    """Branded splash: per-chapter repeatable test codes + chapter ballot links."""
    polls = load_polls()
    links = "".join(f'<li><a href="/p/{pid}/">{cfg["name"]}</a></li>' for pid, cfg in polls.items())
    rows = "".join(
        f'<p style="margin:0 0 4px"><b>{cfg["name"]}</b><br/>'
        f'<code class="tc" style="font-size:.86rem">{cfg["test_code"]}</code></p>'
        f'<a class="btn" style="min-height:40px;font-size:1.05rem;margin:6px 0 14px" '
        f'href="/p/{pid}/v/{cfg["test_code"]}">Open {cfg["name"]} ballot with test code &rarr;</a>'
        for pid, cfg in polls.items() if cfg.get("test_code")
    )
    html = SPLASH.replace("__TEST_ROWS__", rows).replace("__CHAPTER_LINKS__", links)
    return Response(html, mimetype="text/html")


# ---- admin auth: scoped tokens -------------------------------------------
# Two tiers, both via the X-Admin-Token header:
#   * ADMIN_TOKEN env var — national break-glass root token. If unset AND no
#     tokens exist in config__admins, all admin surface stays disabled (safe
#     default for public staging). Production should sit behind IAM/IAP too.
#   * config__admins — Firestore docs keyed by SHA-256(token):
#     {name, role: "national"|"chapter", polls: [poll_id...], active}.
#     Minted by tools/seed_config.py admin; plaintext is never stored.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
ADMINS_COLL = "config__admins"

VOID_REASONS = {"stolen_code", "technical_failure", "provisional_adjudication"}


def _admin_identity(token: str):
    if not token or len(token) > 128:
        return None
    if ADMIN_TOKEN and secrets.compare_digest(token, ADMIN_TOKEN):
        return {"name": "root", "role": "national", "polls": []}
    try:
        snap = db.collection(ADMINS_COLL).document(code_hash(token)).get()
    except Exception:
        return None
    if not getattr(snap, "exists", False):
        return None
    d = snap.to_dict() or {}
    if not d.get("active", True):
        return None
    return {"name": d.get("name", "?"), "role": d.get("role", "chapter"),
            "polls": list(d.get("polls") or [])}


def require_admin(poll_id: str = None, national_only: bool = False):
    """Resolve the caller's admin identity, or None if unauthorized.
    Chapter-scoped tokens only reach polls in their own list."""
    ident = _admin_identity(request.headers.get("X-Admin-Token", ""))
    if not ident:
        return None
    if ident["role"] == "national":
        return ident
    if national_only:
        return None
    if poll_id is not None and poll_id not in ident["polls"]:
        return None
    return ident


def _audit(poll_id: str, action: str, admin: str, **fields):
    db.collection(f"{poll_id}__audit_log").document(secrets.token_hex(16)).set(
        {"action": action, "admin": admin, "at": firestore.SERVER_TIMESTAMP, **fields})


# ---- admin: election builder (config CRUD w/ Art. V validation) -----------
POLL_ID_RE = re.compile(r"^[a-z0-9_]{3,64}$")
CAND_ID_RE = re.compile(r"^[A-Z0-9_]{2,32}$")
ART5_MIN_DAYS = 45          # Const. Art. V §5: ≥45 days before convention...
ART5_MAX_MONTHS = 4         # ...and ≤4 months before it, post-apportionment.


def _months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def validate_poll_config(poll_id: str, body: dict):
    """Election-builder validation. Returns (normalized_cfg, errors, warnings).
    Art. V §5 is enforced structurally: when a convention date is given, the
    whole voting window must sit inside [convention − 4 months,
    convention − 45 days] and apportionment must be confirmed done."""
    errors, warnings = [], []
    if not POLL_ID_RE.match(poll_id or ""):
        errors.append("poll_id must match [a-z0-9_]{3,64}")

    name = str(body.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    q6 = str(body.get("q6") or "").strip()
    q8 = str(body.get("q8") or "").strip()
    if not q6 or not q8:
        errors.append("both local-issue questions (q6, q8) are required")
    test_code = str(body.get("test_code") or "").strip()
    if test_code and not CODE_RE.match(test_code):
        errors.append("test_code must match [A-Za-z0-9_-]{12,64}")

    def ts(key):
        v = body.get(key)
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            errors.append(f"{key} must be unix seconds")
            return None
    opens_at, closes_at = ts("opens_at"), ts("closes_at")
    if opens_at and closes_at and opens_at >= closes_at:
        errors.append("opens_at must be before closes_at")

    q7in = body.get("q7") or {}
    def n(key, minimum):
        try:
            v = int(q7in.get(key, 0))
        except (TypeError, ValueError):
            v = -1
        if v < minimum:
            errors.append(f"q7.{key} must be an integer ≥ {minimum}")
        return max(v, 0)
    seats = n("seats", 1)
    alternates = n("alternates", 0)
    real_delegates = n("real_delegates", 0)
    real_alternates = n("real_alternates", 0)

    cands, seen = [], set()
    for c in (q7in.get("candidates") or []):
        cid = str((c.get("id") if isinstance(c, dict) else c[0]) or "").strip().upper()
        cname = str((c.get("name") if isinstance(c, dict) else c[1]) or "").strip()
        if not CAND_ID_RE.match(cid) or cid == "ABSTAIN":
            errors.append(f"bad candidate id {cid!r} ([A-Z0-9_]{{2,32}}, ABSTAIN reserved)")
        elif cid in seen:
            errors.append(f"duplicate candidate id {cid!r}")
        elif not cname:
            errors.append(f"candidate {cid!r} needs a display name")
        else:
            seen.add(cid)
            cands.append((cid, cname))
    if not cands:
        errors.append("q7 needs at least one candidate")
    elif len(cands) <= seats + alternates:
        warnings.append(f"uncontested: {len(cands)} candidate(s) for "
                        f"{seats} seat(s) + {alternates} alternate(s)")

    conv_raw = str(body.get("convention_date") or "").strip()
    apportioned = bool(body.get("apportionment_done"))
    if conv_raw:
        try:
            conv = date.fromisoformat(conv_raw)
        except ValueError:
            conv = None
            errors.append("convention_date must be YYYY-MM-DD")
        if conv:
            if not apportioned:
                errors.append("Art. V: delegates must be apportioned before the election")
            if not (opens_at and closes_at):
                errors.append("Art. V: a delegate election needs an explicit voting window")
            else:
                earliest = _months_before(conv, ART5_MAX_MONTHS)
                latest = conv - timedelta(days=ART5_MIN_DAYS)
                w_open = date.fromtimestamp(opens_at)
                w_close = date.fromtimestamp(closes_at)
                if w_open < earliest:
                    errors.append(f"Art. V: opens {w_open}, but no earlier than "
                                  f"{earliest} (4 months before convention)")
                if w_close > latest:
                    errors.append(f"Art. V: closes {w_close}, but no later than "
                                  f"{latest} (45 days before convention)")
    else:
        warnings.append("no convention_date — Art. V delegate-window validation skipped")

    cfg = {
        "name": name, "opens_at": opens_at, "closes_at": closes_at,
        "test_code": test_code or None, "q6": q6, "q8": q8,
        "convention_date": conv_raw or None, "apportionment_done": apportioned,
        "q7": {"seats": seats, "alternates": alternates,
               "real_delegates": real_delegates, "real_alternates": real_alternates,
               "candidates": cands},
    }
    return cfg, errors, warnings


ADMIN_CONSOLE = open(os.path.join(os.path.dirname(__file__), "admin_console.html")).read()


@app.get("/admin/")
def admin_console_page():
    """Static console shell — every API it calls is token-gated."""
    return Response(ADMIN_CONSOLE, mimetype="text/html")


@app.get("/admin/api/whoami")
def admin_whoami():
    ident = require_admin()
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(ident), 200


@app.get("/admin/api/polls")
def admin_list_polls():
    ident = require_admin()
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    polls = load_polls(force=True)
    if ident["role"] != "national":
        polls = {pid: cfg for pid, cfg in polls.items() if pid in ident["polls"]}
    return jsonify({pid: dict(cfg_to_doc(cfg), state=window_state(cfg))
                    for pid, cfg in polls.items()}), 200


@app.post("/admin/api/polls/<poll_id>")
def admin_save_poll(poll_id):
    """Create or update a poll config (national admins only)."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg, errors, warnings = validate_poll_config(poll_id, request.get_json(silent=True) or {})
    if errors:
        return jsonify({"error": "invalid_config", "errors": errors,
                        "warnings": warnings}), 400
    db.collection(CONFIG_COLL).document(poll_id).set(cfg_to_doc(cfg))
    _audit(poll_id, "config_save", ident["name"])
    load_polls(force=True)
    return jsonify({"ok": True, "warnings": warnings}), 200


@app.post("/admin/api/polls/<poll_id>/close")
def admin_close_poll(poll_id):
    """Close-out button: closes the window now (upserts the config doc, so it
    also works while still running off the CHAPTERS seed)."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    cfg = dict(cfg, closes_at=int(time.time()))
    db.collection(CONFIG_COLL).document(poll_id).set(cfg_to_doc(cfg))
    _audit(poll_id, "close_poll", ident["name"])
    load_polls(force=True)
    return jsonify({"ok": True, "closes_at": cfg["closes_at"]}), 200


@app.post("/admin/api/cron/closeout")
def cron_closeout():
    """Close-out automation (Cloud Scheduler hits this every 15 min with a
    national token): finalize every poll whose window has closed. Finalizing
    snapshots the final counts into the config doc and the audit log —
    idempotent, so repeated runs are no-ops. The heavy export (hash chain,
    BLTs, results packages) still runs from tools/ against the frozen data."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    now = int(time.time())
    finalized = {}
    for pid, cfg in load_polls(force=True).items():
        if not cfg.get("closes_at") or now <= cfg["closes_at"] or cfg.get("finalized"):
            continue
        n_ballots = n_voided = 0
        for snap in db.collection(f"{pid}__ballots").stream():
            if (snap.to_dict() or {}).get("voided"):
                n_voided += 1
            else:
                n_ballots += 1
        n_pending = sum(1 for _ in db.collection(f"{pid}__provisional")
                        .where("status", "==", "pending").stream())
        counts = {"ballots": n_ballots, "voided": n_voided,
                  "provisional_pending": n_pending}
        db.collection(CONFIG_COLL).document(pid).set(cfg_to_doc(dict(
            cfg, finalized=True, finalized_at=now, final_counts=counts)))
        _audit(pid, "closeout_finalize", ident["name"], counts=counts)
        finalized[pid] = counts
    if finalized:
        load_polls(force=True)
    return jsonify({"finalized": finalized}), 200


# ---- admin: provisional adjudication queue --------------------------------
@app.get("/admin/api/polls/<poll_id>/provisionals")
def admin_list_provisionals(poll_id):
    """Pending provisionals — identity fields only. The ballot answers stay
    sealed; adjudicators match the person, not the vote."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    if not chapter_or_none(poll_id):
        return jsonify({"error": "unknown_poll"}), 404
    out = []
    for snap in db.collection(f"{poll_id}__provisional").where("status", "==", "pending").stream():
        d = snap.to_dict() or {}
        out.append({k: d.get(k, "") for k in
                    ("receipt", "first", "last", "emails", "phones",
                     "chapter", "join_date", "alt_names")})
    return jsonify({"pending": out}), 200


@app.post("/admin/api/polls/<poll_id>/provisionals/<receipt>")
def admin_adjudicate_provisional(poll_id, receipt):
    """Adjudicate a sealed provisional: verify (promote into the real ballot
    collections + burn the member's unused codes) or reject. Allowed after
    close — adjudication routinely happens between close and certification,
    before the hash chain is built."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    if not chapter_or_none(poll_id):
        return jsonify({"error": "unknown_poll"}), 404
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("verify", "reject"):
        return jsonify({"error": "bad_request", "message": "action must be verify|reject"}), 400

    ref = db.collection(f"{poll_id}__provisional").document(receipt)
    snap = ref.get()
    if not getattr(snap, "exists", False):
        return jsonify({"error": "receipt_not_found"}), 404
    prov = snap.to_dict() or {}
    if prov.get("status") != "pending":
        return jsonify({"error": "already_adjudicated", "status": prov.get("status")}), 409

    if action == "reject":
        ref.update({"status": "rejected", "note": str(data.get("note", "")),
                    "adjudicated_by": ident["name"], "adjudicated_at": firestore.SERVER_TIMESTAMP})
        _audit(poll_id, "provisional_reject", ident["name"], receipt=receipt)
        return jsonify({"ok": True, "status": "rejected"}), 200

    member_id = str(data.get("member_id") or "").strip()
    if not member_id:
        return jsonify({"error": "bad_request",
                        "message": "verify requires the matched member_id"}), 400

    # one-member-one-vote guard: if any of their codes was used, this
    # provisional cannot also count.
    codes = db.collection(f"{poll_id}__codes")
    member_codes = list(codes.where("member_id", "==", member_id).stream())
    if any((c.to_dict() or {}).get("used") for c in member_codes):
        return jsonify({"error": "member_already_voted"}), 409
    for c in member_codes:
        c.reference.update({"used": True, "burned_by": "provisional_adjudication"})

    # promote the sealed answers into the real collections, same split as
    # cast_vote: identity-linked main record (no q7) + secret delegate ballot.
    answers = prov.get("answers") or {}
    main_answers = {k: v for k, v in answers.items() if k != "q7"}
    nonce = secrets.token_hex(8)
    ac = canon_answers(main_answers)
    rh = make_record_hash(receipt, ac, nonce)
    db.collection(f"{poll_id}__ballots").document(rh).set({
        "receipt": receipt, "answers": main_answers, "answers_canon": ac,
        "nonce": nonce, "record_hash": rh,
        "member_id": member_id, "chapter": prov.get("chapter"),
        "code_hash": None, "provisional": True,
        "comment": prov.get("comment", ""),
        "day_bucket": firestore.SERVER_TIMESTAMP,
    })
    dq = {"q7": answers.get("q7") or []}
    dnonce = secrets.token_hex(8)
    dcanon = canon_answers(dq)
    drh = make_record_hash(receipt, dcanon, dnonce)
    db.collection(f"{poll_id}__delegate_ballots").document(drh).set({
        "receipt": receipt, "q7": dq["q7"], "answers_canon": dcanon,
        "nonce": dnonce, "record_hash": drh,
        "code_hash": None, "provisional": True,
        "day_bucket": firestore.SERVER_TIMESTAMP,
    })
    ref.update({"status": "verified", "member_id": member_id,
                "adjudicated_by": ident["name"], "adjudicated_at": firestore.SERVER_TIMESTAMP})
    _audit(poll_id, "provisional_verify", ident["name"], receipt=receipt, member_id=member_id)
    return jsonify({"ok": True, "status": "verified", "receipt": receipt}), 200


# ---- admin: void-and-reissue (never edit) --------------------------------
@app.post("/p/<poll_id>/admin/void")
def admin_void(poll_id):
    """Void a cast ballot (flag, never delete) and reissue a fresh code.
    Administrators control WHETHER a ballot counts — with an audit trail —
    never WHAT it says. Permitted only while the chapter's window is open,
    for pre-committed reasons. Voided records stay in the export, flagged,
    excluded from tallies, disclosed in aggregate at certification."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    if window_state(cfg) != "open":
        return jsonify({"error": "window_closed",
                        "message": "Void/reissue is only available while the poll is open."}), 403

    data = request.get_json(silent=True) or {}
    receipt = str(data.get("receipt", "")).strip().upper()
    reason = str(data.get("reason", "")).strip()
    admin = str(data.get("admin", "")).strip() or ident["name"]
    if not receipt or reason not in VOID_REASONS:
        return jsonify({"error": "bad_request",
                        "message": f"receipt and reason (one of {sorted(VOID_REASONS)}) required"}), 400

    ballots = db.collection(f"{poll_id}__ballots")
    matches = list(ballots.where("receipt", "==", receipt).stream())
    if not matches:
        return jsonify({"error": "receipt_not_found"}), 404
    main = matches[0]
    md = main.to_dict()
    if md.get("voided"):
        return jsonify({"error": "already_voided"}), 409

    # 1. flag (never delete) — main + matching secret delegate ballot
    void_fields = {"voided": True, "void_reason": reason,
                   "voided_by": admin, "voided_at": firestore.SERVER_TIMESTAMP}
    main.reference.update(void_fields)
    for doc in db.collection(f"{poll_id}__delegate_ballots").where("receipt", "==", receipt).stream():
        doc.reference.update(void_fields)

    # 2. reissue: fresh code for the same member (old code stays burned)
    new_code = secrets.token_urlsafe(12)
    new_hash = code_hash(new_code)
    db.collection(f"{poll_id}__codes").document(new_hash).set({
        "used": False,
        "member_id": md.get("member_id"),
        "chapter": md.get("chapter"),
        "reissued_from": md.get("code_hash"),
    })

    # 3. append-only audit record
    db.collection(f"{poll_id}__audit_log").document(secrets.token_hex(16)).set({
        "action": "void_reissue", "receipt": receipt, "reason": reason,
        "admin": admin, "old_code_hash": md.get("code_hash"),
        "new_code_hash": new_hash, "at": firestore.SERVER_TIMESTAMP,
    })
    # plaintext code returned ONCE for delivery to the voter; only its hash is stored
    return jsonify({"ok": True, "receipt_voided": receipt, "new_code": new_code}), 200


@app.get("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
