# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
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

import base64
import calendar
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
from datetime import date, timedelta

from flask import Flask, request, jsonify, Response, redirect, g
from google.cloud import firestore
from google.api_core import exceptions as gcloud_exc

app = Flask(__name__)

# Hard cap on request bodies (Flask answers 413 past it). Without one, any
# unauthenticated POST — /vote, /provisional — can stream an arbitrarily large
# JSON body that the app buffers and parses before a single validation rule
# runs. The largest legitimate body is a console roll import (20k members),
# which fits inside this comfortably.
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

# Canonical host. When both rosavote.com and rosavote.org point at this service,
# any request to a non-canonical host is 301-redirected to CANONICAL_HOST — so
# the .com → .org redirect works regardless of registrar/CDN. Set CANONICAL_HOST
# (e.g. "vote.rosavote.org") in the environment; unset = no redirect (dev/Cloud
# Run default URL). Health checks and the ACME challenge path are never redirected.
CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "").strip().lower()

# Split hosting: the marketing page is served on the apex (rosavote.org) and the
# app itself on APP_HOST (app.rosavote.org). MARKETING_HOSTS get the pitch page
# at "/"; every other host (the app subdomain, the run.app URL) gets the app
# splash. APP_ORIGIN is where the marketing page's "open the app" links point
# (empty = same-origin, for the /about copy on the app host).
MARKETING_HOSTS = {h.strip().lower() for h in
                   os.environ.get("MARKETING_HOSTS", "rosavote.org,www.rosavote.org").split(",")
                   if h.strip()}
APP_ORIGIN = os.environ.get("APP_ORIGIN", "").strip().rstrip("/")

# AGPL-3.0 §13: users interacting with the (possibly modified) program over a
# network must be offered its Corresponding Source. SOURCE_URL is that public
# offer — surfaced in the footer of every page. Set it to the repository (or a
# tagged release) the running deployment was built from.
SOURCE_URL = os.environ.get(
    "SOURCE_URL", "https://github.com/walkergreen/rosavote")


@app.before_request
def _canonical_host_redirect():
    if not CANONICAL_HOST:
        return None
    host = (request.host or "").split(":", 1)[0].lower()
    if not host or host == CANONICAL_HOST:
        return None
    if request.path.startswith("/.well-known/"):   # ACME / domain verification
        return None
    target = f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}"
    return redirect(target, code=301)


@app.after_request
def _security_headers(resp):
    # Defense-in-depth behind the admin-HTML sanitizer. connect-src 'self' is
    # the load-bearing one: even if script somehow reached a page, it could
    # not POST captured votes/codes to an off-origin server. Inline scripts
    # are the app's own (templates); admin prose is sanitized of <script>.
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


class _LazyDB:
    """Defers firestore.Client() to first use so the module imports (and
    /healthz serves) without credentials — tools that only need the CHAPTERS
    seed, and local shells without ADC, can import vote_service freely."""
    _client = None

    def __getattr__(self, name):
        if _LazyDB._client is None:
            # FIRESTORE_DATABASE selects a named database (e.g. "staging") so a
            # staging deployment is fully isolated from production data in the
            # same project; unset uses the "(default)" database (production).
            dbname = os.environ.get("FIRESTORE_DATABASE", "").strip()
            _LazyDB._client = (firestore.Client(database=dbname) if dbname
                               else firestore.Client())
        return getattr(_LazyDB._client, name)


db = _LazyDB()

# ---- generic ballot schema ------------------------------------------------
# Every poll is an ordered list of QUESTIONS. Types:
#   yesno  — YES / NO (+ Abstain unless allow_abstain=False)
#   ranked — Scottish STV; options [{id,name,sub?}], seats, alternates
#            (alternates>0 => expanded/replacement count + over-seat rank styling),
#            secret=True stores the ranking in the admin-only secret
#            collection, delegate=True additionally applies Art. V rules,
#            shuffle=True randomizes option order per page load
#   multi  — optional multi-select with exclusive Abstain
#   text   — optional free text (max chars); stored as an identity-linked
#            comment on the main record, NEVER in the canonical answers, so
#            the publishable ballot file can't leak identifying prose
# Legacy configs (the CHAPTERS seed and early Firestore docs) don't carry a
# questions list — demo_questions() expresses the original combined 8-question
# ballot in this schema so they render identically.
TEXT_MAX_DEFAULT = 1000


def _q7_note(cfg) -> str:
    q7 = cfg["q7"]
    real = (f"At the national convention {cfg['name']} elects {q7['real_delegates']} "
            f"delegates and {q7['real_alternates']} alternates (2025 apportionment, "
            f"1:60 and 1:10). ") if q7.get("real_delegates") else ""
    total = q7["seats"] + q7["alternates"]
    return (real + f"<b>How this is counted (expanded count):</b> delegates are "
            f"decided by a Scottish STV count for {q7['seats']} seats. The same "
            f"ballots are then recounted for {total} seats — anyone elected in "
            "the recount who is not already a delegate becomes an alternate, in "
            "order of election. Every preference you rank can matter in both "
            "counts, so rank as many candidates as you like (a first choice is "
            "required) — or tap Abstain to skip. Candidates appear in random "
            "order, freshly shuffled for each voter. Ranks past the first "
            f"{q7['seats']} turn black — those preferences still count and "
            "help decide the alternates.")


def demo_questions(cfg) -> list:
    """The original combined ballot, expressed in the generic schema.
    Chapter-specific pieces come from the legacy q6/q8/q7 config fields."""
    q7 = cfg["q7"]
    alts = q7["alternates"]
    return [
        {"key": "q1", "type": "yesno", "label": "Endorsement",
         "section": {"style": 1, "kicker": "Section 1 of 3 · Shared questions",
                     "title": "Chapter Poll",
                     "sub": "Questions 1–5 are polled identically in every chapter — an "
                            "advisory expression of the membership, reported per chapter "
                            "and aggregated nationally."},
         "title": "Shall DSA endorse Eugene V. Debs for President of the United States?",
         "text": ["<em>Resolved,</em> that the Democratic Socialists of America endorses "
                  "Eugene V. Debs — founder of the American Railway Union, leader of the "
                  "1894 Pullman strike, five-time Socialist Party of America candidate for "
                  "President, and Convict No. 9653, who polled nearly a million votes in "
                  "1920 from a cell in the Atlanta Federal Penitentiary after his "
                  "conviction under the Espionage Act for speaking against the war — for "
                  "President of the United States;",
                  "<em>Resolved,</em> that the campaign shall carry the platform he ran "
                  "on: collective ownership of the railroads, mines, and utilities; the "
                  "eight-hour day and the abolition of child labor; industrial unionism "
                  "as the engine of working-class power; equal suffrage; amnesty for "
                  "political prisoners and repeal of the Espionage Act; and unconditional "
                  "opposition to imperialist war; and",
                  "<em>Resolved,</em> that the campaign be conducted in his spirit — "
                  "<em>“while there is a lower class, I am in it; while there is a "
                  "criminal element, I am of it; while there is a soul in prison, I am "
                  "not free”</em> — organizing the working class rather than "
                  "courting donors, and that this endorsement may be withdrawn by a "
                  "subsequent vote of the membership. <em>(Draft test language — Debs, "
                  "1855–1926, stands in for an eventual nominee.)</em>"],
         "option_subs": {"YES": "Endorse", "NO": "Do not endorse",
                         "ABSTAIN": "Counted for quorum only"}},
        {"key": "q2", "type": "ranked", "label": "Campaign structure — ranked choice",
         "title": "Rank the campaign structures",
         "text": ["Counted by <b>Scottish STV</b>. Tap options in order of preference — "
                  "1 is your first choice; tap again to remove. Rank as many or as few "
                  "as you like (at least a first choice) — or tap Abstain to skip."],
         "options": [{"id": "IE", "name": "Independent expenditure", "sub": "DSA runs its own program"},
                     {"id": "COORD", "name": "Coordinated campaign", "sub": "Work directly with the campaign"},
                     {"id": "NOEND", "name": "No endorsement", "sub": "Run no presidential campaign"},
                     {"id": "NOTA", "name": "None of the above", "sub": ""}]},
        {"key": "q3", "type": "ranked", "label": "The 1912 field — ranked choice",
         "title": "Rank the candidates",
         "text": ["Test question using Debs’s actual 1912 opponents. Counted by "
                  "<b>Scottish STV</b> — same rules as above."],
         "options": [{"id": "DEBS", "name": "Eugene V. Debs", "sub": "Socialist Party"},
                     {"id": "WILSON", "name": "Woodrow Wilson", "sub": "Democratic Party"},
                     {"id": "ROOSEVELT", "name": "Theodore Roosevelt", "sub": "Progressive “Bull Moose” Party"},
                     {"id": "TAFT", "name": "William Howard Taft", "sub": "Republican Party"},
                     {"id": "NOTA2", "name": "None of the above", "sub": ""}]},
        {"key": "pledges", "type": "multi", "label": "Pledges — check all that apply, optional",
         "title": "If the campaign moves forward, I pledge to…",
         "options": [{"id": "DONATE", "name": "Donate", "sub": "Chip in to the campaign fund"},
                     {"id": "VOLUNTEER", "name": "Volunteer", "sub": "General campaign work"},
                     {"id": "CANVASS", "name": "Canvass", "sub": "Knock doors with my chapter"},
                     {"id": "PHONEBANK", "name": "Phone bank", "sub": "Call voters and members"},
                     {"id": "TEXTBANK", "name": "Text bank", "sub": "Send campaign texts"},
                     {"id": "HOST", "name": "Host", "sub": "House meeting or debate watch party"}],
         "abstain_sub": "No pledges"},
        {"key": "text", "type": "text", "label": "What should the NPC know? — optional",
         "title": "Anything the NPC should know?",
         "text": ["Free form — priorities, concerns, conditions on the endorsement. "
                  "Stored with your ballot record (election administrators only, never "
                  "published). Leave blank to abstain."],
         "max": 1000},
        {"key": "q7", "type": "ranked", "label": "Convention delegates — ranked choice",
         "section": {"style": 2, "kicker": f"Section 2 of 3 · {cfg['name']}",
                     "title": "Convention Delegates",
                     "sub": "Your chapter's delegation to the national convention — ranked "
                            "choice, counted by Scottish STV. <b>Secret ballot</b> (Const. "
                            "Art. V &sect;5): your ranking is stored without your name or "
                            "chapter. Election administrators can access delegate ballots "
                            "only for troubleshooting, under audit."},
         "title": f"Elect {q7['seats']} delegates + {alts} alternate" + ("" if alts == 1 else "s"),
         "text": [_q7_note(cfg) + " <em>(Test slate — historical figures, obviously not running.)</em>"],
         "options": [{"id": cid, "name": name, "sub": ""} for cid, name in q7["candidates"]],
         "seats": q7["seats"], "alternates": alts,
         "secret": True, "delegate": True, "shuffle": True},
        {"key": "q6", "type": "yesno", "label": "Local issue 1",
         "section": {"style": 3, "kicker": f"Section 3 of 3 · {cfg['name']}",
                     "title": "Local Issues",
                     "sub": f"Ballot issues specific to {cfg['name']} — these appear only "
                            "on your chapter's ballot."},
         "title": cfg["q6"],
         "text": [f"This question appears only on the {cfg['name']} ballot."]},
        {"key": "q8", "type": "yesno", "label": "Local issue 2",
         "title": cfg["q8"],
         "text": [f"Second local ballot issue for {cfg['name']}."]},
    ]


def poll_questions(cfg) -> list:
    return cfg.get("questions") or demo_questions(cfg)


# ---- visibility: who may learn how a given member voted -------------------
# Bodies differ, and the difference is per QUESTION, not per poll: the same
# meeting can take a recorded roll-call vote on a motion and a secret ballot
# for officers.
#   public — identity-linked AND published by name (recorded roll call)
#   named  — identity-linked; admins + the voter's own chapter; only
#            aggregates are published. The default, and what every existing
#            non-secret question already does.
#   secret — content stored with NO identity, in the admin-only collection
#            (Const. Art. V §5 delegate elections, and any question flagged
#            secret). Published as an anonymous ballot file so the result is
#            still independently recountable.
VISIBILITIES = ("public", "named", "secret")


def q_visibility(q) -> str:
    """A question's visibility. Legacy configs predate the field: `secret`
    (and `delegate`, which implies it) meant the secret storage split, and
    everything else was named."""
    v = str(q.get("visibility") or "").strip().lower()
    if v in VISIBILITIES:
        return v
    return "secret" if (q.get("secret") or q.get("delegate")) else "named"


def secret_keys(cfg) -> list:
    return [q["key"] for q in poll_questions(cfg) if q_visibility(q) == "secret"]


def public_keys(cfg) -> list:
    """Questions published as a by-name roll call."""
    return [q["key"] for q in poll_questions(cfg)
            if q_visibility(q) == "public" and q["type"] != "text"]


def validate_answers(data, cfg) -> tuple[dict, dict]:
    """Validate submitted answers against the poll's question schema.
    Returns (answers, comments): text-type answers are split out into
    `comments` and never enter the canonical answer record.
    Raises ValueError(question_key) on anything malformed."""
    if not isinstance(data, dict):
        raise ValueError("answers")
    answers, comments = {}, {}
    for q in poll_questions(cfg):
        key, typ = q["key"], q["type"]
        v = data.get(key)
        if typ == "yesno":
            s = str(v or "").upper()
            allowed = {"YES", "NO"} | ({"ABSTAIN"} if q.get("allow_abstain", True) else set())
            if s not in allowed:
                raise ValueError(key)
            answers[key] = s
        elif typ == "ranked":
            ids = {o["id"] for o in q["options"]}
            if not isinstance(v, list) or not v:
                raise ValueError(key)
            v = [str(x).upper() for x in v]
            if v != ["ABSTAIN"] and (len(set(v)) != len(v) or not set(v) <= ids):
                raise ValueError(key)
            if q.get("require_full") and v != ["ABSTAIN"] and len(v) != len(ids):
                raise ValueError(key)      # complete-ranking enforced
            answers[key] = v
        elif typ == "multi":
            v = v or []
            if not isinstance(v, list):
                raise ValueError(key)
            v = [str(x).upper() for x in v]
            ids = {o["id"] for o in q["options"]}
            if v != ["ABSTAIN"] and (len(set(v)) != len(v) or not set(v) <= ids):
                raise ValueError(key)
            answers[key] = v if v == ["ABSTAIN"] else sorted(v)
        elif typ == "score":
            ids = {o["id"] for o in q["options"]}
            max_s = int(q.get("max_score", 2))
            if v == "ABSTAIN" or v == ["ABSTAIN"]:
                if not q.get("allow_abstain", True):
                    raise ValueError(key)
                answers[key] = "ABSTAIN"
                continue
            if not isinstance(v, dict) or not v:
                raise ValueError(key)
            scores = {}
            for oid, s in v.items():
                oid = str(oid).upper()
                if oid not in ids:
                    raise ValueError(key)
                try:
                    si = int(s)
                except (TypeError, ValueError):
                    raise ValueError(key)
                if si < 0 or si > max_s:
                    raise ValueError(key)
                scores[oid] = si
            # spec: every candidate must be scored (unless partial allowed)
            if q.get("require_full", True) and set(scores) != ids:
                raise ValueError(key)
            answers[key] = scores
        elif typ == "text":
            comments[key] = str(v or "")[: q.get("max", TEXT_MAX_DEFAULT)]
    return answers, comments


def canon_answers(a: dict) -> str:
    """Deterministic serialization — MUST match tools/verify.py + build_chain.py."""
    return json.dumps(a, separators=(",", ":"), sort_keys=True)


def _js_json(obj) -> str:
    """json.dumps hardened for embedding inside an inline <script> element.
    HTML parsing beats JS parsing, so a bare "</script>" (or a stray "<") in
    any string value — e.g. an admin-set candidate name or question title —
    would end the script element and let following markup execute. Escape the
    characters that can break out of the element or the JS string so the blob
    is inert as markup but still valid JSON/JS."""
    return (json.dumps(obj)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

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
        polls, n_docs = {}, 0
        for snap in db.collection(CONFIG_COLL).stream():
            d = snap.to_dict() or {}
            n_docs += 1
            if d.get("archived"):
                continue
            polls[snap.id] = _normalize_cfg(d)
        # Only a COMPLETELY UNSEEDED collection falls back to the built-in
        # seed. "Every poll archived" is a real, intentional state — it must
        # not resurrect the demo chapters as live, votable ballots.
        if not n_docs:
            polls = CHAPTERS
    except Exception:
        # Firestore unreachable: keep serving the LAST GOOD config. Falling
        # back to the built-in seed mid-election would hand voters a
        # different ballot (demo questions, demo test codes, demo windows)
        # for the same poll_id. Back off for the TTL so a down backend isn't
        # hammered once per request; only an instance that has never loaded a
        # config at all uses the seed.
        _cfg_cache["at"] = now
        stale = _cfg_cache["polls"]
        return stale if stale is not None else CHAPTERS
    _cfg_cache.update(at=now, polls=polls)
    return polls


def _fresh_cfg_doc(poll_id: str):
    """The poll's config doc read STRAIGHT from Firestore, bypassing the 60s
    cache. Any read-modify-write of a config doc must start here: writing back
    a cached copy silently reverts whatever another admin (or another Cloud
    Run instance) saved inside the TTL window."""
    try:
        snap = db.collection(CONFIG_COLL).document(poll_id).get()
        if getattr(snap, "exists", False):
            return _normalize_cfg(snap.to_dict() or {})
    except Exception:
        return None
    return None


def chapter_or_none(poll_id: str):
    cfg = load_polls().get(poll_id)
    if cfg is None:
        # a just-created poll may not be in this instance's cached set yet
        # (60s TTL, per-instance). One forced refresh closes the
        # create-then-immediately-use race across Cloud Run instances.
        cfg = load_polls(force=True).get(poll_id)
    return cfg


def poll_tz(cfg):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(cfg.get("timezone") or "America/New_York")
    except Exception:
        return ZoneInfo("America/New_York")


def fmt_local(ts, cfg) -> str:
    from datetime import datetime
    if not ts:
        return "—"
    d = datetime.fromtimestamp(ts, poll_tz(cfg))
    return d.strftime("%B %-d, %Y at %-I:%M %p %Z")


def window_state(cfg):
    # A finalized poll never reopens implicitly — EXCEPT a `demo` poll, which
    # may be finalized (so its results page stays live) yet still accept test
    # votes for people trying the app. Test-code votes are never stored, so
    # demo voting can't change the published result.
    if cfg.get("finalized") and not cfg.get("demo"):
        return "closed"
    now = time.time()
    if cfg.get("opens_at") and now < cfg["opens_at"]:
        return "not_open"
    if cfg.get("closes_at") and now > cfg["closes_at"]:
        return "closed"
    return "open"


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---- admin rich-text sanitizer -------------------------------------------
# Question descriptions (and other admin-authored prose) may use light
# formatting, so they are rendered as HTML rather than escaped. A national
# admin token is the only way to save them — but a stolen/misused token must
# not be able to plant <script>, event handlers, or javascript: URLs on a
# ballot page. Everything not on this allowlist is escaped to text.
import html.parser as _htmlparser

_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "br", "p", "ul", "ol", "li",
                 "a", "small", "sub", "sup", "span", "code"}
_VOID_TAGS = {"br"}
_ALLOWED_ATTRS = {"a": {"href", "title"}, "span": set(), "code": set()}
_SAFE_URL = re.compile(r"^(https?:|mailto:|/|#)", re.I)


class _Sanitizer(_htmlparser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, set())
        kept = []
        for k, v in attrs:
            k = (k or "").lower()
            if k not in allowed:
                continue
            if k == "href" and not _SAFE_URL.match((v or "").strip()):
                continue
            kept.append(f' {k}="{_esc(v)}"')
        rel = ' rel="noopener nofollow"' if tag == "a" else ""
        if tag in _VOID_TAGS:
            self.out.append(f"<{tag}{''.join(kept)}/>")
        else:
            self.stack.append(tag)
            self.out.append(f"<{tag}{''.join(kept)}{rel}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        if tag in self.stack:
            # close any tags opened after this one, then this one
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        self.out.append(_esc(data))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(s) -> str:
    p = _Sanitizer()
    p.feed(str(s or ""))
    return p.result()


# Column labels for the common score scales; the DSA at-large delegate rules
# use 0/1/2 = disapprove/neutral/approve. Other maxima render numbers only.
SCORE_LABELS = {
    2: {0: "Disapprove", 1: "Neutral", 2: "Approve"},
    5: {0: "Worst", 5: "Best"},
}


def _yesno_options(q):
    subs = q.get("option_subs") or {}
    opts = [{"id": "YES", "name": "Yes", "sub": subs.get("YES", "")},
            {"id": "NO", "name": "No", "sub": subs.get("NO", "")}]
    if q.get("allow_abstain", True):
        opts.append({"id": "ABSTAIN", "name": "Abstain", "sub": subs.get("ABSTAIN", "")})
    return opts


_BUBBLE_SVG = ('<svg class="vk-bubble" viewBox="0 0 44 26" aria-hidden="true">'
               '<ellipse class="ring" cx="22" cy="13" rx="18" ry="9"/>'
               '<ellipse class="ink" cx="22" cy="13" rx="16.5" ry="7.8"/></svg>')


def _opt_button(q, opt, marker_html, role="") -> str:
    sub = f'<small>{_esc(opt["sub"])}</small>' if opt.get("sub") else (
        "" if q["type"] == "yesno" else "<small></small>")
    # explicit accessible name — the marker (bubble/checkbox SVG) is aria-hidden,
    # so a role=radio/checkbox button needs its name spelled out for screen readers
    aria = f' aria-label="{_esc(opt["name"])}"' if role else ""
    role_attr = f' role="{role}" aria-checked="false"{aria}' if role else ""
    size = "" if q["type"] == "yesno" else ' style="font-size:1.35rem;"'
    return (f'<button type="button" class="vk-opt"{role_attr} data-choice="{_esc(opt["id"])}"{size}>'
            f'{marker_html}<span>{_esc(opt["name"])}{sub}</span></button>')


def _question_html(q, n, total) -> str:
    """One question: optional section banner, measure block, options group."""
    out = []
    sect = q.get("section")
    if sect:
        style = int(sect.get("style") or 1)
        out.append(f'<div class="vk-sect vk-sect-{style}">'
                   f'<p class="vk-sect-k">{_esc(sect.get("kicker") or "")}</p>'
                   f'<h3 class="vk-sect-t">{_esc(sect.get("title") or "")}</h3>'
                   # sanitize at RENDER as well as at save: these two fields are
                   # deliberately rendered as HTML (admins format their prose),
                   # and _validate_questions cleans them on the way in — but a
                   # config that never passed through the builder (seeded
                   # directly, written straight to Firestore, or predating
                   # validation) would otherwise render whatever it holds.
                   + (f'<p class="vk-sect-s">{sanitize_html(sect.get("sub"))}</p>'
                      if sect.get("sub") else "")
                   + "</div>")
    label = f' &middot; {_esc(q["label"])}' if q.get("label") else ""
    out.append('<div class="vk-measure"' + (' style="margin-top:20px;"' if n > 1 else "") + ">"
               f'<p class="vk-measure-no">Question {n} of {total}{label}</p>'
               f'<p class="vk-measure-q">{_esc(q["title"])}</p>'
               + "".join(f'<p class="vk-measure-t">{sanitize_html(para)}</p>'
                         for para in (q.get("text") or []))
               + "</div>")

    typ, key = q["type"], q["key"]
    if typ == "yesno":
        opts = "".join(_opt_button(q, o, _BUBBLE_SVG, role="radio") for o in _yesno_options(q))
        out.append(f'<div class="vk-opts" role="radiogroup" aria-label="Question {n} — '
                   f'{_esc(q["title"])}" data-q="{key}" data-type="single">{opts}</div>')
    elif typ == "ranked":
        options = list(q["options"])
        if q.get("shuffle"):
            random.shuffle(options)   # no alphabet advantage; fresh per page load
        if q.get("allow_abstain", True):
            options = options + [{"id": "ABSTAIN", "name": "Abstain", "sub": "Skip this question"}]
        marker = '<span class="vk-rankn" aria-hidden="true"></span>'
        opts = "".join(_opt_button(q, o, marker) for o in options)
        out.append(f'<div class="vk-opts" aria-label="Question {n} — {_esc(q["title"])}, ranked" '
                   f'data-q="{key}" data-type="rank">{opts}</div>')
        out.append(f'<p class="vk-rank-hint"><span id="hint-{key}" aria-live="polite">'
                   'No preferences ranked yet.</span>'
                   f'<button type="button" class="vk-rank-clear" data-clear="{key}">'
                   'Clear rankings</button></p>')
    elif typ == "multi":
        options = list(q["options"])
        if q.get("allow_abstain", True):
            options = options + [{"id": "ABSTAIN", "name": "Abstain",
                                  "sub": q.get("abstain_sub", "")}]
        marker = '<span class="vk-checkbox" aria-hidden="true">✓</span>'
        opts = "".join(_opt_button(q, o, marker, role="checkbox") for o in options)
        out.append(f'<div class="vk-opts" aria-label="Question {n} — {_esc(q["title"])}" '
                   f'data-q="{key}" data-type="multi">{opts}</div>')
    elif typ == "score":
        options = list(q["options"])
        if q.get("shuffle"):
            random.shuffle(options)
        max_s = int(q.get("max_score", 2))
        labels = SCORE_LABELS.get(max_s, {})
        head = "".join(f'<span class="vk-score-h">{s}'
                       + (f'<small>{_esc(labels[s])}</small>' if labels.get(s) else "")
                       + "</span>" for s in range(max_s + 1))
        rows = []
        for o in options:
            cells = "".join(
                f'<button type="button" class="vk-score-b" data-choice="{_esc(o["id"])}" '
                f'data-score="{s}" aria-label="{_esc(o["name"])}: {s}'
                + (f' ({_esc(labels[s])})' if labels.get(s) else "")
                + f'" role="radio" aria-checked="false">{s}</button>'
                for s in range(max_s + 1))
            sub = f'<small>{_esc(o["sub"])}</small>' if o.get("sub") else ""
            rows.append(f'<div class="vk-score-row" role="radiogroup" '
                        f'aria-label="{_esc(o["name"])}">'
                        f'<span class="vk-score-name">{_esc(o["name"])}{sub}</span>'
                        f'<span class="vk-score-cells">{cells}</span></div>')
        out.append(f'<div class="vk-score" data-q="{key}" data-type="score" '
                   f'data-max="{max_s}"><div class="vk-score-legend">'
                   f'<span class="vk-score-name"></span>'
                   f'<span class="vk-score-cells">{head}</span></div>'
                   + "".join(rows) + "</div>")
        out.append(f'<p class="vk-rank-hint"><span id="hint-{key}" aria-live="polite">'
                   'Rate every candidate.</span>'
                   + (f'<button type="button" class="vk-rank-clear" data-clear="{key}">'
                      'Clear ratings</button>' if q.get("allow_abstain", True) else "")
                   + "</p>")
    elif typ == "text":
        mx = q.get("max", TEXT_MAX_DEFAULT)
        out.append(f'<label class="vk-label" for="txt-{key}">Your answer (optional, '
                   f'{mx:,} characters max)</label>'
                   f'<textarea class="vk-input" id="txt-{key}" maxlength="{mx}" '
                   'placeholder="In your own words…"></textarea>')
    return "".join(out)


def _disclosure_html(cfg) -> str:
    """Visibility disclosure shown above question 1 (decided policy)."""
    if secret_keys(cfg):
        return ('<div class="vk-warn"><b class="vk-warn-t">How your votes are seen</b>'
                'Named questions on this ballot are recorded by name — election '
                'administrators and your chapter can see how each member voted. '
                '<b>Questions marked as a secret ballot (such as convention delegates) '
                'are different</b>: your ranking is stored without your name or chapter; '
                'only election administrators can access those ballots, solely for '
                'troubleshooting. National does not publish your chapter’s results '
                '— your chapter decides whether to publish its results, including '
                'how members voted (never secret-ballot rankings).</div>')
    return ('<div class="vk-warn"><b class="vk-warn-t">How your votes are seen</b>'
            'Votes on this ballot are recorded by name — election administrators '
            'and your chapter can see how each member voted. National does not publish '
            'your chapter’s results — your chapter decides whether to publish '
            'its own.</div>')


def render_ballot(poll_id: str, cfg: dict, code: str = "") -> str:
    questions = poll_questions(cfg)
    total = len(questions)
    parts = [_disclosure_html(cfg)]
    qdef = []
    for i, q in enumerate(questions):
        parts.append(_question_html(q, i + 1, total))
        names = {}
        if q["type"] == "yesno":
            names = {o["id"]: o["name"] for o in _yesno_options(q)}
        elif q["type"] in ("ranked", "multi", "score"):
            names = {o["id"]: o["name"] for o in q["options"]}
            names.setdefault("ABSTAIN", "Abstain")
        qdef.append({"key": q["key"], "type": q["type"], "n": i + 1,
                     "title": q["title"], "names": names,
                     "seats": q.get("seats", 0) if q.get("alternates") else 0,
                     "max_score": int(q.get("max_score", 2)) if q["type"] == "score" else 0,
                     "n_opts": len(q.get("options") or []) if q["type"] == "score" else 0,
                     "allow_abstain": bool(q.get("allow_abstain", True)),
                     "require_full": bool(q.get("require_full", True)) if q["type"] == "score" else False,
                     "required": q.get("required", q["type"] in ("yesno", "ranked", "score"))})

    html = TEMPLATE.replace("__POLL_ID__", poll_id)
    # cfg["name"] is admin-set config, reflected into text, a <title>, AND an
    # attribute (value="__CHAPTER_NAME__") — escape so a name with a quote or
    # angle bracket can't break out of any of those contexts.
    html = html.replace("__CHAPTER_NAME__", _esc(cfg["name"]))
    from urllib.parse import quote
    html = html.replace("__HELP_SUBJECT__", quote(f"[BALLOT26] {cfg['name']} — Can't find my code"))
    html = html.replace("__QUESTIONS_HTML__", "".join(parts))
    # QDEF carries admin-set titles/candidate names into an inline <script>;
    # _js_json keeps a "</script>" in any of them from ending the element.
    html = html.replace("__QDEF__", _js_json(qdef))
    html = html.replace("__N_Q__", str(total))
    html = html.replace("__CODE__", code if CODE_RE.match(code or "") else "")
    return html


# ---- vote transaction (single-doc: no hot document) -----------------------
def _ballot_docs(poll_id: str, cfg: dict, receipt: str, answers: dict,
                 comments: dict, identity: dict, code_h, weight: int = 1):
    """Build the (doc_ref, payload) pairs for one cast ballot: the main
    (identity-linked) record and, when the ballot has secret questions, the
    separate secret-ballot record. Returns (pairs, record_hash).

    Building and writing are split so a coded vote can commit the code burn
    and BOTH ballot records in a single Firestore transaction — see
    `_cast_txn`. `weight` is only stamped for codeless (provisional) ballots;
    coded ballots resolve their CURRENT weight from the code doc at tally
    time."""
    skeys = set(secret_keys(cfg))
    main_answers = {k: v for k, v in answers.items() if k not in skeys}
    nonce = secrets.token_hex(8)
    ac = canon_answers(main_answers)
    rh = make_record_hash(receipt, ac, nonce)
    wfield = {"weight": weight} if (code_h is None and weight != 1) else {}
    pairs = [(db.collection(f"{poll_id}__ballots").document(rh), {
        "receipt": receipt, "answers": main_answers, "answers_canon": ac,
        "nonce": nonce, "record_hash": rh,
        "code_hash": code_h,
        "comment": comments.get("text", ""),   # legacy field for older tools
        "comments": comments,
        "day_bucket": firestore.SERVER_TIMESTAMP,
        **wfield,
        **identity,
    })]
    if skeys:
        dq = {k: (answers.get(k) or []) for k in sorted(skeys)}
        dnonce = secrets.token_hex(8)
        dcanon = canon_answers(dq)
        drh = make_record_hash(receipt, dcanon, dnonce)
        pairs.append((db.collection(f"{poll_id}__delegate_ballots").document(drh), {
            "receipt": receipt, **dq, "answers_canon": dcanon,
            "nonce": dnonce, "record_hash": drh,
            "code_hash": code_h,   # ADMIN-ONLY troubleshooting trace — never chapter-visible
            "day_bucket": firestore.SERVER_TIMESTAMP,
            **wfield,
            **({"provisional": True} if identity.get("provisional") else {}),
        }))
    return pairs, rh


def _write_ballot_docs(poll_id: str, cfg: dict, receipt: str, answers: dict,
                       comments: dict, identity: dict, code_h, weight: int = 1,
                       txn=None):
    """Persist a cast ballot. Shared by coded votes and provisional promotion
    so the storage split can't drift. Pass `txn` to enlist the writes in a
    Firestore transaction."""
    pairs, rh = _ballot_docs(poll_id, cfg, receipt, answers, comments,
                             identity, code_h, weight)
    for ref, payload in pairs:
        if txn is not None:
            txn.set(ref, payload)
        else:
            ref.set(payload)
    return rh


def _claim_code(txn, codes_coll, ch: str):
    """Claim the code. Returns the code doc's voter fields (member_id,
    chapter) on success, None if already used. Runs INSIDE `_cast_txn`'s
    transaction — the read has to happen before any write is issued."""
    ref = codes_coll.document(ch)
    snap = ref.get(transaction=txn)
    if not snap.exists:
        raise LookupError("unknown code")
    d = snap.to_dict() or {}
    if d.get("used"):
        return None
    txn.update(ref, {"used": True})
    return {"member_id": d.get("member_id"), "chapter": d.get("chapter")}


@firestore.transactional
def _cast_txn(txn, poll_id: str, cfg: dict, codes_coll, ch: str, receipt: str,
              answers: dict, comments: dict):
    """Claim the code AND write the ballot record(s) in ONE transaction.

    These must not be separate steps: burning the code first and writing the
    ballot afterwards means any failure between them (Firestore blip, request
    deadline, instance eviction) leaves a used code with no ballot — the voter
    is silently disenfranchised and cannot retry, because their code now reads
    as already-voted. Committing together makes that state unreachable.

    Returns None if the code was already used, else {"receipt": ...}."""
    claim = _claim_code(txn, codes_coll, ch)
    if claim is None:
        return None
    _write_ballot_docs(poll_id, cfg, receipt, answers, comments,
                       {"member_id": claim.get("member_id"),
                        "chapter": claim.get("chapter")}, ch, txn=txn)
    return {"receipt": receipt}


def cast_vote(poll_id: str, cfg: dict, code_plaintext: str, answers: dict,
              comments: dict) -> dict:
    """HYBRID VISIBILITY MODEL:
    * Named questions ({poll}__ballots): identity-linked (member_id, chapter,
      code_hash) — visible to election administrators and the voter's
      chapter. Results not published publicly.
    * SECRET questions ({poll}__delegate_ballots — delegate elections per
      Const. Art. V §5, or any question flagged secret): stored separately
      with NO member_id and NO chapter identity. The collection is
      ADMIN-ONLY; it retains the code_hash solely so an election
      administrator can trace a specific ballot for troubleshooting (via the
      codes collection). Chapters never get access to this collection or the
      code mapping. Admin access must be IAM-restricted + audit-logged.
    Voters are told all of this on the ballot before voting."""
    codes = db.collection(f"{poll_id}__codes")
    ch = code_hash(code_plaintext)
    receipt = new_receipt()
    txn = db.transaction()
    claim = _cast_txn(txn, poll_id, cfg, codes, ch, receipt, answers, comments)
    if claim is None:
        return {"status": "already_voted"}
    return {"status": "recorded", "receipt": claim["receipt"]}


def cast_provisional(poll_id: str, answers: dict, comments: dict, info: dict) -> dict:
    """Sealed provisional ballot: stored separately, NOT counted, no code used.
    Adjudicated by staff against membership after the fact. (Identity-linked
    by design until adjudication, so the comment lives with it.)"""
    prov = db.collection(f"{poll_id}__provisional")
    # The receipt IS this document's id, so a collision would silently
    # OVERWRITE another member's sealed provisional ballot. 56 bits makes
    # that vanishingly unlikely; the existence check makes it impossible.
    receipt = new_receipt("P")
    for _ in range(5):
        if not getattr(prov.document(receipt).get(), "exists", False):
            break
        receipt = new_receipt("P")
    else:
        return {"status": "error", "error": "receipt_allocation_failed"}
    prov.document(receipt).set({
        "receipt": receipt,
        "answers": answers, "comments": comments,
        "comment": comments.get("text", ""),      # sealed; not in tally until verified
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


RESEND_COOLDOWN_S = 15 * 60   # one self-serve resend per contact per 15 min

# Notification preferences: a GLOBAL, contact-keyed suppression list (spans
# elections). A member can stop RosaVote from contacting a given email/phone
# and re-enable it. SMS carrier STOP/START is handled by the SMS provider; this
# covers email + a self-serve page, and the delivery paths honor it.
PREFS_COLL = "notify_prefs"


def _contact_hash(contact: str) -> str:
    return hashlib.sha256(_norm_contact(contact).encode()).hexdigest()


def _is_suppressed(contact: str) -> bool:
    try:
        d = db.collection(PREFS_COLL).document(_contact_hash(contact)).get()
        return bool(getattr(d, "exists", False) and (d.to_dict() or {}).get("suppressed"))
    except Exception:
        return False


def _set_suppressed(contact: str, suppressed: bool):
    db.collection(PREFS_COLL).document(_contact_hash(contact)).set(
        {"suppressed": suppressed, "at": firestore.SERVER_TIMESTAMP})


# ---- signed opt-out tokens -----------------------------------------------
# Suppressing delivery to a contact is a DELIVERY DENIAL: a member whose
# email/phone is suppressed stops receiving their ballot. So opt-out must be
# authenticated by POSSESSION OF THE MEMBER'S OWN LINK, not by typing an
# address anyone can guess. Each ballot message carries a per-contact signed
# link; only that link can suppress (or restore) that contact. Enumeration-safe
# and action-bound (an opt-out token can't be replayed as opt-in).
#
# Signing key: PREFS_SIGNING_KEY if set, else derived from ADMIN_TOKEN so a
# normal deployment needs no extra config. Rotating either invalidates
# outstanding links (they simply stop verifying — fail closed, never open).
PREFS_ACTIONS = ("optout", "optin")
_PREFS_FALLBACK_SECRET = secrets.token_bytes(32)   # dev only: no stable secret


def _prefs_secret() -> bytes:
    k = os.environ.get("PREFS_SIGNING_KEY", "").strip()
    if k:
        return k.encode()
    if ADMIN_TOKEN:
        return hashlib.sha256(b"rosavote-prefs-v1|" + ADMIN_TOKEN.encode()).digest()
    return _PREFS_FALLBACK_SECRET


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _prefs_token(contact: str, action: str) -> str:
    payload = f"{action}|{_norm_contact(contact)}".encode()
    sig = hmac.new(_prefs_secret(), payload, hashlib.sha256).hexdigest()[:32]
    return f"{_b64u(payload)}.{sig}"


def _prefs_verify(token: str):
    """Return (action, contact) for a valid token, else None."""
    try:
        b64, sig = str(token or "").split(".", 1)
        payload = _b64u_dec(b64)
        action, contact = payload.decode().split("|", 1)
    except Exception:
        return None
    if action not in PREFS_ACTIONS:
        return None
    good = hmac.new(_prefs_secret(), payload, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    return action, contact


def prefs_optout_url(base_url: str, contact: str) -> str:
    """The per-member unsubscribe link to drop into a ballot email/SMS footer.
    Only this link (not a typed address) can suppress this contact."""
    return f"{base_url.rstrip('/')}/prefs?t={_prefs_token(contact, 'optout')}"


def _mask_contact(c: str) -> str:
    """A recognizable-but-not-full rendering, so the confirmation page shows
    which contact without splashing the whole address across logs/history."""
    c = _norm_contact(c)

    def _lead(s):   # first alphanumeric char only — never markup
        return (s[:1] if s[:1].isalnum() else "*")

    if "@" in c:
        user, _, dom = c.partition("@")
        host, _, tld = dom.rpartition(".")
        tld = "".join(ch for ch in tld if ch.isalnum())[:8]
        return _lead(user) + "***@" + _lead(host) + "***" + (f".{tld}" if tld else "")
    digits = "".join(ch for ch in c if ch.isdigit())
    return "***-***-" + digits[-4:] if len(digits) >= 4 else "***"


def _prefs_apply(want_action: str, suppressed: bool):
    v = _prefs_verify((request.get_json(silent=True) or {}).get("token"))
    if not v or v[0] != want_action:
        # no contact was ever revealed to an unsigned caller, so this leaks
        # nothing; it simply refuses to act without the member's own link.
        return jsonify({"status": "error",
                        "message": "This link is invalid or has expired. Use the "
                                   "unsubscribe link from your most recent RosaVote "
                                   "message, reply STOP to a text, or contact your "
                                   "chapter."}), 400
    contact = v[1]
    if len(contact) >= 3:
        try:
            _set_suppressed(contact, suppressed)
        except Exception:
            pass
    return jsonify({"status": "ok"}), 200


@app.post("/prefs/optout")
def prefs_optout():
    """Stop contacting an email/phone. REQUIRES the member's signed opt-out
    token (from the unsubscribe link in their own email/text) — a typed
    address is refused, because suppressing delivery is a way to keep a member
    from getting their ballot."""
    return _prefs_apply("optout", True)


@app.post("/prefs/optin")
def prefs_optin():
    """Resume contact — also token-gated (the confirmation page hands back a
    matching opt-in link after an opt-out)."""
    return _prefs_apply("optin", False)


_PREFS_PAGE = """<!doctype html>
<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Notification preferences — RosaVote</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml"/>
<style>body{font:16px/1.55 Georgia,serif;background:#fff5e5;color:#111;margin:0}
main{max-width:620px;margin:0 auto;padding:22px 18px 60px}
.banner{background:#dd1111;color:#fff5e5;padding:12px 18px;font-family:"Arial Narrow",sans-serif;font-weight:bold;text-transform:uppercase}
h1{font-family:"Arial Narrow",sans-serif;text-transform:uppercase}
.card{background:#fff;border:1px solid #000;box-shadow:5px 5px 0 0 #000;padding:14px 16px;margin:14px 0}
input,button{font:inherit;padding:9px 11px;border:2px solid #000}
button{font-family:"Arial Narrow",sans-serif;font-weight:bold;text-transform:uppercase;cursor:pointer}
.out{background:#dd1111;color:#fff5e5}.in{background:#fff}
.warn{background:#ffe1b2;border:1px solid #000;padding:8px 12px;font-size:.9rem}
.r{font-weight:bold;margin:8px 0 0}a{color:#dd1111}</style></head><body>
<div class="banner"><img src="/logo.svg" alt="" style="height:26px;vertical-align:-7px;margin-right:6px"/>RosaVote</div>
<main><h1>Notification preferences</h1>
<div class="card" id="ctx-card">
<p id="lead"></p>
<div style="display:flex;gap:8px;flex-wrap:wrap"><button id="act" type="button"></button></div>
<p class="r" id="msg" aria-live="polite"></p>
</div>
<div class="warn"><b>Important:</b> your ballot is delivered by these same channels.
If you opt an address out, make sure you can still receive your ballot another way
(email, text, or postcard) — otherwise you may not be able to vote. Opting out of a
text is also as simple as replying <b>STOP</b>; reply <b>START</b> to resume.</div>
<p><a href="/">&larr; RosaVote</a></p></main>
<script>
(function(){
 var CTX=__PREFS_CTX__;   // {action,endpoint,reverse,reverse_endpoint,token,reverse_token,masked} or null
 var lead=document.getElementById("lead"),act=document.getElementById("act"),msg=document.getElementById("msg");
 function labelFor(a){return a==="optout"?"Stop these messages":"Resume these messages";}
 function render(a,tok,ep,masked){
  lead.innerHTML=(a==="optout"
    ?"Confirm you want RosaVote to <b>stop</b> sending election messages to <b>"
    :"Confirm you want RosaVote to <b>resume</b> election messages to <b>")+masked+"</b>.";
  act.textContent=labelFor(a);act.className=a==="optout"?"out":"in";
  act.onclick=function(){msg.textContent="Saving\\u2026";
   fetch(ep,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:tok})})
    .then(function(r){return r.json();}).then(function(d){
     if(d.status==="ok"){msg.textContent=a==="optout"
       ?"Done \\u2014 we won't send election messages to that contact.":"Done \\u2014 messages resumed.";
      if(CTX&&CTX.reverse){render(CTX.reverse,CTX.reverse_token,CTX.reverse_endpoint,CTX.masked);CTX=null;}
      else{act.style.display="none";}}
     else msg.textContent=d.message||"That link is invalid or expired.";
    }).catch(function(){msg.textContent="Network error \\u2014 try again.";});};
 }
 if(CTX){render(CTX.action,CTX.token,CTX.endpoint,CTX.masked);}
 else{lead.innerHTML="To change whether RosaVote contacts you for elections, use the "
   +"<b>unsubscribe link</b> at the bottom of your most recent RosaVote email or text. "
   +"For a text you can also reply <b>STOP</b> (or <b>START</b> to resume). Still stuck? "
   +"Contact your chapter\\u2019s elections committee.";
  act.style.display="none";}
})();
</script></body></html>"""


@app.get("/prefs")
def prefs_page():
    tok = request.args.get("t", "")
    v = _prefs_verify(tok) if tok else None
    if not v:
        ctx = None
    else:
        action, contact = v
        reverse = "optin" if action == "optout" else "optout"
        ctx = {"action": action, "endpoint": f"/prefs/{action}",
               "reverse": reverse, "reverse_endpoint": f"/prefs/{reverse}",
               "token": tok, "reverse_token": _prefs_token(contact, reverse),
               "masked": _mask_contact(contact)}
    return Response(_PREFS_PAGE.replace("__PREFS_CTX__", _js_json(ctx)),
                    mimetype="text/html")


def _norm_contact(c: str) -> str:
    c = (c or "").strip().lower()
    if "@" in c:
        return c
    return "".join(ch for ch in c if ch.isdigit() or ch == "+")


def _deliver_resend(link: str, dests: list):
    """Re-send an existing ballot link to a member's OWN on-record contacts,
    via the same channels as the mass send. Failures are swallowed — resend is
    best-effort and must never leak which contacts exist."""
    import senders
    mail = senders.MailgunSender()
    sms = senders.sms_sender()   # Twilio for one-off SMS if configured
    for dest in dests:
        try:
            if _is_suppressed(dest):
                continue                 # member opted this contact out
            if "@" in dest and mail.configured():
                mail.send_batch([{"email": dest, "link": link}])
            elif "@" not in dest and sms.configured():
                sms.send_one(dest, link)
        except Exception:
            pass


@app.post("/p/<poll_id>/resend")
def resend_code(poll_id):
    """Self-serve 'I lost my code'. ENUMERATION-SAFE: always returns the same
    generic {status:"ok"} whether or not the contact matches a member, so it
    can't be used to test who's registered. RATE-LIMITED to one send per
    contact per 15 min. Re-sends the member's EXISTING link only to THEIR
    on-record contacts (never to the address typed in the form), so it can't
    redirect a ballot. Requires the chapter to have provisioned a
    `{poll}__resend` index (contact_hash -> {link, dests}); if it hasn't, the
    endpoint safely no-ops and members fall back to contacting their chapter."""
    generic = (jsonify({"status": "ok"}), 200)
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return generic
    contact = _norm_contact((request.get_json(silent=True) or {}).get("contact"))
    if len(contact) < 3:
        return generic
    ch = hashlib.sha256(f"{poll_id}|{contact}".encode()).hexdigest()
    now = int(time.time())
    tref = db.collection(f"{poll_id}__resend_log").document(ch)
    try:
        tsnap = tref.get()
        if getattr(tsnap, "exists", False):
            if now - int((tsnap.to_dict() or {}).get("at", 0)) < RESEND_COOLDOWN_S:
                return generic     # throttled — still say "ok"
        rec = db.collection(f"{poll_id}__resend").document(ch).get()
    except Exception:
        return generic
    if not getattr(rec, "exists", False):
        return generic            # not provisioned / no match
    d = rec.to_dict() or {}
    link, dests = d.get("link"), (d.get("dests") or [])
    if link and dests:
        _deliver_resend(link, dests)
        try:
            tref.set({"at": now})
        except Exception:
            pass
    return generic


RECEIPT_RE = re.compile(r"^[A-Z0-9-]{4,16}$")
# Receipts are 64-bit (16 hex chars). The old 32-bit receipt was a real
# collision risk, not a theoretical one: across a 120k-voter election the
# birthday bound puts the expected number of duplicate receipts near 2. That
# matters because receipt is the lookup key for `verify`, `void`, and the
# published ballots.csv — two voters sharing one would see (and could void)
# each other's ballot. Short legacy receipts still validate and still resolve.
RECEIPT_BYTES = 8


def new_receipt(prefix: str = "") -> str:
    """A fresh voter receipt. Stays inside RECEIPT_RE's 16-char budget."""
    n = RECEIPT_BYTES - (1 if prefix else 0)
    return prefix + secrets.token_hex(n).upper()


_PUB_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>{title} — Results — RosaVote</title><link rel="icon" href="/logo.svg" type="image/svg+xml"/>
<style>*{{box-sizing:border-box}}body{{font:16px/1.5 Georgia,serif;background:#fff5e5;color:#111;margin:0}}
.banner{{background:#dd1111;color:#fff5e5;padding:14px 18px;font-family:"Arial Narrow",sans-serif;
font-weight:bold;text-transform:uppercase;font-size:1.4rem}}.banner small{{display:block;font-size:.85rem;opacity:.9}}
main{{max-width:640px;margin:0 auto;padding:20px 16px 60px}}
.card{{background:#fff;border:1px solid #000;box-shadow:5px 5px 0 0 #000;margin:0 0 18px;padding:14px}}
h2{{font-family:"Arial Narrow",sans-serif;text-transform:uppercase;margin:0 0 8px;font-size:1.15rem}}
.row{{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:.9rem;font-family:system-ui,sans-serif}}
.nm{{flex:0 0 170px;text-align:right;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tr{{flex:1;position:relative;height:16px;background:#fff5e5;border:1px solid rgba(0,0,0,.25)}}
.fl{{position:absolute;left:0;top:0;bottom:0;background:#dd1111;border-radius:0 4px 4px 0}}
.fl.b{{background:#2a78d6}}.fl.g{{background:#767676}}
.vl{{flex:0 0 60px;font-variant-numeric:tabular-nums}}
.win{{font-family:system-ui,sans-serif;font-size:.95rem}}.k{{font-family:system-ui,sans-serif;
font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;color:#dd1111;font-weight:700}}
footer{{text-align:center;font:12px system-ui,sans-serif;color:rgba(0,0,0,.55);padding:16px}}
table.rc{{width:100%;border-collapse:collapse;font:.85rem system-ui,sans-serif;margin-top:8px;
display:block;overflow-x:auto}}
table.rc th,table.rc td{{border:1px solid rgba(0,0,0,.25);padding:4px 6px;text-align:left;
vertical-align:top}}
table.rc th{{background:#fff5e5;text-transform:uppercase;font-size:.7rem;letter-spacing:.06em}}
a{{color:#dd1111}}</style></head><body>
<div class="banner"><img src="/logo.svg" alt="" style="height:30px;vertical-align:-8px;margin-right:6px"/>RosaVote<small>{title} — Official Results</small></div>
<main>{body}</main>
<footer><b>RosaVote</b> · results certified by the body conducting the election ·
<a href="/terms">Terms</a> · <a href="/privacy">Privacy</a><br/>Built with 🌹 by Walker Green</footer>
</body></html>"""


def _pub_bar(name, v, vmax, cls=""):
    pct = round(v / vmax * 100, 1) if vmax else 0
    return (f'<div class="row"><span class="nm">{_esc(name)}</span>'
            f'<span class="tr"><span class="fl {cls}" style="width:{pct}%"></span></span>'
            f'<span class="vl">{v:g}</span></div>')


@app.get("/p/<poll_id>/results")
def public_results(poll_id):
    """PUBLIC, shareable results page — live only after the poll is
    finalized AND its admins chose to publish (each chapter decides its own
    publication). Shows outcomes and aggregate counts, never any voter's
    ballot and never per-ballot secret content."""
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return "Unknown poll.", 404
    if not (cfg.get("finalized") and cfg.get("results_published")):
        return Response(_PUB_SHELL.format(
            title=_esc(cfg.get("name") or poll_id),
            body='<div class="card"><h2>Results not published</h2>'
                 '<p class="win">This election\'s results have not been published '
                 'by its administrators (or voting is still under way). Check '
                 'back later, or contact your chapter.</p></div>'),
            mimetype="text/html")
    res = _read_published_results(poll_id) or _store_published_results(poll_id, cfg)
    cards = []
    for q in res["questions"]:
        inner = ""
        if q["type"] == "yesno":
            c = q["counts"]
            vmax = max(c["YES"], c["NO"], c["ABSTAIN"], 1)
            inner = (f'<p class="win"><b>{_esc(q["result"])}</b> '
                     '<small>(abstentions excluded)</small></p>'
                     + _pub_bar("Yes", c["YES"], vmax) + _pub_bar("No", c["NO"], vmax, "b")
                     + _pub_bar("Abstain", c["ABSTAIN"], vmax, "g"))
        elif q["type"] == "ranked":
            inner = ('<p class="win"><b>Elected'
                     + (f' ({q["seats"]} seats)' if q["seats"] > 1 else "") + ":</b> "
                     + _esc(", ".join(q["winners"]) or "—") + "</p>")
            if q.get("alternates"):
                inner += ('<p class="win"><b>Alternates:</b> '
                          + _esc(", ".join(q["alternates"])) + "</p>")
            if not q.get("group_partial"):
                for cn in (q.get("constraints") or []):
                    bound = f"max {cn['max']}" if "max" in cn else f"min {cn['min']}"
                    inner += ('<p class="win k">Quota requirement met: '
                              + _esc(cn.get("label") or f"{bound} {cn['tag']}")
                              + f' — {cn["elected"]} elected'
                              + (' (across the full body)' if q.get("quota_group") else '')
                              + '</p>')
            fp = q.get("first_prefs") or {}
            vmax = max(list(fp.values()) + [1])
            inner += '<p class="k">First preferences</p>' + "".join(
                _pub_bar(n, v, vmax, "" if n in q["winners"] else "b")
                for n, v in sorted(fp.items(), key=lambda kv: -kv[1]))
            inner += (f'<p class="win"><small>{_esc(q.get("method_used") or "Scottish STV")} · {q["valid_ballots"]} valid '
                      f'ballots · quota {q["quota"]} · full round-by-round record held '
                      'by election administration</small></p>')
        elif q["type"] == "score":
            inner = ('<p class="win"><b>Elected'
                     + (f' ({q["seats"]} seats)' if q["seats"] > 1 else "") + ":</b> "
                     + _esc(", ".join(q["winners"]) or "—") + "</p>")
            if not q.get("group_partial"):
                for cn in (q.get("constraints") or []):
                    bound = f"max {cn['max']}" if "max" in cn else f"min {cn['min']}"
                    inner += ('<p class="win k">Quota requirement met: '
                              + _esc(cn.get("label") or f"{bound} {cn['tag']}")
                              + f' — {cn["elected"]} elected'
                              + (' (across the full body)' if q.get("quota_group") else '')
                              + '</p>')
            sc = q.get("scores") or {}
            vmax = max(list(sc.values()) + [1])
            inner += '<p class="k">Total score</p>' + "".join(
                _pub_bar(n, v, vmax, "" if n in q["winners"] else "b")
                for n, v in sorted(sc.items(), key=lambda kv: -kv[1]))
            inner += (f'<p class="win"><small>{_esc(q.get("method_used") or "Score voting")} · '
                      f'{q["valid_ballots"]} valid ballots · scores 0–{q.get("max_score", 2)} · '
                      'full record held by election administration</small></p>')
        elif q["type"] == "multi":
            vmax = max(list(q["counts"].values()) + [1])
            inner = "".join(_pub_bar(n, v, vmax) for n, v in q["counts"].items())
        elif q["type"] == "text":
            inner = (f'<p class="win">{q["responses"]} written response(s) received '
                     '(content reviewed by election administration).</p>')
        if q.get("visibility") == "public":
            # Recorded roll call: this body publishes how each member voted.
            qdef = next((x for x in poll_questions(cfg) if x["key"] == q["key"]), None)
            rows = roll_call_rows(poll_id, qdef) if qdef else []
            link = (f'<p class="win"><a href="/p/{poll_id}/rollcall/{q["key"]}.csv">'
                    f'Download the full roll call ({len(rows)} ballots, CSV)</a></p>')
            inner += ('<p class="k">Recorded vote — this question is published by name</p>'
                      + link)
            if 0 < len(rows) <= ROLLCALL_INLINE_MAX:
                inner += ('<table class="rc"><tr><th>Member</th><th>Chapter</th>'
                          '<th>Vote</th></tr>' + "".join(
                    f'<tr><td>{_esc(r["member_id"])}</td><td>{_esc(r["chapter"])}</td>'
                    f'<td>{_esc(r["answer"])}'
                    + (' <b>(VOIDED — not counted)</b>' if r["voided"] else '')
                    + '</td></tr>' for r in rows) + '</table>')
        cards.append(f'<div class="card"><h2>{_esc(q["title"])}</h2>{inner}</div>')
    meta = (f'<div class="card"><p class="win">{res["ballots_counted"]} ballots counted'
            + (" · weighted election (ballots count at each voter's assigned weight)"
               if res.get("weighted") else "")
            + '. Voters can confirm their own ballot and independently recompute this '
            f'result — see <a href="/p/{poll_id}/verify-vote">Verify your vote</a>.</p></div>')
    return Response(_PUB_SHELL.format(title=_esc(res.get("name") or poll_id),
                                      body=meta + "".join(cards)),
                    mimetype="text/html")


@app.get("/p/<poll_id>/verify")
def verify_receipt(poll_id):
    """PUBLIC receipt check — a voter whose browser/VPN made casting feel
    flaky can confirm their ballot was stored: /p/<poll>/verify?receipt=XXX.
    Reveals only that a receipt exists and its status, never any answer and
    never who cast it. (Receipts carry no identity, so enumeration exposes
    nothing personal.)"""
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    receipt = str(request.args.get("receipt", "")).strip().upper()
    if not RECEIPT_RE.match(receipt):
        return jsonify({"error": "invalid_receipt_format"}), 400
    for snap in db.collection(f"{poll_id}__ballots").where("receipt", "==", receipt).stream():
        d = snap.to_dict() or {}
        return jsonify({"found": True,
                        "status": "voided" if d.get("voided") else "recorded"}), 200
    prov = db.collection(f"{poll_id}__provisional").document(receipt).get()
    if getattr(prov, "exists", False):
        st = (prov.to_dict() or {}).get("status", "pending")
        return jsonify({"found": True, "status": f"provisional_{st}"}), 200
    return jsonify({"found": False}), 404


_VERIFY_PAGE = """<!doctype html>
<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Verify your vote — __POLL_NAME__ — RosaVote</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml"/>
<style>
 body{font:16px/1.55 Georgia,serif;background:#fff5e5;color:#111;margin:0}
 main{max-width:680px;margin:0 auto;padding:22px 18px 60px}
 .banner{background:#dd1111;color:#fff5e5;padding:12px 18px;font-family:"Arial Narrow",sans-serif;font-weight:bold;text-transform:uppercase}
 h1{font-family:"Arial Narrow",sans-serif;text-transform:uppercase;margin:.4em 0}
 h2{font-family:"Arial Narrow",sans-serif;text-transform:uppercase;font-size:1.15rem;margin:1.4em 0 .3em}
 .card{background:#fff;border:1px solid #000;box-shadow:5px 5px 0 0 #000;padding:14px 16px;margin:14px 0}
 code{background:#f3ead9;padding:1px 5px}
 ol{margin:6px 0 6px 20px}li{margin:0 0 6px}
 input,button{font:inherit;padding:9px 11px;border:2px solid #000}
 button{background:#dd1111;color:#fff5e5;font-family:"Arial Narrow",sans-serif;font-weight:bold;text-transform:uppercase;cursor:pointer}
 .r{margin:8px 0 0;font-weight:bold}.ok{color:#0a7d928}.mut{color:#555;font-size:.9rem}
 a{color:#dd1111}
</style></head><body>
<div class="banner"><img src="/logo.svg" alt="" style="height:26px;vertical-align:-7px;margin-right:6px"/>RosaVote</div>
<main>
<h1>Verify your vote</h1>
<p class="mut">__POLL_NAME__</p>
<p>You don't have to take anyone's word for it. There are two independent checks:
one you can do right now, and one anyone can do after the election closes.</p>

<div class="card">
<h2>1 · Confirm your ballot was recorded</h2>
<p>Enter the <b>receipt code</b> shown when you voted. This confirms your ballot is
stored — it never reveals how you voted or who you are.</p>
<div style="display:flex;gap:8px;flex-wrap:wrap">
  <input id="rc" placeholder="e.g. A1B2C3D4" style="flex:1;min-width:170px" autocapitalize="characters"/>
  <button id="rb" type="button">Check</button>
</div>
<p class="r" id="rr" aria-live="polite"></p>
<p class="mut">Lost your receipt? It was on your confirmation screen. If you can't find it,
your chapter's elections committee can look up your ballot status for you.</p>
</div>

<div class="card">
<h2>2 · Independently verify the whole count (after close)</h2>
<p>After voting closes, the election administrators publish three files that let
<b>anyone</b> — you, any candidate, any observer — recompute the entire result from
scratch, with no access to the live system:</p>
<ul>
 <li><code>ballots.csv</code> — every anonymous ballot: its receipt, the exact votes on it, and a tamper-evidence hash.</li>
 <li><code>used_codes.csv</code> — the voting codes that were used (hashed, no identities).</li>
 <li><code>chain_head.txt</code> — a single fingerprint of the whole ballot set, posted publicly at close.</li>
</ul>
<p>With those files you can:</p>
<ol>
 <li><b>Find your own receipt</b> in <code>ballots.csv</code> and confirm the votes next to it are exactly what you cast.</li>
 <li><b>Recompute the fingerprint</b> from <code>ballots.csv</code> and check it matches the published <code>chain_head.txt</code> — if even one ballot were changed, added, or removed, it wouldn't match.</li>
 <li><b>Recount it yourself</b> — run the open-source tally on <code>ballots.csv</code> (or upload the published <code>.blt</code> to OpaVote) and confirm the winners.</li>
 <li><b>Check turnout</b> — the number of ballots equals the number of used codes, and every used code was on a list published <i>before</i> voting opened (so no codes were manufactured).</li>
</ol>
<p>A ready-made checker (Python standard library only) does steps 2–4 automatically:</p>
<p><code>python verify.py ballots.csv used_codes.csv chain_head.txt</code></p>
<p><a href="__SOURCE_URL__">Get <code>verify.py</code> and the full source (AGPL-3.0)</a> ·
<a href="/accuracy">how the tabulation is tested</a> ·
<a href="/methods">the counting methods</a></p>
<hr style="border:none;border-top:1px solid #ddd;margin:14px 0"/>
<p><b>Prefer not to run anything?</b> Verify the tamper-evidence fingerprint right here —
your browser downloads the published ballots and recomputes the whole chain locally.</p>
<p><button id="vb" type="button">Verify the chain in my browser</button></p>
<p class="r" id="vbr" aria-live="polite"></p>
<p class="mut">Published files:
 <a href="/p/__POLL_ID__/verify/ballots.csv"><code>ballots.csv</code></a> ·
 <a href="/p/__POLL_ID__/verify/used_codes.csv"><code>used_codes.csv</code></a> ·
 <a href="/p/__POLL_ID__/verify/chain_head.txt"><code>chain_head.txt</code></a>
 <span id="vfnote"></span></p>
</div>

<div class="card">
<h2>What this does and doesn't prove</h2>
<p><b>It proves</b> your ballot is in the published record with your votes, unaltered;
that no ballot was tampered with; and that the announced winners follow from the
published ballots. That's a stronger guarantee than "trust us" — it's independently
reproducible.</p>
<p class="mut"><b>Honest limits:</b> because you can look up your receipt and see your
own votes, you could also show them to someone else — so keep your receipt private if
you'd prefer your vote stay between you and the record. And this confirms what was
<i>recorded</i>; verifying that your device submitted exactly what you intended is the
one step you perform yourself, at the moment you vote.</p>
</div>
<p><a href="/p/__POLL_ID__/">&larr; Back to the ballot</a></p>
</main>
<script>
(function(){var b=document.getElementById("rb"),i=document.getElementById("rc"),r=document.getElementById("rr");
 function esc(t){return String(t).replace(/[&<>]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}
 function check(){var v=(i.value||"").trim().toUpperCase();if(v.length<4){r.textContent="Enter your receipt code.";return;}
  r.textContent="Checking…";
  fetch("/p/__POLL_ID__/verify?receipt="+encodeURIComponent(v)).then(function(x){return x.json();}).then(function(d){
   if(d.found&&d.status==="recorded")r.innerHTML="\\u2713 Found — your ballot is recorded and counting.";
   else if(d.found&&d.status==="voided")r.innerHTML="This receipt was voided and reissued. If that's unexpected, contact your chapter.";
   else if(d.found&&d.status&&d.status.indexOf("provisional")===0)r.innerHTML="Found — provisional ballot, pending membership verification.";
   else r.innerHTML="No ballot found for that receipt. Check the code, or contact your chapter.";
  }).catch(function(){r.textContent="Network error — please try again.";});}
 b.addEventListener("click",check);i.addEventListener("keydown",function(e){if(e.key==="Enter")check();});
})();
(function(){var b=document.getElementById("vb"),out=document.getElementById("vbr");if(!b)return;
 var GEN="0000000000000000000000000000000000000000000000000000000000000000";
 function hex(buf){return Array.prototype.map.call(new Uint8Array(buf),function(x){return x.toString(16).padStart(2,"0");}).join("");}
 function sha(s){return crypto.subtle.digest("SHA-256",new TextEncoder().encode(s)).then(hex);}
 function csv(line){var o=[],c="",q=false;for(var i=0;i<line.length;i++){var ch=line[i];
  if(q){if(ch==='"'){if(line[i+1]==='"'){c+='"';i++;}else q=false;}else c+=ch;}
  else{if(ch==='"')q=true;else if(ch===','){o.push(c);c="";}else c+=ch;}}o.push(c);return o;}
 b.addEventListener("click",function(){out.textContent="Downloading the published ballots…";b.disabled=true;
  Promise.all([fetch("/p/__POLL_ID__/verify/ballots.csv"),fetch("/p/__POLL_ID__/verify/chain_head.txt")])
   .then(function(rs){if(rs[0].status===409){throw "notpub";}return Promise.all([rs[0].text(),rs[1].text()]);})
   .then(function(t){var rows=t[0].trim().split("\\n"),head=t[1].trim();rows.shift();
    var prev=GEN,i=0,total=rows.length;
    function step(){
     if(i>=total){out.innerHTML=(prev===head)
       ?("\\u2713 Verified all "+total+" ballots. The recomputed fingerprint matches the published one \\u2014 nothing was added, removed, or altered.")
       :("\\u2717 MISMATCH \\u2014 the recomputed fingerprint does not match. Do not trust this result; contact the election administrators.");
      b.disabled=false;return;}
     var f=csv(rows[i]);// receipt,answers,nonce,record_hash,prev_hash,chain_hash,...
     sha(f[0]+"|"+f[1]+"|"+f[2]).then(function(rh){
      if(rh!==f[3]||f[4]!==prev){out.innerHTML="\\u2717 Ballot "+(i+1)+" failed its hash check \\u2014 the record was altered.";b.disabled=false;return;}
      return sha(prev+"|"+rh).then(function(link){
       if(link!==f[5]){out.innerHTML="\\u2717 Chain broken at ballot "+(i+1)+".";b.disabled=false;return;}
       prev=link;i++;if(i%200===0)out.textContent="Verifying \\u2026 "+i+"/"+total;setTimeout(step,0);});});}
    step();})
   .catch(function(e){out.textContent=(e==="notpub")
     ?"The published files aren't available yet \\u2014 they appear once the election is finalized."
     :"Couldn't download or verify the files. Try again later.";b.disabled=false;});});})();
</script>
</body></html>"""


_GENESIS = "0" * 64


def _build_verification(poll_id: str):
    """Generate the public verification artifacts on demand from stored
    ballots — identical algorithm to tools/build_chain.py, so the published
    files and any independent recompute agree. Returns
    (ballots_csv, used_codes_csv, chain_head)."""
    ballots = []
    for snap in db.collection(f"{poll_id}__ballots").stream():
        d = snap.to_dict() or {}
        if not d.get("record_hash"):
            continue
        ballots.append({
            "receipt": d.get("receipt", ""),
            "answers": d.get("answers_canon", ""),
            "nonce": d.get("nonce", ""),
            "record_hash": d["record_hash"],
            "voided": bool(d.get("voided")),
            "void_reason": d.get("void_reason", "") or "",
        })
    ballots.sort(key=lambda b: b["record_hash"])   # deterministic order
    prev = _GENESIS
    for b in ballots:
        b["prev_hash"] = prev
        b["chain_hash"] = hashlib.sha256(
            f"{prev}|{b['record_hash']}".encode()).hexdigest()
        prev = b["chain_hash"]
    head = prev

    def _c(s):   # minimal CSV-quote for the answers JSON field
        s = str(s)
        return '"' + s.replace('"', '""') + '"' if any(x in s for x in ',"\n') else s
    lines = ["receipt,answers,nonce,record_hash,prev_hash,chain_hash,voided,void_reason"]
    for b in ballots:
        lines.append(",".join([b["receipt"], _c(b["answers"]), b["nonce"],
                               b["record_hash"], b["prev_hash"], b["chain_hash"],
                               str(b["voided"]).lower(), _c(b["void_reason"])]))
    ballots_csv = "\n".join(lines) + "\n"

    ulines = ["code_hash,used"]
    for snap in db.collection(f"{poll_id}__codes").stream():
        ulines.append(f"{snap.id},{str(bool((snap.to_dict() or {}).get('used'))).lower()}")
    used_codes_csv = "\n".join(ulines) + "\n"
    return ballots_csv, used_codes_csv, head + "\n"


@app.get("/p/<poll_id>/verify/<fname>")
def verify_file(poll_id, fname):
    """PUBLIC verification files — served only after finalize + publish, so
    anyone can recompute the whole result and the tamper-evidence chain:
    ballots.csv (anonymous ballots + chain), used_codes.csv (turnout),
    chain_head.txt (the fingerprint). Anonymous — no voter identity."""
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return "Unknown poll.", 404
    if not (cfg.get("finalized") and cfg.get("results_published")):
        return "Verification files publish after the election is finalized.", 409
    if fname.endswith(".blt"):
        # SECRET contests are absent from ballots.csv by design (their content
        # never enters the identity-linked collection), which left the
        # highest-stakes race — the delegate election — as the one nobody
        # outside administration could recount. The BLT carries rankings and
        # weights and NO identity at all, so publishing it restores public
        # verifiability without touching ballot secrecy.
        qkey = fname[:-4]
        q = next((x for x in poll_questions(cfg)
                  if x["key"] == qkey and q_visibility(x) == "secret"
                  and x["type"] == "ranked"), None)
        if not q:
            return "Not found.", 404
        _, _, secret_rows = _tally_rows(poll_id)
        blt = _blt_text(secret_rows, qkey, q["options"], int(q.get("seats", 1)),
                        title=f"{cfg.get('name', poll_id)} - {q['title']}")
        return Response(blt, mimetype="text/plain",
                        headers={"Content-Disposition": f"inline; filename={fname}",
                                 "Cache-Control": "public, max-age=300"})
    if fname not in ("ballots.csv", "used_codes.csv", "chain_head.txt"):
        return "Not found.", 404
    ballots_csv, used_csv, head = _build_verification(poll_id)
    body = {"ballots.csv": ballots_csv, "used_codes.csv": used_csv,
            "chain_head.txt": head}[fname]
    mt = "text/plain" if fname.endswith(".txt") else "text/csv"
    return Response(body, mimetype=mt,
                    headers={"Content-Disposition": f"inline; filename={fname}",
                             "Cache-Control": "public, max-age=300"})


ROLLCALL_INLINE_MAX = 300     # bigger than this, the page links the CSV only


@app.get("/p/<poll_id>/rollcall/<qkey>.csv")
def rollcall_csv(poll_id, qkey):
    """PUBLIC by-name roll call for a question the poll declares
    `visibility: public` — the recorded vote some bodies require. Same gate as
    the results page: finalized AND published. A question that is `named` or
    `secret` is never served here, whatever the caller asks for."""
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return "Unknown poll.", 404
    if not (cfg.get("finalized") and cfg.get("results_published")):
        return "Roll calls publish after the election is finalized.", 409
    q = next((x for x in poll_questions(cfg)
              if x["key"] == qkey and q_visibility(x) == "public"), None)
    if not q:
        return "Not a published roll-call question.", 404
    return Response(_roll_call_csv(roll_call_rows(poll_id, q)), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename={poll_id}_{qkey}_rollcall.csv",
                             "Cache-Control": "public, max-age=300"})


@app.get("/p/<poll_id>/verify-vote")
def verify_vote_page(poll_id):
    """Plain-language 'verify your vote' guide: the receipt check (now) and the
    independent full-count verification (after close)."""
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return "Unknown poll.", 404
    html = (_VERIFY_PAGE.replace("__POLL_ID__", poll_id)
            .replace("__POLL_NAME__", _esc(cfg.get("name") or poll_id))
            .replace("__SOURCE_URL__", SOURCE_URL))
    return Response(html, mimetype="text/html")


@app.post("/p/<poll_id>/vote")
def vote(poll_id):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    st = window_state(cfg)
    if st == "not_open":
        return jsonify({"error": "not_open", "message": f"Voting for {cfg['name']} opens "
                        f"{fmt_local(cfg.get('opens_at'), cfg)}."}), 403
    if st == "closed":
        closed_msg = (f"Voting has closed for {cfg['name']}"
                      + (f" (closed {fmt_local(cfg['closes_at'], cfg)})"
                         if cfg.get("closes_at") else "") + ".")
        return jsonify({"error": "closed", "message": closed_msg}), 403

    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    if not CODE_RE.match(code):
        return jsonify({"error": "invalid_code_format"}), 400
    try:
        answers, comments = validate_answers(data.get("answers"), cfg)
    except ValueError as bad:
        return jsonify({"error": "invalid_answers", "field": str(bad)}), 400
    if code == cfg.get("test_code"):
        # repeatable test vote: full UX (receipt + confirmation), nothing stored
        return jsonify({"status": "recorded", "receipt": "TEST-" + secrets.token_hex(3).upper()}), 200
    try:
        result = cast_vote(poll_id, cfg, code, answers, comments)
    except LookupError:
        return jsonify({"error": "invalid_code"}), 404
    except gcloud_exc.Aborted:
        return jsonify({"error": "try_again"}), 503
    except Exception:
        # The cast is one transaction, so a failure here means NOTHING was
        # committed — the code is still unused and the voter can safely retry.
        # Never surface the exception text: it can carry ballot content.
        app.logger.exception("vote transaction failed for poll %s", poll_id)
        return jsonify({"error": "try_again"}), 503
    return jsonify(result), 200



# Provisional ballots are the one UNAUTHENTICATED write path — no code, no
# token. A script can therefore mint Firestore documents at will, burying the
# adjudication queue in junk that staff must hand-review and running up
# storage. This is a per-instance throttle (Cloud Run spreads traffic, so the
# effective ceiling is this times the instance count); it is a speed bump for
# casual abuse, NOT a substitute for the edge rate limits in
# infra/STAGING_AND_EDGE.md.
PROV_MAX_PER_IP = 5
PROV_WINDOW_S = 15 * 60
_prov_hits: dict = {}


def _prov_throttled(ip: str) -> bool:
    now = time.time()
    if len(_prov_hits) > 10000:            # bounded: drop everything stale
        for k, v in list(_prov_hits.items()):
            if not [t for t in v if now - t < PROV_WINDOW_S]:
                _prov_hits.pop(k, None)
    hits = [t for t in _prov_hits.get(ip, ()) if now - t < PROV_WINDOW_S]
    if len(hits) >= PROV_MAX_PER_IP:
        _prov_hits[ip] = hits
        return True
    hits.append(now)
    _prov_hits[ip] = hits
    return False


# X-Forwarded-For is CLIENT-SUPPLIED up to the point a trusted proxy appends
# to it. Cloud Run appends the real peer to whatever the caller sent, so the
# LEFTMOST entry is attacker-chosen and the RIGHTMOST entries are the ones our
# own infrastructure wrote. Reading the leftmost entry made every per-IP
# throttle bypassable by rotating a header value. Count in from the right
# instead: TRUSTED_PROXY_HOPS is how many proxies append to XFF in front of the
# app (Cloud Run direct = 1; add 1 if an external HTTPS LB / Cloud Armor sits
# in front, per infra/STAGING_AND_EDGE.md).
try:
    TRUSTED_PROXY_HOPS = max(1, int(os.environ.get("TRUSTED_PROXY_HOPS", "1")))
except (TypeError, ValueError):
    TRUSTED_PROXY_HOPS = 1


def _client_ip() -> str:
    parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",")
             if p.strip()]
    if parts:
        # the entry our outermost trusted proxy appended; clamp so a short
        # header can never walk back into caller-controlled territory
        return parts[max(0, len(parts) - TRUSTED_PROXY_HOPS)]
    return request.remote_addr or "?"


@app.post("/p/<poll_id>/provisional")
def provisional(poll_id):
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    st = window_state(cfg)
    if st != "open":
        return jsonify({"error": st, "message": f"Voting is not open for {cfg['name']}."}), 403
    if _prov_throttled(_client_ip()):
        return jsonify({"error": "too_many_requests",
                        "message": "Too many provisional ballots from this connection. "
                                   "If you are helping several members vote, contact "
                                   "your chapter's election administrator."}), 429

    data = request.get_json(silent=True) or {}
    info = data.get("info", {}) or {}
    try:
        answers, comments = validate_answers(data.get("answers"), cfg)
    except ValueError as bad:
        return jsonify({"error": "invalid_answers", "field": str(bad)}), 400
    if not info.get("first") or not info.get("last") or not info.get("chapter"):
        return jsonify({"error": "missing_info"}), 400
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", info.get("emails", "")):
        return jsonify({"error": "missing_info", "field": "emails"}), 400
    out = cast_provisional(poll_id, answers, comments, info)
    return jsonify(out), (200 if out.get("status") == "provisional_recorded" else 503)


SPLASH = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>RosaVote — Chapter Member Ballot</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml"/>
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
  /* ---- animated intro ---- */
  #intro{position:fixed;inset:0;z-index:100;background:var(--cream);display:flex;
    flex-direction:column;align-items:center;justify-content:center;gap:18px;
    animation:intro-out .7s ease 2.5s forwards}
  #intro svg{width:min(46vw,180px);height:auto;overflow:visible}
  #intro .wordmark{font-family:var(--disp);font-weight:900;text-transform:uppercase;
    font-size:clamp(2.4rem,11vw,4rem);color:var(--dsa);line-height:.9;letter-spacing:.01em;
    opacity:0;animation:word-in .6s ease 1.5s forwards}
  #intro .tagline{font-family:var(--ui);font-weight:700;font-size:.8rem;letter-spacing:.14em;
    text-transform:uppercase;color:rgba(0,0,0,.55);opacity:0;animation:word-in .6s ease 1.9s forwards}
  /* box drops in, then stem draws up, leaves pop, bloom unfurls */
  .an-box{transform-origin:29px 45px;transform:translateY(-6px) scale(.4);opacity:0;
    animation:box-in .5s cubic-bezier(.34,1.56,.64,1) .15s forwards}
  .an-slot{opacity:0;animation:fade-in .3s ease .5s forwards}
  .an-stem{stroke-dasharray:16;stroke-dashoffset:16;animation:draw .5s ease .6s forwards}
  .an-leaf{transform-origin:29px 32px;transform:scale(0);animation:pop .35s cubic-bezier(.34,1.56,.64,1) forwards}
  .an-leaf.l2{animation-delay:1s}.an-leaf.l1{animation-delay:1.1s}
  .an-bloom{transform-origin:29px 16px;transform:scale(0) rotate(-40deg);opacity:0;
    animation:bloom 1s cubic-bezier(.34,1.56,.64,1) 1.15s forwards}
  @keyframes box-in{to{transform:translateY(0) scale(1);opacity:1}}
  @keyframes fade-in{to{opacity:1}}
  @keyframes draw{to{stroke-dashoffset:0}}
  @keyframes pop{to{transform:scale(1)}}
  @keyframes bloom{to{transform:scale(1) rotate(0);opacity:1}}
  @keyframes word-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  @keyframes intro-out{to{opacity:0;visibility:hidden}}
  #intro.hide{display:none}
  @media (prefers-reduced-motion:reduce){
    #intro{animation:intro-out .3s ease 1s forwards}
    #intro *{animation-duration:.01s!important;animation-delay:0s!important;opacity:1!important;
      transform:none!important;stroke-dashoffset:0!important}
  }
</style></head><body>
<div id="intro" aria-hidden="true">
  <svg viewBox="0 0 58 58" role="img" aria-label="RosaVote">
    <g class="an-bloom">
      <circle cx="29" cy="16.5" r="8" fill="#dd1111" stroke="#000" stroke-width="2.2"/>
      <path d="M29 12 c 2.7 0 4.5 1.8 4.5 4 0 2.4-2 4.2-4.5 4.2 s-4.5-1.8-4.5-4.2" fill="none" stroke="#fff5e5" stroke-width="1.8" stroke-linecap="round"/>
      <path d="M29 14.8 c 1.1 0 1.9.7 1.9 1.7" fill="none" stroke="#fff5e5" stroke-width="1.4" stroke-linecap="round"/>
    </g>
    <path class="an-stem" d="M29 24.5 V 38.5" fill="none" stroke="#000" stroke-width="2.4" stroke-linecap="round"/>
    <path class="an-leaf l1" d="M28 30.5 C 25 30 22.8 28.3 22 25.7 C 25.2 26.1 27.4 27.8 28 30.5 z" fill="#dd1111" stroke="#000" stroke-width="1.6"/>
    <path class="an-leaf l2" d="M30 34.5 C 33 34 35.2 32.3 36 29.7 C 32.8 30.1 30.6 31.8 30 34.5 z" fill="#dd1111" stroke="#000" stroke-width="1.6"/>
    <g class="an-box">
      <rect x="20" y="37.5" width="18" height="4" fill="#000"/>
      <rect x="14" y="39" width="30" height="11.5" fill="#dd1111" stroke="#000" stroke-width="2.2"/>
      <path class="an-slot" d="M18.5 45 h 21" stroke="#fff5e5" stroke-width="2" stroke-linecap="round"/>
    </g>
  </svg>
  <div class="wordmark">RosaVote</div>
  <div class="tagline">Independent open-source election platform</div>
</div>
<header class="band"><div class="band-in">
  <div class="wm">RosaVote<small>Member Ballot Demo &middot; Independent &amp; open source</small></div>
  <a class="rose" href="/" aria-label="RosaVote home"><img src="/logo.svg" alt="" width="34" height="34"/></a>
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
    <div class="card-band" style="background:#000">Admin Console</div>
    <div class="card-body">
      <p class="kicker">Election administration</p>
      <p>Build elections, import voter rolls, watch turnout, adjudicate
      provisionals, void &amp; reissue, and view results &mdash; all in the
      browser console.</p>
      <a class="btn" style="background:#000" href="/admin/">Open the Admin Console &rarr;</a>
      <p class="fine" style="margin-top:8px">Developers: RosaVote is API-first &mdash; see the <a href="/api" style="color:var(--dsa);font-weight:700">API reference</a> &middot; <a href="/methods" style="color:var(--dsa);font-weight:700">voting methods</a> &middot; <a href="/accuracy" style="color:var(--dsa);font-weight:700">tabulation accuracy &amp; tests</a> &middot; <a href="/vs-opavote" style="color:var(--dsa);font-weight:700">Why RosaVote</a>.</p>
      <p class="fine" style="margin-top:8px">Just exploring? The console&rsquo;s sign-in page has a
      <b>one-tap demo sign-in</b> that opens as <b>national root admin</b> on a synthetic-data
      sandbox &mdash; the full admin experience, no credentials needed.</p>
      <details style="margin-top:12px">
        <summary>How to get access</summary>
        <div class="dbody">
        <p>The console needs an <b>admin token</b> (entered on its sign-in
        screen; nothing here works without one). <b>The chapter TEST codes at
        the top of this page are voting codes, not admin tokens</b> &mdash;
        they open ballots, never the console. <b>National (root) token:</b>
        held in Secret Manager as <code>ballot-admin-token</code> in the
        <code>rosavote-app</code> project &mdash; operators with project access
        retrieve it via
        <code>gcloud secrets versions access latest --secret ballot-admin-token</code>.
        <b>Chapter tokens:</b> minted by national staff
        (<code>tools/seed_config.py admin --role chapter</code>) and scoped so a
        chapter's admins see only their own poll. Tokens are stored hashed and
        can't be recovered &mdash; keep them in a password manager, and every
        admin action is recorded in the audit log under the token's name.</p>
        </div>
      </details>
      <details>
        <summary>How to test without affecting anything</summary>
        <div class="dbody">
        <p>Use the repeatable <b>TEST codes</b> listed at the top of this page
        &mdash; they walk the full voting experience (ballot, review, receipt)
        but <b>never write a ballot</b>, so test votes can't pollute results or
        turnout. Cast as many as you like. To rehearse the admin side, create a
        throwaway poll with the console's election builder, import a few fake
        member IDs on the Voters tab, vote with the minted codes, close the
        poll, and watch results unlock &mdash; then leave the test poll closed
        or ask national to archive it.</p>
        </div>
      </details>
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
        &mdash; cite Scottish STV and the expanded-count alternates rule in the chapter's
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
        are counted by <b>Scottish STV</b> (SSI 2007/42). Delegates use the <b>expanded count</b>
        for alternates (the default; an admin can switch a contest to the replacement re-run):
        an STV count for the delegate seats decides the delegation; the same
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
        <summary>Why RosaVote is the preferred alternative</summary>
        <div class="dbody">
        <p>OpaVote is a good partner to DSA &mdash; an affordable, dependable service that has
        run our elections for years, and a solid choice, especially for high-stakes or
        contested races where an independent third party adds trust. This app doesn't force a
        migration off it; it's the <b>preferred alternative</b> for most elections:
        <b>stronger, more flexible, more customizable, and more affordable</b>.
        (See the fuller <a href="/vs-opavote">side-by-side</a>.)</p>
        <p><b>More affordable at our scale.</b> OpaVote prices per voter, per election
        (about $0.08/voter) &mdash; reasonable for one vote, but it multiplies across every
        chapter running its own election. This app can run on cloud infrastructure DSA already operates (as this deployment does), so the
        marginal cost of an election is a fraction of a vendor fee; message delivery
        (SMS/postcards) is the main variable, and that's an add-on either way.</p>
        <p><b>Built for DSA's ballots.</b> Because it's open source and free to shape, this app bakes in what
        OpaVote can only handle by hand: codes generated straight from the deduplicated
        membership roll (no list uploads), multi-section ballots with per-section visibility
        (named poll votes beside a secret delegate ballot on one page), diversity-quota
        reservations, Article&nbsp;V timing, the expanded-count alternates rule, chapter-unique
        questions, and randomized slates. There they're workarounds; here they're
        configuration.</p>
        <p><b>Verifiable and owned.</b> Same trusted Scottish STV math (OpaVote's descends
        from OpenSTV; ours implements the same statute, reading the same BLT files), but this
        app adds per-voter receipts and a public hash chain so members verify their own vote
        and anyone can recount &mdash; and the code is open source while the data and
        infrastructure stay under the control of the organization running the election,
        not a vendor. Ballots are retained on its own terms instead of being deleted about
        12 weeks after the election.</p>
        <p><b>The trade.</b> The organization runs the infrastructure itself &mdash; IAM, audit logs,
        hardening, an accountable administrator &mdash; where OpaVote hands you a fully hosted
        service. That's real work, and it's the price of owning our own democratic
        infrastructure. For most elections at our scale, it's worth it.</p>
        <p><b>Not a forced migration.</b> OpaVote is a good partner, and no chapter has to leave
        it. For an especially high-stakes or contested election, some will prefer the
        independence of a neutral third-party vendor with commercial support &mdash; and OpaVote
        stays an excellent choice there. RosaVote is the preferred default everywhere else,
        ready whenever a chapter is.</p>
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
        <p><b>Voting features.</b> OpenSlides handles in-meeting voting &mdash; Yes/No/Abstain
        and per-candidate (approval-style) polls, weighted and anonymous &mdash; alongside its
        core strengths in motions, amendments, quorum tracking, and the projector-ready floor
        process this app doesn't attempt. What it isn't built for today is <b>ranked-choice
        STV elections</b> (its poll methods are Yes/No/Abstain variants; ranked STV isn't among
        them yet) &mdash; and that's exactly this app's lane: Scottish &amp; Meek STV,
        the expanded-count alternates recount, code-gated voting for 120k members with no
        accounts to provision, codes generated straight from the deduplicated membership roll,
        SMS and postcard delivery with reminder waves, self-serve provisional ballots,
        per-section visibility (named poll votes beside a secret delegate ballot), chapter-unique
        questions on one styled page, randomized candidate order, and per-chapter voting windows
        on a single deployment. Different tools for different jobs.</p>
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
<footer><b>RosaVote</b> &middot; <a href="/about" style="color:inherit">About</a> &middot; <a href="/terms" style="color:inherit">Terms</a> &middot; <a href="/privacy" style="color:inherit">Privacy</a> &middot; <a href="/methods" style="color:inherit">Methods</a> &middot; <a href="/vs-opavote" style="color:inherit">Why RosaVote</a> &middot; <a href="__SOURCE_URL__" style="color:inherit">Source (AGPL-3.0)</a><br/>Questions? Email <b>support@rosavote.org</b><br/>An independent open-source project &mdash; not affiliated with or endorsed by the Democratic Socialists of America (DSA)<br/>Built with 🌹 by Walker Green</footer>
<script>
(function(){
  var intro = document.getElementById("intro");
  if(!intro) return;
  // once per browser session: skip the animation on repeat visits
  try{ if(sessionStorage.getItem("rv_intro")){ intro.classList.add("hide"); return; }
       sessionStorage.setItem("rv_intro","1"); }catch(e){}
  // let anyone tap through it, and hard-remove after it finishes
  intro.addEventListener("click", function(){ intro.classList.add("hide"); });
  setTimeout(function(){ intro.classList.add("hide"); }, 3600);
})();
</script>
</body></html>"""


@app.get("/")
def index():
    """Marketing page on the apex host; the app splash everywhere else."""
    host = (request.host or "").split(":", 1)[0].lower()
    if host in MARKETING_HOSTS:
        return Response(_marketing_doc(), mimetype="text/html")
    polls = load_polls()
    # pid matches POLL_ID_RE and test_code matches CODE_RE (both safe charsets),
    # but cfg["name"] is free-form admin text — escape it into the link markup.
    links = "".join(f'<li><a href="/p/{pid}/">{_esc(cfg["name"])}</a></li>'
                    for pid, cfg in polls.items())
    rows = "".join(
        f'<p style="margin:0 0 4px"><b>{_esc(cfg["name"])}</b><br/>'
        f'<code class="tc" style="font-size:.86rem">{_esc(cfg["test_code"])}</code></p>'
        f'<a class="btn" style="min-height:40px;font-size:1.05rem;margin:6px 0 14px" '
        f'href="/p/{pid}/v/{cfg["test_code"]}">Open {_esc(cfg["name"])} ballot with test code &rarr;</a>'
        for pid, cfg in polls.items() if cfg.get("test_code")
    )
    html = (SPLASH.replace("__TEST_ROWS__", rows).replace("__CHAPTER_LINKS__", links)
            .replace("__SOURCE_URL__", SOURCE_URL))
    return Response(html, mimetype="text/html")


# ---- marketing landing (/about) -----------------------------------------
# The RosaVote pitch page (features, OpaVote comparison, cost calculator,
# OpenSlides "different tools" section). Authored as a self-contained body in
# marketing.html; wrapped in a full HTML document here so it serves standalone.
_MARKETING_DOC = None


def _marketing_doc() -> str:
    global _MARKETING_DOC
    if _MARKETING_DOC is None:
        raw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "marketing.html"), encoding="utf-8").read()
        m = re.search(r"<title>(.*?)</title>", raw, re.S)
        title = (m.group(1).strip() if m else "RosaVote")
        body = raw.replace(m.group(0), "", 1) if m else raw
        # The authored file links to the app by absolute run.app URL (needed for
        # the off-site artifact copy). Point those at APP_ORIGIN (e.g.
        # https://app.rosavote.org) so the marketing page on the apex sends
        # visitors to the app host; empty falls back to same-origin.
        body = body.replace("https://member-ballot-v3-62155002849.us-east1.run.app", APP_ORIGIN)
        _MARKETING_DOC = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"color-scheme\" content=\"light dark\">"
            f"<title>{title}</title></head><body>{body}</body></html>")
    return _MARKETING_DOC


@app.get("/about")
def about_page():
    return Response(_marketing_doc(), mimetype="text/html")


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


# ---- admin auth throttle -------------------------------------------------
# A presented-but-invalid admin token is a sign-in attempt. Each one costs a
# Firestore read (config__admins lookup), and operator-chosen tokens may be as
# short as 12 chars, so unlimited attempts allow both online brute force and a
# read-amplification nuisance. Lock an address out after too many FAILED
# attempts in a window — but never block a VALID token, so a legitimate admin
# behind the same NAT as an attacker is not collaterally locked out (the gate
# below checks the token first and only 429s an attempt that is itself invalid).
ADMIN_AUTH_MAX_FAILS = max(1, int(os.environ.get("ADMIN_AUTH_MAX_FAILS", "10") or 10))
ADMIN_AUTH_WINDOW_S = max(1, int(os.environ.get("ADMIN_AUTH_WINDOW_S", "300") or 300))
_auth_hits: dict = {}


def _auth_recent(ip: str) -> list:
    now = time.time()
    if len(_auth_hits) > 10000:                # bound memory: drop stale keys
        for k, v in list(_auth_hits.items()):
            if not [t for t in v if now - t < ADMIN_AUTH_WINDOW_S]:
                _auth_hits.pop(k, None)
    return [t for t in _auth_hits.get(ip, ()) if now - t < ADMIN_AUTH_WINDOW_S]


def _auth_locked(ip: str) -> bool:
    return len(_auth_recent(ip)) >= ADMIN_AUTH_MAX_FAILS


def _auth_fail(ip: str):
    hits = _auth_recent(ip)
    hits.append(time.time())
    _auth_hits[ip] = hits


def _auth_ok(ip: str):
    _auth_hits.pop(ip, None)                    # a valid sign-in clears the streak


@app.before_request
def _admin_auth_gate():
    """Resolve the admin token once per request and throttle brute force.
    Guards only the token-gated admin surface; the console shell and every
    public path are untouched. A valid token always passes (and resets the
    failure streak); an invalid token is counted, and once an address is over
    the limit its further INVALID attempts get 429 — valid ones never do."""
    path = request.path
    if not (path.startswith("/admin/api/") or path.endswith("/admin/void")):
        return None
    tok = request.headers.get("X-Admin-Token", "")
    ident = _admin_identity(tok) if tok else None
    g.admin_ident = ident
    ip = _client_ip()
    if ident is not None:
        _auth_ok(ip)
        return None
    if tok:                                     # a real (failed) sign-in attempt
        if _auth_locked(ip):
            return jsonify({"error": "too_many_auth_attempts",
                            "message": "Too many failed admin sign-ins from this "
                                       "address. Wait a few minutes and try again."}), 429
        _auth_fail(ip)
    return None


def require_admin(poll_id: str = None, national_only: bool = False):
    """Resolve the caller's admin identity, or None if unauthorized.
    Chapter-scoped tokens only reach polls in their own list. Reuses the
    identity the before_request gate already resolved (so no double lookup)."""
    ident = getattr(g, "admin_ident", None)
    if ident is None:
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


def _parse_when(body, key, tzinfo, errors):
    """Window bound: unix seconds, or a naive 'YYYY-MM-DDTHH:MM' interpreted
    in the poll's timezone."""
    v = body.get(key)
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return int(v)
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(v)).replace(tzinfo=tzinfo).timestamp())
    except ValueError:
        errors.append(f"{key} must be unix seconds or YYYY-MM-DDTHH:MM (poll-local time)")
        return None


QKEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
QUESTION_TYPES = ("yesno", "ranked", "multi", "text", "score")


def _validate_constraints(cons_in, where, errors):
    cons = []
    for i, con in enumerate(cons_in):
        if not isinstance(con, dict):
            errors.append(f"{where}: constraints[{i}] must be an object")
            continue
        tag = str(con.get("tag") or "").strip().lower().replace(" ", "_")
        label = str(con.get("label") or "").strip()
        kind = "max" if "max" in con else "min" if "min" in con else None
        try:
            bound = int(con.get(kind)) if kind else -1
        except (TypeError, ValueError):
            bound = -1
        if not tag or kind is None or bound < 0:
            errors.append(f"{where}: constraints[{i}] needs tag and an "
                          "integer min or max ≥ 0")
            continue
        nc = {"tag": tag, kind: bound}
        if label:
            nc["label"] = label
        if con.get("local"):
            # enforced WITHIN this contest (no later-contest relief) — e.g.
            # "at least one of the two co-chairs is a non-cis man"
            nc["local"] = True
        cons.append(nc)
    return cons


def _validate_questions(qs, errors, warnings):
    """Validate a generic questions list; returns the normalized list."""
    if not isinstance(qs, list) or not qs:
        errors.append("questions must be a non-empty list")
        return []
    out, keys = [], set()
    for i, q in enumerate(qs):
        if not isinstance(q, dict):
            errors.append(f"questions[{i}] must be an object")
            continue
        key = str(q.get("key") or "").strip()
        typ = str(q.get("type") or "").strip()
        title = str(q.get("title") or "").strip()
        if not QKEY_RE.match(key):
            errors.append(f"questions[{i}]: key must match [a-z][a-z0-9_]{{0,31}}")
            continue
        if key in keys:
            errors.append(f"duplicate question key {key!r}")
            continue
        keys.add(key)
        if typ not in QUESTION_TYPES:
            errors.append(f"question {key!r}: type must be one of {QUESTION_TYPES}")
            continue
        if not title:
            errors.append(f"question {key!r}: title is required")
        nq = {"key": key, "type": typ, "title": title}
        if q.get("label"):
            nq["label"] = str(q["label"])
        text = q.get("text")
        if text:
            parts = text if isinstance(text, list) else [text]
            nq["text"] = [sanitize_html(p) for p in parts]   # rendered as HTML
        sect = q.get("section")
        if isinstance(sect, dict):
            nq["section"] = {"style": int(sect.get("style") or 1),
                             "kicker": str(sect.get("kicker") or ""),
                             "title": str(sect.get("title") or ""),
                             "sub": sanitize_html(sect.get("sub") or "")}
        if "allow_abstain" in q:
            nq["allow_abstain"] = bool(q["allow_abstain"])
        if "required" in q:
            nq["required"] = bool(q["required"])

        raw_vis = str(q.get("visibility") or "").strip().lower()
        if raw_vis and raw_vis not in VISIBILITIES:
            errors.append(f"question {key!r}: visibility must be one of {VISIBILITIES}")
        vis = q_visibility(q)
        if typ == "text" and vis != "named":
            # Free text is stored as an identity-linked comment, never in the
            # canonical answers — it can be neither anonymized nor published.
            errors.append(f"question {key!r}: text questions are always 'named' "
                          "(free text is never published or anonymized)")
            vis = "named"
        nq["visibility"] = vis
        # `secret` stays the storage flag every downstream path reads; it is
        # now derived from visibility rather than set independently.
        nq["secret"] = (vis == "secret")

        if typ in ("ranked", "multi", "score"):
            opts, seen = [], set()
            for c in (q.get("options") or []):
                cid = str((c.get("id") if isinstance(c, dict) else c[0]) or "").strip().upper()
                cname = str((c.get("name") if isinstance(c, dict) else c[1]) or "").strip()
                sub = str((c.get("sub") or "") if isinstance(c, dict) else "")
                if not CAND_ID_RE.match(cid) or cid == "ABSTAIN":
                    errors.append(f"question {key!r}: bad option id {cid!r} "
                                  "([A-Z0-9_]{2,32}, ABSTAIN reserved)")
                elif cid in seen:
                    errors.append(f"question {key!r}: duplicate option id {cid!r}")
                elif not cname:
                    errors.append(f"question {key!r}: option {cid!r} needs a display name")
                else:
                    seen.add(cid)
                    opt = {"id": cid, "name": cname, "sub": sub}
                    if isinstance(c, dict) and "tags" in c:
                        # explicit tags key — even [] — means "attributes were
                        # collected; none of the tagged categories apply"
                        opt["tags"] = [str(t).strip().lower().replace(" ", "_")
                                       for t in (c.get("tags") or [])
                                       if str(t).strip()][:8]
                    opts.append(opt)
            if not opts:
                errors.append(f"question {key!r}: needs at least one option")
            nq["options"] = opts
            if q.get("abstain_sub"):
                nq["abstain_sub"] = str(q["abstain_sub"])
        if typ == "ranked":
            def _n(field, default, minimum):
                try:
                    v = int(q.get(field, default))
                except (TypeError, ValueError):
                    v = minimum - 1
                if v < minimum:
                    errors.append(f"question {key!r}: {field} must be an integer ≥ {minimum}")
                return max(v, minimum)
            nq["seats"] = _n("seats", 1, 1)
            nq["alternates"] = _n("alternates", 0, 0)
            if nq["alternates"]:
                am = str(q.get("alternate_method") or "expanded").strip().lower()
                if am not in ("expanded", "replacement"):
                    errors.append(f"question {key!r}: alternate_method must be "
                                  "expanded or replacement")
                    am = "expanded"
                nq["alternate_method"] = am
            if q.get("require_full"):
                nq["require_full"] = True
            ew = q.get("eliminate_winners_of")
            if ew:
                if not isinstance(ew, list) or not all(isinstance(k, str) for k in ew):
                    errors.append(f"question {key!r}: eliminate_winners_of must be a "
                                  "list of earlier question keys")
                else:
                    earlier = {x.get("key") for x in qs[:i]}
                    bad = [k for k in ew if k not in earlier]
                    if bad:
                        errors.append(f"question {key!r}: eliminate_winners_of "
                                      f"references non-earlier question(s): {', '.join(bad)}")
                    nq["eliminate_winners_of"] = [k for k in ew if k in earlier]
            method = str(q.get("method") or "scottish").strip().lower()
            if method not in ("scottish", "meek"):
                errors.append(f"question {key!r}: method must be scottish or meek")
                method = "scottish"
            nq["method"] = method   # meek is required for YDSA delegate elections
            # quota constraints for leadership elections (NPC-style: e.g.
            # max 13 cis_man; min 8 marginalized) — chapters set their own
            cons_in = q.get("constraints") or []
            if q.get("quota_group"):
                nq["quota_group"] = str(q["quota_group"]).strip().lower()
            if cons_in:
                cons = _validate_constraints(cons_in, f"question {key!r}", errors)
                mins = sum(c["min"] for c in cons if "min" in c)
                if mins > nq["seats"]:
                    errors.append(f"question {key!r}: constraint minimums ({mins}) "
                                  f"exceed the {nq['seats']} seats")
                if cons:
                    nq["constraints"] = cons
            if cons_in or nq.get("quota_group"):
                # quotas require COLLECTED attributes: every candidate needs an
                # explicit tags list (empty = collected, none apply)
                missing = [o["id"] for o in nq["options"] if "tags" not in o]
                if missing:
                    errors.append(f"question {key!r}: quota requirements need "
                                  "collected attributes — add a tags list (may "
                                  f"be []) for: {', '.join(missing)}")
                all_tags = set()
                for o in nq["options"]:
                    all_tags |= set(o.get("tags") or [])
                for con in (nq.get("constraints") or []):
                    if con["tag"] not in all_tags:
                        warnings.append(f"question {key!r}: constraint tag "
                                        f"{con['tag']!r} matches no candidate tags")
            # nq["secret"] already came from visibility above; `delegate`
            # implies it, which q_visibility() accounts for.
            nq["delegate"] = bool(q.get("delegate"))
            nq["shuffle"] = bool(q.get("shuffle", nq["secret"]))
            for f in ("real_delegates", "real_alternates"):
                if q.get(f):
                    nq[f] = int(q[f])
            if nq["options"] and len(nq["options"]) <= nq["seats"] + nq["alternates"]:
                warnings.append(f"question {key!r} uncontested: {len(nq['options'])} "
                                f"option(s) for {nq['seats']} seat(s) + "
                                f"{nq['alternates']} alternate(s)")
        elif typ == "score":
            # Score / STAR voting: voters rate every candidate 0..max_score.
            try:
                seats = int(q.get("seats", 1))
            except (TypeError, ValueError):
                seats = 0
            if seats < 1:
                errors.append(f"question {key!r}: seats must be an integer ≥ 1")
                seats = 1
            nq["seats"] = seats
            try:
                max_s = int(q.get("max_score", 2))
            except (TypeError, ValueError):
                max_s = 0
            if not 1 <= max_s <= 10:
                errors.append(f"question {key!r}: max_score must be between 1 and 10")
                max_s = 2
            nq["max_score"] = max_s
            method = str(q.get("method") or "score").strip().lower()
            if method not in ("score", "star", "star_pr"):
                errors.append(f"question {key!r}: method must be score, star, or star_pr")
                method = "score"
            nq["method"] = method   # star = STAR (Bloc if multi); star_pr = proportional
            # by default every candidate must be scored (spec requirement); a
            # chapter may relax this so unrated candidates simply score 0
            nq["require_full"] = bool(q.get("require_full", True))
            cons_in = q.get("constraints") or []
            if q.get("quota_group"):
                nq["quota_group"] = str(q["quota_group"]).strip().lower()
            if cons_in:
                cons = _validate_constraints(cons_in, f"question {key!r}", errors)
                mins = sum(c["min"] for c in cons if "min" in c)
                if mins > seats:
                    errors.append(f"question {key!r}: constraint minimums ({mins}) "
                                  f"exceed the {seats} seats")
                if cons:
                    nq["constraints"] = cons
            if cons_in or nq.get("quota_group"):
                missing = [o["id"] for o in nq["options"] if "tags" not in o]
                if missing:
                    errors.append(f"question {key!r}: quota requirements need "
                                  "collected attributes — add a tags list (may "
                                  f"be []) for: {', '.join(missing)}")
            if method == "star" and seats > 1:
                warnings.append(f"question {key!r}: multi-seat STAR is sequential "
                                "(majoritarian), not proportional — use ranked STV "
                                "or Meek for a proportional multi-winner contest")
            nq["shuffle"] = bool(q.get("shuffle", nq["secret"]))
            if nq["options"] and len(nq["options"]) <= seats:
                warnings.append(f"question {key!r} uncontested: {len(nq['options'])} "
                                f"option(s) for {seats} seat(s)")
        elif typ == "text":
            try:
                mx = int(q.get("max", TEXT_MAX_DEFAULT))
            except (TypeError, ValueError):
                mx = 0
            if not 1 <= mx <= 10000:
                errors.append(f"question {key!r}: max must be between 1 and 10000")
                mx = TEXT_MAX_DEFAULT
            nq["max"] = mx
        elif typ == "yesno" and isinstance(q.get("option_subs"), dict):
            nq["option_subs"] = {str(k).upper(): str(v)
                                 for k, v in q["option_subs"].items()}
        out.append(nq)
    return out


def validate_poll_config(poll_id: str, body: dict):
    """Election-builder validation. Returns (normalized_cfg, errors, warnings).

    Ballots are either a generic `questions` list (any mix of yesno / ranked /
    multi / text — standalone referendums, officer elections, full combined
    ballots) or the legacy combined-demo shape (q6/q8/q7 fields).

    Art. V §5 is enforced structurally for DELEGATE elections (a ranked
    question flagged delegate:true, or the legacy demo ballot): when a
    convention date is given, the whole voting window must sit inside
    [convention − 4 months, convention − 45 days] (dates in the poll's own
    timezone) and apportionment must be confirmed done. convention_date is
    optional — without it, delegate polls get a warning, everything else
    validates silently."""
    errors, warnings = [], []
    if not POLL_ID_RE.match(poll_id or ""):
        errors.append("poll_id must match [a-z0-9_]{3,64}")

    name = str(body.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    test_code = str(body.get("test_code") or "").strip()
    if test_code and not CODE_RE.match(test_code):
        errors.append("test_code must match [A-Za-z0-9_-]{12,64}")

    tz_name = str(body.get("timezone") or "America/New_York").strip()
    from zoneinfo import ZoneInfo
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        errors.append(f"unknown timezone {tz_name!r} (use an IANA name like America/Chicago)")
        tzinfo = ZoneInfo("America/New_York")

    opens_at = _parse_when(body, "opens_at", tzinfo, errors)
    closes_at = _parse_when(body, "closes_at", tzinfo, errors)
    if opens_at and closes_at and opens_at >= closes_at:
        errors.append("opens_at must be before closes_at")

    questions = None
    legacy = {}
    if body.get("questions") is not None:
        questions = _validate_questions(body.get("questions"), errors, warnings)
        has_delegate = any(q.get("delegate") for q in questions)
    else:
        # legacy combined-demo shape: shared questions + q6/q8/q7 chapter fields
        has_delegate = True
        q6 = str(body.get("q6") or "").strip()
        q8 = str(body.get("q8") or "").strip()
        if not q6 or not q8:
            errors.append("both local-issue questions (q6, q8) are required")
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
        legacy = {"q6": q6, "q8": q8,
                  "q7": {"seats": seats, "alternates": alternates,
                         "real_delegates": real_delegates,
                         "real_alternates": real_alternates, "candidates": cands}}

    # FULL-BODY quota groups: constraints spanning several contests (e.g.
    # co-chairs + at-large all count toward one requirement)
    qgroups = {}
    if isinstance(body.get("quota_groups"), dict):
        for gname, cons_list in body["quota_groups"].items():
            g = str(gname).strip().lower()
            gcons = _validate_constraints(cons_list or [], f"quota_groups[{g!r}]", errors)
            if gcons:
                qgroups[g] = gcons
    if questions is not None:
        for q in questions:
            gg = q.get("quota_group")
            if gg and gg not in qgroups:
                errors.append(f"question {q.get('key')!r}: quota_group {gg!r} "
                              "is not defined in quota_groups")
        for g, gcons in qgroups.items():
            members = [q for q in questions
                       if q.get("quota_group") == g and q.get("type") == "ranked"]
            if not members:
                warnings.append(f"quota_groups[{g!r}] has no ranked member questions")
                continue
            total_seats = sum(int(q.get("seats", 1)) for q in members)
            mins = sum(c["min"] for c in gcons if "min" in c)
            if mins > total_seats:
                errors.append(f"quota_groups[{g!r}]: minimums ({mins}) exceed "
                              f"the group's {total_seats} total seats")

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
                from datetime import datetime
                earliest = _months_before(conv, ART5_MAX_MONTHS)
                latest = conv - timedelta(days=ART5_MIN_DAYS)
                w_open = datetime.fromtimestamp(opens_at, tzinfo).date()
                w_close = datetime.fromtimestamp(closes_at, tzinfo).date()
                if w_open < earliest:
                    errors.append(f"Art. V: opens {w_open}, but no earlier than "
                                  f"{earliest} (4 months before convention)")
                if w_close > latest:
                    errors.append(f"Art. V: closes {w_close}, but no later than "
                                  f"{latest} (45 days before convention)")
    elif has_delegate:
        warnings.append("no convention_date — Art. V delegate-window validation skipped")

    cfg = {
        "name": name, "opens_at": opens_at, "closes_at": closes_at,
        "timezone": tz_name,
        "test_code": test_code or None,
        "convention_date": conv_raw or None, "apportionment_done": apportioned,
    }
    if qgroups:
        cfg["quota_groups"] = qgroups
    if body.get("demo"):
        cfg["demo"] = True   # demo/test poll: stays votable even when finalized
    if body.get("admin_sees_answers"):
        # Off by default: troubleshooting works from recorded-yes/no alone.
        # A body that wants administrators able to read a named ballot
        # (the pre-2026-07 behavior) opts in here, per poll.
        cfg["admin_sees_answers"] = True
    if questions is not None:
        cfg["questions"] = questions
    else:
        cfg.update(legacy)
    return cfg, errors, warnings


ADMIN_CONSOLE = open(os.path.join(os.path.dirname(__file__), "admin_console.html")).read()


@app.get("/admin/")
def admin_console_page():
    """Static console shell — every API it calls is token-gated."""
    return Response(ADMIN_CONSOLE, mimetype="text/html")


@app.post("/admin/api/admins")
def admin_mint_admin():
    """Mint a scoped admin token (root only, audited). Body: {name, role:
    national|chapter, polls: [...], token?: <plaintext ≥12 chars — otherwise
    generated>}. The plaintext is returned ONCE; only its hash is stored."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    role = str(data.get("role") or "chapter").strip()
    polls = [str(p) for p in (data.get("polls") or [])]
    token = str(data.get("token") or "") or secrets.token_urlsafe(24)
    if not name or role not in ("national", "chapter") or len(token) < 12 or len(token) > 128:
        return jsonify({"error": "bad_request",
                        "message": "name, role national|chapter, and (optional) "
                                   "token ≥12 chars required"}), 400
    if role == "chapter" and not polls:
        return jsonify({"error": "bad_request", "message": "chapter role needs polls"}), 400
    db.collection(ADMINS_COLL).document(code_hash(token)).set({
        "name": name, "role": role, "polls": polls, "active": True,
        "minted_by": ident["name"],
    })
    _audit("config", "admin_minted", ident["name"], admin_name=name,
           role=role, polls=polls)
    return jsonify({"ok": True, "name": name, "role": role, "polls": polls,
                    "token": token}), 200   # shown once — never stored in plaintext


@app.post("/admin/api/count_blt")
def admin_count_blt():
    """Manual count workbench: POST a raw .blt file body ({"blt": "..."}),
    get the full Scottish STV round-by-round result. Touches no election
    data — any signed-in admin (including the public demo token) may use it
    to verify a downloaded ballot file or experiment with edits."""
    ident = require_admin()
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    blt = str((request.get_json(silent=True) or {}).get("blt") or "")
    if not blt.strip():
        return jsonify({"error": "bad_request", "message": "blt text required"}), 400
    if len(blt) > 2_000_000:
        return jsonify({"error": "too_large", "message": "cap is 2 MB"}), 400
    import stv_tabulate
    data = request.get_json(silent=True) or {}
    cons = data.get("constraints") or None
    ctags = data.get("cand_tags") or None
    try:
        res = stv_tabulate.count(blt, constraints=cons, cand_tags=ctags)
    except Exception as e:
        return jsonify({"error": "parse_or_count_failed",
                        "message": f"{type(e).__name__}: check the BLT format "
                                   "(header 'candidates seats'; ballot lines "
                                   "'weight rank1 rank2 … 0'; a lone 0; quoted "
                                   "names; quoted title)"}), 400
    return jsonify(res), 200


@app.get("/admin/api/whoami")
def admin_whoami():
    ident = require_admin()
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(ident), 200


@app.get("/admin/api/polls")
def admin_list_polls():
    """List polls for the console. Unlike the voting/public paths (which use
    load_polls and never see archived polls), this INCLUDES archived polls
    with an `archived: true` flag so an admin can review and unarchive them."""
    ident = require_admin()
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    out = {}
    try:
        for snap in db.collection(CONFIG_COLL).stream():
            d = snap.to_dict() or {}
            cfg = _normalize_cfg(d)
            out[snap.id] = dict(cfg_to_doc(cfg), state=window_state(cfg),
                                archived=bool(d.get("archived")))
    except Exception:
        out = {}
    if not out:   # unseeded — fall back to the built-in demo chapters
        out = {pid: dict(cfg_to_doc(cfg), state=window_state(cfg), archived=False)
               for pid, cfg in CHAPTERS.items()}
    if ident["role"] != "national":
        out = {pid: cfg for pid, cfg in out.items() if pid in ident["polls"]}
    return jsonify(out), 200


@app.post("/admin/api/polls/<poll_id>/archive")
def admin_archive_poll(poll_id):
    """Archive (national only): hide a poll from the voting and public paths
    and from the normal console list. Ballots and results are NOT deleted —
    archiving is reversible with /unarchive. A poll can be archived whether
    open or closed; archiving a still-open poll also stops it accepting votes
    (it leaves the active set)."""
    return _set_archived(poll_id, True)


@app.post("/admin/api/polls/<poll_id>/unarchive")
def admin_unarchive_poll(poll_id):
    """Restore an archived poll to the active set (national only)."""
    return _set_archived(poll_id, False)


def _set_archived(poll_id: str, archived: bool):
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    try:
        snap = db.collection(CONFIG_COLL).document(poll_id).get()
    except Exception:
        snap = None
    if not snap or not getattr(snap, "exists", False):
        return jsonify({"error": "unknown_poll"}), 404
    db.collection(CONFIG_COLL).document(poll_id).update({"archived": archived})
    _audit(poll_id, "archive" if archived else "unarchive", ident["name"])
    load_polls(force=True)
    return jsonify({"ok": True, "archived": archived}), 200


@app.post("/admin/api/polls/<poll_id>")
def admin_save_poll(poll_id):
    """Create or update a poll config (national admins only).

    A FINALIZED poll cannot be edited — and therefore cannot reopen — unless
    the request explicitly sets unfinalize:true. Reopening a certified
    election is a deliberate, audited act, never a side effect of an edit."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    try:
        snap = db.collection(CONFIG_COLL).document(poll_id).get()
        existing = snap.to_dict() if getattr(snap, "exists", False) else None
    except Exception:
        existing = None
    unfinalize = bool(body.get("unfinalize"))
    if existing and existing.get("finalized") and not unfinalize:
        return jsonify({"error": "finalized",
                        "message": "This poll is finalized. Pass unfinalize:true "
                                   "to deliberately reopen it (audited)."}), 409
    cfg, errors, warnings = validate_poll_config(poll_id, body)
    if errors:
        return jsonify({"error": "invalid_config", "errors": errors,
                        "warnings": warnings}), 400
    db.collection(CONFIG_COLL).document(poll_id).set(cfg_to_doc(cfg))
    # a config change invalidates any frozen public results; re-freeze from the
    # new config if still published, else drop the stale frozen doc entirely.
    if cfg.get("finalized") and cfg.get("results_published"):
        _store_published_results(poll_id, cfg)
    else:
        _clear_published(poll_id)
    if existing and existing.get("finalized") and unfinalize:
        _audit(poll_id, "unfinalize_reopen", ident["name"])
    _audit(poll_id, "config_save", ident["name"])
    load_polls(force=True)
    return jsonify({"ok": True, "warnings": warnings}), 200


@app.post("/admin/api/polls/<poll_id>/open")
def admin_open_poll(poll_id):
    """Open-election button: start voting NOW — with or without a schedule.
    Sets opens_at to now; a close time already in the past is cleared (the
    poll runs until closed). Chapter admins may open their own poll, same
    as closing. Finalized polls stay shut — reopening a certified election
    is the deliberate national unfinalize flow, not this button."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    # Read-modify-write on the config doc must start from the STORED doc, not
    # the 60s cache — otherwise flipping a window rewrites the whole document
    # from a stale copy and silently reverts a concurrent builder save.
    cfg = _fresh_cfg_doc(poll_id) or cfg
    if cfg.get("finalized"):
        return jsonify({"error": "finalized",
                        "message": "This election is finalized. Reopening it is the "
                                   "deliberate unfinalize flow (national admins, "
                                   "Reopen button)."}), 409
    now = int(time.time())
    closes = cfg.get("closes_at")
    if closes and closes <= now:
        closes = None            # stale close time — run until closed
    cfg = dict(cfg, opens_at=now - 1, closes_at=closes)
    db.collection(CONFIG_COLL).document(poll_id).set(cfg_to_doc(cfg))
    _audit(poll_id, "open_poll", ident["name"])
    load_polls(force=True)
    return jsonify({"ok": True, "opens_at": cfg["opens_at"],
                    "closes_at": cfg["closes_at"]}), 200


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
    cfg = dict(_fresh_cfg_doc(poll_id) or cfg, closes_at=int(time.time()))
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


# ---- admin: results (finalized polls only) --------------------------------
WEIGHT_MIN, WEIGHT_MAX = 1, 1000


def _blt_text(rows, key, options, seats, title="contest", withdrawn=()):
    """In-memory BLT (same shape make_blt writes) for the STV tabulator.
    rows: (answers_dict, weight) pairs — BLT carries integer ballot weights
    natively, so weighted polls verify in any standard tabulator.
    `withdrawn`: option ids to mark withdrawn (BLT negative-number line) —
    the dropout-recount path."""
    idx = {o["id"]: i + 1 for i, o in enumerate(options)}
    lines = [f"{len(options)} {seats}"]
    wd = [idx[w] for w in withdrawn if w in idx]
    if wd:
        lines.append(" ".join(f"-{n}" for n in sorted(wd)))
    for a, w in rows:
        ranking = [str(x).upper() for x in (a.get(key) or [])]
        if not ranking or ranking == ["ABSTAIN"]:
            lines.append(f"{w} 0")
            continue
        if not set(ranking) <= set(idx):
            continue
        lines.append(f"{w} " + " ".join(str(idx[c]) for c in ranking) + " 0")
    lines.append("0")
    for o in options:
        lines.append('"' + o["name"].replace('"', "'") + '"')
    lines.append('"' + title.replace('"', "'") + '"')
    return "\n".join(lines) + "\n"


def _scores_text(rows, key, options, seats, max_score, title="contest"):
    """In-memory score-ballot file (parseable by stv_tabulate.parse_scores).
    Each ballot lists ``candidate:score`` pairs; an ABSTAIN answer is an
    empty ballot. Same weighted envelope as _blt_text so score contests
    export and cross-check the same way."""
    idx = {o["id"]: i + 1 for i, o in enumerate(options)}
    lines = [f"{len(options)} {seats} {int(max_score)}"]
    for a, w in rows:
        v = a.get(key)
        if not isinstance(v, dict) or not v:      # ABSTAIN / missing => empty
            lines.append(f"{w} 0")
            continue
        toks = [f"{idx[c]}:{int(s)}" for c, s in v.items() if c in idx]
        lines.append(f"{w} " + " ".join(toks) + " 0" if toks else f"{w} 0")
    lines.append("0")
    for o in options:
        lines.append('"' + o["name"].replace('"', "'") + '"')
    lines.append('"' + title.replace('"', "'") + '"')
    return "\n".join(lines) + "\n"


def _code_weights(poll_id: str) -> dict:
    """code_hash -> voter weight (default 1). Weights live on the CODE docs
    so they can be provisioned at import and edited before/during/after the
    election — tallies always resolve the CURRENT weight."""
    weights = {}
    for snap in db.collection(f"{poll_id}__codes").stream():
        d = snap.to_dict() or {}
        try:
            w = int(d.get("weight") or 1)
        except (TypeError, ValueError):
            w = 1
        weights[snap.id] = max(WEIGHT_MIN, min(WEIGHT_MAX, w))
    return weights


def _ballot_weight(d: dict, weights: dict) -> int:
    w = weights.get(d.get("code_hash"))
    if w is None:   # promoted provisionals carry their own weight field
        try:
            w = int(d.get("weight") or 1)
        except (TypeError, ValueError):
            w = 1
    return max(WEIGHT_MIN, min(WEIGHT_MAX, w))


def _tally_rows(poll_id: str):
    """(main_rows, comments_rows, secret_rows) with per-ballot weights."""
    weights = _code_weights(poll_id)
    main_rows, comments_rows = [], []
    for snap in db.collection(f"{poll_id}__ballots").stream():
        d = snap.to_dict() or {}
        if d.get("voided"):
            continue
        main_rows.append((d.get("answers") or {}, _ballot_weight(d, weights)))
        comments_rows.append(d.get("comments") or
                             ({"text": d["comment"]} if d.get("comment") else {}))
    secret_rows = []
    for snap in db.collection(f"{poll_id}__delegate_ballots").stream():
        d = snap.to_dict() or {}
        if not d.get("voided"):
            secret_rows.append((d, _ballot_weight(d, weights)))
    return main_rows, comments_rows, secret_rows


def _answer_display(v, q, names=None) -> str:
    """One voter's answer to one question, as a person reads it. Option ids
    are resolved to candidate names so a roll call is legible without the
    config next to it."""
    names = names if names is not None else {
        o["id"]: o["name"] for o in (q.get("options") or [])}
    if v is None or v == "":
        return "(no answer)"
    if isinstance(v, dict):        # score: {option_id: rating}
        return "; ".join(f"{names.get(k, k)}={v[k]}" for k in sorted(v))
    if isinstance(v, list):        # ranked (ordered) / multi (a set)
        if v == ["ABSTAIN"]:
            return "ABSTAIN"
        sep = " > " if q["type"] == "ranked" else ", "
        return sep.join(names.get(str(x).upper(), str(x)) for x in v)
    return str(v)


def roll_call_rows(poll_id: str, q: dict):
    """By-name record for a `public` question: who voted, and how.

    Only ever called for questions the poll declares `visibility: public` —
    the recorded roll call some bodies require. Voided ballots are included
    and flagged rather than dropped, because a roll call that quietly omits
    a cancelled vote reads as if the member never voted at all."""
    names = {o["id"]: o["name"] for o in (q.get("options") or [])}
    rows = []
    for snap in db.collection(f"{poll_id}__ballots").stream():
        d = snap.to_dict() or {}
        rows.append({
            "member_id": str(d.get("member_id") or ""),
            "chapter": str(d.get("chapter") or ""),
            "answer": _answer_display((d.get("answers") or {}).get(q["key"]), q, names),
            "voided": bool(d.get("voided")),
            "provisional": bool(d.get("provisional")),
        })
    rows.sort(key=lambda r: (r["chapter"], r["member_id"]))
    return rows


def _roll_call_csv(rows) -> str:
    lines = ["member_id,chapter,answer,voided,provisional"]
    for r in rows:
        lines.append(",".join([_csv_cell(r["member_id"]), _csv_cell(r["chapter"]),
                               _csv_cell(r["answer"]), str(r["voided"]).lower(),
                               str(r["provisional"]).lower()]))
    return "\n".join(lines) + "\n"


def _is_blank_answer(v, typ) -> bool:
    """An answer that carries no real selection — empty or all-abstain. An
    all-blank ballot is the signature of a client glitch or a bot that opened
    and submitted a ballot without a human choosing anything."""
    if v is None:
        return True
    if typ == "yesno":
        return str(v).upper() in ("", "ABSTAIN")
    if typ == "score":
        return v == "ABSTAIN" or (isinstance(v, dict) and not v)
    if isinstance(v, list):        # ranked / multi
        return not v or v == ["ABSTAIN"]
    return not v


def compute_results(poll_id: str, cfg: dict) -> dict:
    """WEIGHTED tallies per question (each ballot counts at its voter's
    current weight; default 1). Ranked contests use the shipped Scottish STV
    tabulator; delegate-style contests also run the alternates recount
    (expanded count by default). Text answers are counted, never displayed."""
    import stv_tabulate
    main_rows, comments_rows, secret_rows = _tally_rows(poll_id)
    weighted = any(w != 1 for _, w in main_rows) or any(w != 1 for _, w in secret_rows)

    out = []
    qgroups = cfg.get("quota_groups") or {}
    group_elected = {g: {} for g in qgroups}   # body-wide per-tag winners so far
    all_questions = poll_questions(cfg)
    for q in all_questions:
        key, typ = q["key"], q["type"]
        entry = {"key": key, "type": typ, "title": q["title"],
                 "secret": bool(q.get("secret")),
                 "visibility": q_visibility(q)}
        rows = secret_rows if q.get("secret") else main_rows
        if typ != "text":
            entry["blank"] = sum(w for a, w in rows
                                 if _is_blank_answer(a.get(key), typ))
        if typ == "yesno":
            counts = {"YES": 0, "NO": 0, "ABSTAIN": 0}
            for a, w in main_rows:
                v = str(a.get(key, "")).upper()
                if v in counts:
                    counts[v] += w
            contested = counts["YES"] + counts["NO"]
            entry["counts"] = counts
            entry["result"] = ("PASSES" if counts["YES"] > counts["NO"] else
                               "FAILS" if counts["NO"] > counts["YES"] else
                               "TIE") if contested else "NO CONTEST BALLOTS"
        elif typ == "ranked":
            options = q["options"]
            seats = int(q.get("seats", 1))
            # cross-contest elimination: withdraw the winners of the named
            # earlier contests (e.g. officers who won a seat are removed from
            # the at-large race — Metro DC rule). Match people by option id.
            elim = set()
            for rk in (q.get("eliminate_winners_of") or []):
                ref_entry = next((e for e in out if e["key"] == rk), None)
                ref_q = next((m for m in all_questions if m["key"] == rk), None)
                if ref_entry and ref_q:
                    n2i = {o["name"]: o["id"] for o in ref_q.get("options", [])}
                    for w in ref_entry.get("winners", []):
                        if n2i.get(w):
                            elim.add(n2i[w])
            wd = tuple(i for i in elim if i in {o["id"] for o in options})
            if wd:
                id2name = {o["id"]: o["name"] for o in options}
                entry["eliminated"] = [id2name[i] for i in wd]
            cons = q.get("constraints") or None
            group = q.get("quota_group")
            gpre, glater_seats, glater_supply = None, 0, None
            if group and group in qgroups:
                cons = list(qgroups[group]) + [dict(c, local=True)
                        for c in (q.get("constraints") or [])]
                gpre = dict(group_elected[group])
                seen_self, supply = False, {}
                for m in all_questions:
                    if m.get("quota_group") != group or m["type"] != "ranked":
                        continue
                    if m["key"] == key:
                        seen_self = True
                        continue
                    if not seen_self:
                        continue
                    glater_seats += int(m.get("seats", 1))
                    for o in m["options"]:
                        for t in (o.get("tags") or []):
                            supply[t] = supply.get(t, 0) + 1
                glater_supply = supply or None
                entry["quota_group"] = group
                entry["group_partial"] = glater_seats > 0   # body not complete yet
            ctags = {i + 1: o["tags"] for i, o in enumerate(options)
                     if o.get("tags")} or None
            counter = stv_tabulate.count_meek if q.get("method") == "meek" \
                else stv_tabulate.count
            res = counter(_blt_text(rows, key, options, seats, withdrawn=wd),
                          constraints=cons, cand_tags=ctags,
                          pre_elected=gpre, later_seats=glater_seats,
                          later_supply=glater_supply)
            entry["method_used"] = res["method"]
            entry.update(seats=seats, valid_ballots=res["valid_ballots"],
                         quota=res["quota"], winners=res["winners"],
                         first_prefs=res["stages"][0]["totals"],
                         stages=res["stages"])   # full round-by-round data for charts
            if res.get("constraints"):
                entry["constraints"] = res["constraints"]
            alts = int(q.get("alternates", 0))
            if alts:
                alt_method = q.get("alternate_method") or "expanded"
                entry["alternate_method"] = alt_method
                if alt_method == "replacement":
                    # remove ALL elected, re-run for the alternate seats — the
                    # alternates that would win if no delegate had run.
                    name2id = {o["name"]: o["id"] for o in options}
                    won = tuple(name2id[w] for w in res["winners"] if w in name2id)
                    recount = counter(_blt_text(rows, key, options, alts,
                                                withdrawn=tuple(wd) + won),
                                      constraints=cons, cand_tags=ctags,
                                      pre_elected=gpre, later_seats=glater_seats,
                                      later_supply=glater_supply)
                    entry["alternates"] = list(recount["winners"])
                else:
                    # expanded count: recount for delegate+alternate seats; the
                    # additional winners become alternates, in order.
                    recount = counter(_blt_text(rows, key, options, seats + alts, withdrawn=wd),
                                      constraints=cons, cand_tags=ctags,
                                      pre_elected=gpre, later_seats=glater_seats,
                                      later_supply=glater_supply)
                    entry["alternates"] = [w for w in recount["winners"]
                                           if w not in res["winners"]]
            if cons:
                # the admin-facing comparison: same ballots, quotas off
                res_u = counter(_blt_text(rows, key, options, seats, withdrawn=wd))
                entry["unconstrained"] = {"winners": res_u["winners"],
                                          "quota": res_u["quota"],
                                          "stages": res_u["stages"]}
                if alts:
                    ru = counter(_blt_text(rows, key, options, seats + alts, withdrawn=wd))
                    entry["unconstrained"]["alternates"] = [
                        w for w in ru["winners"] if w not in res_u["winners"]]
            if group and group in qgroups:
                name_tags = {o["name"]: (o.get("tags") or []) for o in options}
                for w in res["winners"]:
                    for t in name_tags.get(w, []):
                        group_elected[group][t] = group_elected[group].get(t, 0) + 1
        elif typ == "score":
            options = q["options"]
            seats = int(q.get("seats", 1))
            max_s = int(q.get("max_score", 2))
            cons = q.get("constraints") or None
            group = q.get("quota_group")
            gpre, glater_seats, glater_supply = None, 0, None
            if group and group in qgroups:
                cons = list(qgroups[group]) + [dict(c, local=True)
                        for c in (q.get("constraints") or [])]
                gpre = dict(group_elected[group])
                seen_self, supply = False, {}
                for m in all_questions:
                    if m.get("quota_group") != group or m["type"] not in ("ranked", "score"):
                        continue
                    if m["key"] == key:
                        seen_self = True
                        continue
                    if not seen_self:
                        continue
                    glater_seats += int(m.get("seats", 1))
                    for o in m["options"]:
                        for t in (o.get("tags") or []):
                            supply[t] = supply.get(t, 0) + 1
                glater_supply = supply or None
                entry["quota_group"] = group
                entry["group_partial"] = glater_seats > 0
            ctags = {i + 1: o["tags"] for i, o in enumerate(options)
                     if o.get("tags")} or None
            counter = {"star": stv_tabulate.count_star,
                       "star_pr": stv_tabulate.count_star_pr}.get(
                           q.get("method"), stv_tabulate.count_score)
            res = counter(_scores_text(rows, key, options, seats, max_s),
                          constraints=cons, cand_tags=ctags,
                          pre_elected=gpre, later_seats=glater_seats,
                          later_supply=glater_supply)
            entry["method_used"] = res["method"]
            entry.update(seats=seats, max_score=max_s,
                         valid_ballots=res["valid_ballots"],
                         winners=res["winners"],
                         scores=res.get("scores", {}),
                         top_scores=res.get("top_scores", {}),
                         stages=res["stages"])
            if res.get("constraints"):
                entry["constraints"] = res["constraints"]
            if cons:
                res_u = counter(_scores_text(rows, key, options, seats, max_s))
                entry["unconstrained"] = {"winners": res_u["winners"],
                                          "scores": res_u.get("scores", {})}
            if group and group in qgroups:
                name_tags = {o["name"]: (o.get("tags") or []) for o in options}
                for w in res["winners"]:
                    for t in name_tags.get(w, []):
                        group_elected[group][t] = group_elected[group].get(t, 0) + 1
        elif typ == "multi":
            counts = {o["id"]: 0 for o in q["options"]}
            for a, w in main_rows:
                for choice in (a.get(key) or []):
                    if choice in counts:
                        counts[choice] += w
            entry["counts"] = {o["name"]: counts[o["id"]] for o in q["options"]}
        elif typ == "text":
            entry["responses"] = sum(1 for cm in comments_rows if (cm or {}).get(key))
            entry["note"] = "free-text answers stay sealed (admin export only)"
        out.append(entry)
    # poll-level: ballots that are blank on EVERY question they carry — the
    # systemic-glitch / empty-ballot signal (per-collection so secrecy holds).
    main_q = [q for q in all_questions if not q.get("secret") and q["type"] != "text"]
    secret_q = [q for q in all_questions if q.get("secret")]
    fully_blank = 0
    if main_q:
        fully_blank += sum(1 for a, _ in main_rows
                           if all(_is_blank_answer(a.get(q["key"]), q["type"]) for q in main_q))
    if secret_q:
        fully_blank += sum(1 for d, _ in secret_rows
                           if all(_is_blank_answer(d.get(q["key"]), q["type"]) for q in secret_q))
    return {"poll_id": poll_id, "name": cfg.get("name"),
            "finalized_at": cfg.get("finalized_at"),
            "final_counts": cfg.get("final_counts"),
            # secret-ballot-only polls keep their ballots in the secret collection
            "ballots_counted": max(len(main_rows), len(secret_rows)), "weighted": weighted,
            "blank_ballots": fully_blank, "questions": out}


@app.get("/admin/api/polls/<poll_id>/results")
def admin_poll_results(poll_id):
    """Results unlock after finalize. Exception (decided policy: live tallies
    are admin-only + audited): NATIONAL admins may pass ?live=1 to see a live
    tally mid-election for troubleshooting — every live view is audit-logged.
    Chapter tokens always wait for finalize."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    live = bool(request.args.get("live"))
    if not cfg.get("finalized"):
        if not (live and ident["role"] == "national"):
            return jsonify({"error": "not_finalized",
                            "message": "Results unlock when the poll is finalized "
                                       "(automatic ~15 min after close). National "
                                       "admins can view a live tally with ?live=1 "
                                       "(audited)."}), 409
        _audit(poll_id, "live_results_view", ident["name"])
    elif not request.args.get("fresh"):
        cached = _read_published_results(poll_id)
        if cached:
            return jsonify(dict(cached, cached=True)), 200
    out = compute_results(poll_id, cfg)
    out["live"] = not cfg.get("finalized")
    return jsonify(out), 200


@app.get("/admin/api/polls/<poll_id>/lookup")
def admin_ballot_lookup(poll_id):
    """Root-only troubleshooting lookup by member_id or receipt: shows the
    voter's identity-linked answers (admin-visible by decided policy) and
    whether their secret-ballot record exists — NEVER the secret ranking
    itself. Every lookup is audit-logged."""
    ident = require_admin(poll_id, national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    member_id = str(request.args.get("member_id", "")).strip()
    receipt = str(request.args.get("receipt", "")).strip().upper()
    if not member_id and not receipt:
        return jsonify({"error": "bad_request",
                        "message": "pass member_id or receipt"}), 400
    matches = []
    for snap in db.collection(f"{poll_id}__ballots").stream():
        d = snap.to_dict() or {}
        if (member_id and str(d.get("member_id", "")) == member_id) or \
           (receipt and d.get("receipt") == receipt):
            matches.append(d)
    secret_receipts = set()
    if matches:
        wanted = {d.get("receipt") for d in matches}
        for snap in db.collection(f"{poll_id}__delegate_ballots").stream():
            r = (snap.to_dict() or {}).get("receipt")
            if r in wanted:
                secret_receipts.add(r)
    # CONTENT IS OFF BY DEFAULT. Troubleshooting asks "did this member's
    # ballot land?", which recorded-yes/no per question answers completely —
    # the same proof `secret_ballot_recorded` has always given for delegate
    # questions, now applied to all of them. Answers come back only for
    # questions the poll publishes by name anyway, or when the body has
    # deliberately set admin_sees_answers on the poll.
    show = bool(cfg.get("admin_sees_answers"))
    qs = [q for q in poll_questions(cfg) if q["type"] != "text"]
    visible = {q["key"] for q in qs
               if q_visibility(q) == "public" or (show and q_visibility(q) != "secret")}
    _audit(poll_id, "ballot_lookup", ident["name"],
           member_id=member_id or None, receipt=receipt or None,
           found=len(matches), answers_shown=bool(visible))

    def _one(d):
        a = d.get("answers") or {}
        out = {
            "receipt": d.get("receipt"), "member_id": d.get("member_id"),
            "chapter": d.get("chapter"),
            "voided": bool(d.get("voided")), "void_reason": d.get("void_reason"),
            "provisional": bool(d.get("provisional")),
            "record_hash": d.get("record_hash"),
            "secret_ballot_recorded": d.get("receipt") in secret_receipts,
            # per-question proof of recording, with no content
            "recorded": {q["key"]: (q["key"] in a) for q in qs},
            # the blank-ballot signature the console flags, computed here so
            # nobody has to read a ballot to investigate a spike
            "blank": all(_is_blank_answer(a.get(q["key"]), q["type"]) for q in qs) if qs else False,
            "answers_shown": sorted(visible),
        }
        if visible:
            out["answers"] = {k: v for k, v in a.items() if k in visible}
            out["comments"] = d.get("comments") or {} if show else {}
        return out
    return jsonify({"found": len(matches),
                    "ballots": [_one(d) for d in matches]}), 200


@app.post("/admin/api/polls/<poll_id>/recount_preview")
def admin_recount_preview(poll_id):
    """Preview a ranked contest under a DIFFERENT counting method
    (plurality/SNTV, approval, borda, sequential IRV) — comparison only,
    never certification. Same gating as results."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    data = request.get_json(silent=True) or {}
    if not cfg.get("finalized"):
        if not (data.get("live") and ident["role"] == "national"):
            return jsonify({"error": "not_finalized"}), 409
        _audit(poll_id, "live_recount_preview", ident["name"])
    import stv_tabulate
    method = str(data.get("method") or "").strip().lower()
    qkey = str(data.get("question") or "").strip()
    q = next((x for x in poll_questions(cfg)
              if x["key"] == qkey and x["type"] in ("ranked", "score")), None)
    if not q:
        return jsonify({"error": "not_a_ranked_or_score_question"}), 404
    main_rows, _, secret_rows = _tally_rows(poll_id)
    rows = secret_rows if q.get("secret") else main_rows
    # SCORE contest: preview the other of score/star, or a plain-score view.
    if q["type"] == "score":
        if method not in stv_tabulate.SCORE_METHODS:
            return jsonify({"error": "bad_method",
                            "message": f"method must be one of {stv_tabulate.SCORE_METHODS}"}), 400
        max_s = int(q.get("max_score", 2))
        ctags = {i + 1: o["tags"] for i, o in enumerate(q["options"])
                 if o.get("tags")} or None
        stext = _scores_text(rows, qkey, q["options"], int(q.get("seats", 1)),
                             max_s, title=q["title"])
        counter = {"star": stv_tabulate.count_star,
                   "star_pr": stv_tabulate.count_star_pr}.get(method, stv_tabulate.count_score)
        r = counter(stext, cand_tags=ctags)
        note = {
            "star": "STAR (score then automatic runoff): the two highest-scoring "
                    "candidates meet in a head-to-head runoff decided by how many voters "
                    "scored each higher (multi-seat = Bloc STAR, majoritarian).",
            "star_pr": "STAR-PR (Allocated Score): proportional multi-winner STAR — a "
                       "cohesive minority earns its share of seats via Hare-quota ballot "
                       "spending.",
        }.get(method, "Score voting: candidates' 0–%d ratings are summed and the highest "
              "totals win." % max_s) + (" Preview only — the official method is set "
                                        "on the question.")
        return jsonify({"title": q["title"], "method": method, "note": note,
                        "seats": r["seats"], "winners": r["winners"],
                        "scores": r.get("scores", {}),
                        "official_method": q.get("method", "score")}), 200
    _allowed = stv_tabulate.ALT_METHODS + stv_tabulate.ADVANCED_ALT_METHODS
    if method not in _allowed:
        return jsonify({"error": "bad_method",
                        "message": f"method must be one of {_allowed}"}), 400
    blt = _blt_text(rows, qkey, q["options"], int(q.get("seats", 1)),
                    title=q["title"])
    res = stv_tabulate.count_alternative(blt, method)
    res["official_method"] = "Scottish STV"
    res["advanced"] = method in stv_tabulate.ADVANCED_ALT_METHODS
    return jsonify(res), 200


# Published results are FROZEN at publish time into {poll}__published/results
# (a JSON blob) and served from an in-process cache — a public results page
# hit is then one cached dict render, no Firestore streaming and no
# re-tabulation, so viral traffic costs ~nothing and can't slow voting.
_pub_cache = {}
PUB_CACHE_TTL = 60.0


def _store_published_results(poll_id: str, cfg: dict) -> dict:
    res = compute_results(poll_id, cfg)
    db.collection(f"{poll_id}__published").document("results").set({
        "json": json.dumps(res), "generated_at": int(time.time())})
    _pub_cache[poll_id] = (time.time(), res)
    return res


def _clear_published(poll_id: str):
    """Drop any FROZEN published results + in-process cache for a poll, so the
    next view/publish recomputes from the CURRENT config. Called whenever a
    poll's config changes — otherwise a page published early (e.g. before its
    real questions were loaded) would keep serving stale/demo content."""
    _pub_cache.pop(poll_id, None)
    try:
        db.collection(f"{poll_id}__published").document("results").delete()
    except Exception:
        pass


def _read_published_results(poll_id: str):
    hit = _pub_cache.get(poll_id)
    if hit and time.time() - hit[0] < PUB_CACHE_TTL:
        return hit[1]
    try:
        snap = db.collection(f"{poll_id}__published").document("results").get()
        if getattr(snap, "exists", False):
            res = json.loads((snap.to_dict() or {}).get("json") or "{}")
            if res:
                _pub_cache[poll_id] = (time.time(), res)
                return res
    except Exception:
        pass
    return None


@app.post("/admin/api/polls/<poll_id>/publish")
def admin_publish_results(poll_id):
    """Publish (or unpublish) the poll's PUBLIC results page at
    /p/<poll>/results. Decided policy: each chapter decides its own
    publication — so any admin of the poll may flip this, audited.
    Requires a finalized poll."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    if not cfg.get("finalized"):
        return jsonify({"error": "not_finalized",
                        "message": "Results publish after the poll is finalized."}), 409
    body = request.get_json(silent=True) or {}
    publish = bool(body.get("publish", True))
    if publish and not body.get("force"):
        # Publishing FREEZES the tally. Adjudicating a provisional afterwards
        # adds a real ballot that the frozen copy will never show, so a poll
        # with a live adjudication queue must not be certified by accident.
        try:
            pending = sum(1 for _ in db.collection(f"{poll_id}__provisional")
                          .where("status", "==", "pending").stream())
        except Exception:
            pending = 0
        if pending:
            return jsonify({"error": "provisionals_pending", "pending": pending,
                            "message": f"{pending} provisional ballot(s) are still "
                                       "awaiting adjudication. Publishing now freezes "
                                       "a tally that excludes them. Clear the queue "
                                       "first, or re-send with force:true."}), 409
    cfg = dict(_fresh_cfg_doc(poll_id) or cfg, results_published=publish)
    db.collection(CONFIG_COLL).document(poll_id).set(cfg_to_doc(cfg))
    if publish:
        _store_published_results(poll_id, cfg)   # freeze + cache for mass viewing
    _audit(poll_id, "results_publish" if publish else "results_unpublish", ident["name"])
    load_polls(force=True)
    return jsonify({"ok": True, "published": publish,
                    "url": f"/p/{poll_id}/results"}), 200


@app.get("/admin/api/polls/<poll_id>/blt/<qkey>")
def admin_export_blt(poll_id, qkey):
    """Download a contest as a standard .blt file for INDEPENDENT
    verification (OpaVote, OpenSTV/Droop, tools/stv_tabulate.py). Ballot
    weights are carried natively. Same gate as results: finalized, or
    national + ?live=1 (audited). ?recount=1 emits the alternates-recount
    header (seats+alternates) over the same ballots."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    if not cfg.get("finalized"):
        if not (request.args.get("live") and ident["role"] == "national"):
            return jsonify({"error": "not_finalized"}), 409
        _audit(poll_id, "live_blt_export", ident["name"], question=qkey)
    q = next((x for x in poll_questions(cfg) if x["key"] == qkey), None)
    if not q or q["type"] not in ("ranked", "yesno"):
        return jsonify({"error": "not_exportable",
                        "message": "only ranked and yesno questions export as BLT"}), 404
    # Decided policy: the secret-ballot collection (delegate rankings) is
    # national-only — chapters get their delegation *result*, never the raw
    # rankings. Gate the raw-ballot export so a chapter token can't pull the
    # anonymized-but-complete secret ballots.
    if q_visibility(q) == "secret" and ident["role"] != "national":
        return jsonify({"error": "forbidden_secret",
                        "message": "secret-ballot contests export only for national "
                                   "admins"}), 403
    main_rows, _, secret_rows = _tally_rows(poll_id)
    if q["type"] == "yesno":
        options = _yesno_options(dict(q, allow_abstain=False))
        rows = [(a, w) for a, w in main_rows
                if str(a.get(qkey, "")).upper() in ("YES", "NO")]
        seats = 1
        # yesno as single-choice rankings: {"key": ["YES"]}-shaped rows
        rows = [({qkey: [str(a.get(qkey)).upper()]}, w) for a, w in rows]
    else:
        options = q["options"]
        seats = int(q.get("seats", 1))
        if request.args.get("recount"):
            seats += int(q.get("alternates", 0))
        rows = secret_rows if q.get("secret") else main_rows
    withdrawn = [w.strip().upper() for w in
                 str(request.args.get("withdraw", "")).split(",") if w.strip()]
    blt = _blt_text(rows, qkey, options, seats,
                    title=f"{cfg.get('name', poll_id)} - {q['title']}"
                          + (" - ALTERNATES RECOUNT" if request.args.get("recount") else "")
                          + (f" - WITHDRAWN: {','.join(withdrawn)}" if withdrawn else ""),
                    withdrawn=withdrawn)
    fname = f"{poll_id}_{qkey}" + ("_alternates_recount" if request.args.get("recount") else "") + ".blt"
    return Response(blt, mimetype="text/plain",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


def _results_txt(res: dict) -> str:
    out = [f"RosaVote Election Results", "", res.get("name") or res["poll_id"], ""]
    out.append(f"Ballots counted: {res['ballots_counted']}"
               + ("  (WEIGHTED — ballots count at each voter's weight)" if res.get("weighted") else ""))
    out.append("")
    for q in res["questions"]:
        out.append("=" * 72)
        out.append(q["title"] + {"secret": "   [secret ballot]",
                                 "public": "   [recorded roll call — published by name]",
                                 }.get(q.get("visibility", ""), ""))
        if q["type"] == "yesno":
            c = q["counts"]
            out.append(f"  Yes {c['YES']} · No {c['NO']} · Abstain {c['ABSTAIN']}"
                       f"  ->  {q['result']} (abstentions excluded)")
        elif q["type"] == "ranked":
            out.append(f"  {q.get('method_used', 'Scottish STV')} · {q['seats']} seat(s) · "
                       f"{q['valid_ballots']} valid ballots · quota {q['quota']}")
            for st in q["stages"]:
                out.append(f"  Stage {st['stage']}: {st['action']}")
                for name, v in sorted(st["totals"].items(), key=lambda kv: -kv[1]):
                    mark = {"elected": "  ELECTED", "excluded": "  excluded"}.get(
                        st["status"].get(name, ""), "")
                    out.append(f"      {name:<40} {v:>12g}{mark}")
                out.append(f"      {'(non-transferable)':<40} {st['nontransferable']:>12g}")
            out.append(f"  WINNERS: {', '.join(q['winners'])}")
            if q.get("alternates"):
                _amlabel = ("replacement re-run"
                            if q.get("alternate_method") == "replacement"
                            else "expanded count")
                out.append(f"  ALTERNATES ({_amlabel}): {', '.join(q['alternates'])}")
            if q.get("quota_group"):
                out.append(f"  QUOTA GROUP: {q['quota_group']} (requirements span "
                           "every contest in the group; tallies are body-wide)")
            for cn in (q.get("constraints") or []):
                bound = f"max {cn['max']}" if "max" in cn else f"min {cn['min']}"
                okc = (cn["elected"] <= cn["max"]) if "max" in cn else (cn["elected"] >= cn["min"])
                out.append(f"  QUOTA: {cn.get('label') or bound + ' ' + cn['tag']} — "
                           f"{cn['elected']} elected [{'OK' if okc else 'VIOLATED'}]")
            if q.get("unconstrained"):
                u = q["unconstrained"]
                out.append(f"  WITHOUT quota requirements the winners would have been: "
                           f"{', '.join(u['winners'])}")
                if u.get("alternates"):
                    out.append(f"  ...and the alternates: {', '.join(u['alternates'])}")
        elif q["type"] == "multi":
            for name, v in q["counts"].items():
                out.append(f"      {name:<40} {v:>8}")
        elif q["type"] == "text":
            out.append(f"  {q['responses']} free-text response(s) — content stays sealed")
    out.append("")
    return "\n".join(out) + "\n"


def _results_csv(res: dict) -> str:
    rows = [f'"Election for","{res.get("name") or res["poll_id"]}"',
            f'"Ballots counted",{res["ballots_counted"]}',
            f'"Weighted","{bool(res.get("weighted"))}"', '"RosaVote",""']
    for q in res["questions"]:
        rows.append("")
        rows.append(f'"Contest","{q["title"]}"')
        if q["type"] == "ranked":
            rows.append(f'"Election rules","Scottish STV"')
            rows.append(f'"Seats",{q["seats"]},"Quota",{q["quota"]},"Valid votes",{q["valid_ballots"]}')
            cands = list(q["stages"][0]["totals"])
            rows.append('"Candidates",' + ",".join(f'"Stage {st["stage"]}"' for st in q["stages"]))
            for cand in cands:
                cells = []
                for st in q["stages"]:
                    v = st["totals"].get(cand)
                    cells.append("-" if v is None else f"{v:g}")
                elected = "Elected" if cand in q["winners"] else ""
                rows.append(f'"{cand}",' + ",".join(cells) + (f',"{elected}"' if elected else ""))
            rows.append('"Non-transferable",' +
                        ",".join(f'{st["nontransferable"]:g}' for st in q["stages"]))
        elif q["type"] in ("yesno", "multi"):
            for name, v in q["counts"].items():
                rows.append(f'"{name}",{v}')
            if q["type"] == "yesno":
                rows.append(f'"Result","{q["result"]}"')
        elif q["type"] == "text":
            rows.append(f'"Responses",{q["responses"]}')
    return "\n".join(rows) + "\n"


VERIFY_README = """HOW TO INDEPENDENTLY VERIFY THESE RESULTS
==========================================

1. The .blt files in this archive are the complete anonymous ballots for
   each contest, in the standard BLT format (OpaVote / OpenSTV compatible).
   Ballot weights ride in the first number of each ballot line.

2. Recount them in software DSA does not control:
     - upload a .blt to OpaVote (choose Scottish STV), or
     - run OpenSTV / Droop, or
     - run the tabulator shipped with this codebase:
         python3 tools/stv_tabulate.py <contest>.blt
   The round-by-round table must match results.txt exactly.

3. Delegate alternates use the TWO-COUNT method: the *_alternates_recount.blt
   file is the same ballots with seats set to delegates+alternates. Winners
   there who are not already delegates are the alternates, in order of
   election.

4. results.json is the machine-readable equivalent (for scripts, archives,
   and future audits). ballots.csv lists each stored ballot record with its
   receipt and record hash; voters can confirm their own receipt at
   /p/<poll>/verify?receipt=... without revealing any content.

5. After certification, the close-out export also publishes a tamper-evident
   hash chain over every ballot record (tools/build_chain.py, checked by
   tools/verify.py).

6. QUOTA-CONSTRAINED contests (leadership diversity requirements): plain
   OpaVote/OpenSTV cannot apply the constraints — recount those with the
   shipped tabulator, passing the candidate tags and constraints recorded in
   results.json, or verify by inspection: the stage log names every
   quota-driven exclusion/guard, and the unconstrained recount of the same
   .blt shows exactly how the quota changed the outcome.
"""


@app.get("/admin/api/polls/<poll_id>/export.zip")
def admin_export_zip(poll_id):
    """One-click results package: results.txt (round-by-round narrative),
    results.csv (stage matrix), results.json (machine-readable), a printable
    results.html, every contest's .blt (+ alternates recounts), ballots.csv,
    and verification instructions. ?anonymize=1 strips member identity from
    ballots.csv (receipts + hashes stay, so self-verification still works).
    Same gate as results: finalized, or national + ?live=1 (audited)."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    if not cfg.get("finalized"):
        if not (request.args.get("live") and ident["role"] == "national"):
            return jsonify({"error": "not_finalized"}), 409
        _audit(poll_id, "live_export_zip", ident["name"])
    anonymize = str(request.args.get("anonymize", "")).lower() in ("1", "true", "yes")
    res = compute_results(poll_id, cfg)
    main_rows, _, secret_rows = _tally_rows(poll_id)

    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("results.txt", _results_txt(res))
        z.writestr("results.csv", _results_csv(res))
        z.writestr("results.json", json.dumps(res, indent=2))
        z.writestr("VERIFY_README.txt", VERIFY_README)
        # printable report (open in a browser, print to PDF)
        body = "<pre style='font: 13px/1.45 ui-monospace,Menlo,monospace'>" \
               + _esc(_results_txt(res)) + "</pre>"
        z.writestr("results.html",
                   f"<!doctype html><meta charset='utf-8'><title>{_esc(res.get('name') or poll_id)}"
                   f" — results</title>{body}")
        for q in poll_questions(cfg):
            if q["type"] not in ("ranked", "yesno"):
                continue
            # secret-ballot raw rankings are national-only (decided policy);
            # a chapter's package omits them (it still gets its result set).
            # q_visibility (not the derived `secret` flag) so a config written
            # straight to Firestore with visibility:"secret" can't slip past.
            if q_visibility(q) == "secret" and ident["role"] != "national":
                continue
            key = q["key"]
            if q["type"] == "yesno":
                options = _yesno_options(dict(q, allow_abstain=False))
                rows = [({key: [str(a.get(key)).upper()]}, w) for a, w in main_rows
                        if str(a.get(key, "")).upper() in ("YES", "NO")]
                seats = 1
            else:
                options = q["options"]
                seats = int(q.get("seats", 1))
                rows = secret_rows if q.get("secret") else main_rows
            title = f"{cfg.get('name', poll_id)} - {q['title']}"
            z.writestr(f"{key}.blt", _blt_text(rows, key, options, seats, title=title))
            alts = int(q.get("alternates", 0)) if q["type"] == "ranked" else 0
            if alts:
                z.writestr(f"{key}_alternates_recount.blt",
                           _blt_text(rows, key, options, seats + alts,
                                     title=title + " - ALTERNATES RECOUNT"))
        # ballot records (never includes secret-question content)
        lines = ["receipt,record_hash,voided,provisional,weight"
                 + ("" if anonymize else ",member_id,chapter")]
        weights = _code_weights(poll_id)
        for snap in db.collection(f"{poll_id}__ballots").stream():
            d = snap.to_dict() or {}
            row = [d.get("receipt", ""), d.get("record_hash", ""),
                   str(bool(d.get("voided"))), str(bool(d.get("provisional"))),
                   str(_ballot_weight(d, weights))]
            if not anonymize:
                # member_id/chapter come from an uploaded roll — untrusted.
                # Unquoted, a comma shifts every later column (the election
                # record silently misaligns); a leading =/+/-/@ executes when
                # staff open the package in Excel or Sheets. Same treatment
                # _manifest_csv and _roll_call_csv already apply.
                row += [_csv_cell(d.get("member_id")), _csv_cell(d.get("chapter"))]
            lines.append(",".join(row))
        z.writestr("ballots.csv", "\n".join(lines) + "\n")
        # by-name roll call for every question this body publishes by name
        for q in poll_questions(cfg):
            if q_visibility(q) == "public" and q["type"] != "text":
                z.writestr(f"rollcall_{q['key']}.csv",
                           _roll_call_csv(roll_call_rows(poll_id, q)))
    buf.seek(0)
    _audit(poll_id, "export_zip", ident["name"], anonymize=anonymize)
    return Response(buf.read(), mimetype="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename={poll_id}_results.zip"})


@app.post("/admin/api/polls/<poll_id>/voters/weight")
def admin_set_weight(poll_id):
    """Set a voter's ballot weight (integer 1–1000) — national admins only,
    audited. Weights live on the code docs and are resolved at tally time,
    so provisioning before open, or adjusting during/after the election,
    always flows into the count (and the exported BLTs)."""
    ident = require_admin(poll_id, national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    if not chapter_or_none(poll_id):
        return jsonify({"error": "unknown_poll"}), 404
    data = request.get_json(silent=True) or {}
    member_id = str(data.get("member_id") or "").strip()
    try:
        weight = int(data.get("weight"))
    except (TypeError, ValueError):
        weight = 0
    if not member_id or not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        return jsonify({"error": "bad_request",
                        "message": f"member_id and integer weight "
                                   f"{WEIGHT_MIN}–{WEIGHT_MAX} required"}), 400
    matched = 0
    for snap in db.collection(f"{poll_id}__codes").where("member_id", "==", member_id).stream():
        snap.reference.update({"weight": weight})
        matched += 1
    if not matched:
        return jsonify({"error": "member_not_found"}), 404
    _audit(poll_id, "weight_set", ident["name"], member_id=member_id,
           weight=weight, codes_updated=matched)
    return jsonify({"ok": True, "member_id": member_id, "weight": weight}), 200


# ---- admin: voters (turnout status — never how anyone voted) --------------
VOTERS_LIST_CAP = 1000
IMPORT_CAP = 20000
SERVER_IMPORT_CAP = 200000


@app.get("/admin/api/polls/<poll_id>/voters")
def admin_list_voters(poll_id):
    """Per-voter turnout: did each member's code get used, and did a ballot
    record actually land for it (received-verification). NEVER exposes answer
    content — only status, receipt, and void state. Includes an integrity
    counter: used codes with no matching ballot record should always be 0."""
    ident = require_admin(poll_id)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    if not chapter_or_none(poll_id):
        return jsonify({"error": "unknown_poll"}), 404
    member_q = str(request.args.get("member_id", "")).strip()

    ballots_by_code = {}
    n_ballots = n_voided = 0
    for snap in db.collection(f"{poll_id}__ballots").stream():
        d = snap.to_dict() or {}
        if d.get("voided"):
            n_voided += 1
        else:
            n_ballots += 1
        if d.get("code_hash"):
            ballots_by_code[d["code_hash"]] = {"receipt": d.get("receipt"),
                                               "voided": bool(d.get("voided"))}
    voters, n_codes, n_used, n_missing = [], 0, 0, 0
    for snap in db.collection(f"{poll_id}__codes").stream():
        d = snap.to_dict() or {}
        n_codes += 1
        used = bool(d.get("used"))
        if used:
            n_used += 1
        ballot = ballots_by_code.get(snap.id)
        if used and not ballot and d.get("burned_by") != "provisional_adjudication":
            n_missing += 1
        if member_q and str(d.get("member_id", "")) != member_q:
            continue
        if len(voters) < VOTERS_LIST_CAP:
            try:
                wv = max(WEIGHT_MIN, min(WEIGHT_MAX, int(d.get("weight") or 1)))
            except (TypeError, ValueError):
                wv = 1
            voters.append({"member_id": d.get("member_id"),
                           "chapter": d.get("chapter"),
                           "voted": used,
                           "weight": wv,
                           "ballot_received": bool(ballot),
                           "receipt": (ballot or {}).get("receipt"),
                           "voided": (ballot or {}).get("voided", False),
                           "reissued": bool(d.get("reissued_from")),
                           "burned_by": d.get("burned_by")})
    voters.sort(key=lambda v: (not v["voted"], str(v["member_id"])))
    return jsonify({
        "summary": {"codes": n_codes, "voted": n_used, "ballots": n_ballots,
                    "voided": n_voided},
        "integrity": {"used_codes_without_ballot": n_missing},
        "truncated": n_codes > VOTERS_LIST_CAP and not member_q,
        "voters": voters}), 200


@app.post("/admin/api/polls/<poll_id>/voters/import")
def admin_import_voters(poll_id):
    """Import a chapter's voter roll (national admins only): mints one code
    per NEW member, stores only the hash, and returns the plaintext manifest
    exactly once — the console downloads it for the send platform; it is
    never stored server-side. Members who already hold a code are skipped."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    data = request.get_json(silent=True) or {}
    members = data.get("members") or []
    if not isinstance(members, list) or not members:
        return jsonify({"error": "bad_request", "message": "members list required"}), 400
    if len(members) > IMPORT_CAP:
        return jsonify({"error": "too_many",
                        "message": f"cap is {IMPORT_CAP} members per import"}), 400

    created, skipped, bad = _import_members(poll_id, cfg, members, ident, "console_csv")
    return jsonify({"created": created, "skipped": skipped, "bad": bad}), 200


BATCH_SIZE = 400          # Firestore hard-caps a WriteBatch at 500 operations


def _import_members(poll_id, cfg, members, ident, source):
    """Mint one hashed code doc per NEW member. Returns (created_manifest,
    skipped, bad); plaintext codes exist only in the returned manifest.

    Writes go out in BATCHES: one round trip per document put a 20k-member
    console import (never mind the 200k server-side cap) far outside the Cloud
    Run request deadline, and a timeout mid-import strands a partial roll with
    no manifest for the codes already minted."""
    codes = db.collection(f"{poll_id}__codes")
    existing = set()
    for snap in codes.stream():
        mid = (snap.to_dict() or {}).get("member_id")
        if mid:
            existing.add(str(mid))
    created, skipped, bad = [], 0, 0
    base = request.url_root.rstrip("/")
    pending = []

    def _flush():
        if not pending:
            return
        batch = db.batch()
        for ref, doc in pending:
            batch.set(ref, doc)
        batch.commit()
        pending.clear()

    for m in members:
        mid = str((m.get("member_id") if isinstance(m, dict) else m) or "").strip()
        chapter = str((m.get("chapter") if isinstance(m, dict) else "") or "").strip() \
            or cfg.get("name", "")
        try:
            weight = int((m.get("weight") if isinstance(m, dict) else 1) or 1)
        except (TypeError, ValueError):
            weight = 0
        if not mid or not WEIGHT_MIN <= weight <= WEIGHT_MAX:
            bad += 1
            continue
        if mid in existing:
            skipped += 1
            continue
        existing.add(mid)
        code = secrets.token_urlsafe(12)
        doc = {"used": False, "member_id": mid, "chapter": chapter}
        if weight != 1:
            doc["weight"] = weight
        pending.append((codes.document(code_hash(code)), doc))
        if len(pending) >= BATCH_SIZE:
            _flush()
        created.append({"member_id": mid, "chapter": chapter, "weight": weight,
                        "code": code, "vote_link": f"{base}/p/{poll_id}/v/{code}"})
    _flush()
    _audit(poll_id, "voters_import", ident["name"], source=source,
           created=len(created), skipped=skipped, bad=bad)
    return created, skipped, bad


def _csv_cell(v) -> str:
    """One CSV field, always quoted, and never a spreadsheet formula.

    member_id and chapter come from an uploaded roll, so they are untrusted:
    an unquoted value containing a comma silently shifts every later column
    (a code lands in the wrong member's row), and a leading =/+/-/@ makes
    Excel or Sheets execute the cell when staff open the manifest."""
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return '"' + s.replace('"', '""') + '"'


def _manifest_csv(created) -> str:
    lines = ["member_id,chapter,weight,code,vote_link"]
    for c in created:
        lines.append(",".join(_csv_cell(x) for x in
                              (c["member_id"], c["chapter"], c.get("weight", 1),
                               c["code"], c["vote_link"])))
    return "\n".join(lines) + "\n"


def _parse_gcs_uri(uri):
    m = re.match(r"^gs://([^/]+)/(.+)$", str(uri or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def _write_manifest_gcs(bucket_name, path, created):
    from google.cloud import storage
    blob = storage.Client().bucket(bucket_name).blob(path)
    blob.upload_from_string(_manifest_csv(created), content_type="text/csv")
    return f"gs://{bucket_name}/{path}"


@app.post("/admin/api/polls/<poll_id>/voters/import_gcs")
def admin_import_voters_gcs(poll_id):
    """Import a roll from a CSV in Cloud Storage (national admins only).
    Required header: member_id. Optional: chapter. Every other column is
    ignored. The code manifest is written BACK to the same bucket
    (<name>_code_manifest_<rand>.csv) — plaintext codes never transit the
    console; lock the bucket down accordingly."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    bucket, path = _parse_gcs_uri((request.get_json(silent=True) or {}).get("gcs_uri"))
    if not bucket:
        return jsonify({"error": "bad_request", "message": "gcs_uri must be gs://bucket/file.csv"}), 400
    import csv as _csv
    import io
    try:
        from google.cloud import storage
        text = storage.Client().bucket(bucket).blob(path).download_as_text()
    except Exception as e:
        return jsonify({"error": "gcs_read_failed", "message": type(e).__name__}), 502
    reader = _csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    if "member_id" not in headers:
        return jsonify({"error": "bad_csv",
                        "message": "required header member_id missing "
                                   f"(found: {sorted(headers)})"}), 400
    members = []
    for row in reader:
        members.append({"member_id": (row.get(headers["member_id"]) or "").strip(),
                        "chapter": (row.get(headers.get("chapter", ""), "") or "").strip(),
                        "weight": (row.get(headers.get("weight", ""), "") or "").strip() or 1})
        if len(members) > SERVER_IMPORT_CAP:
            return jsonify({"error": "too_many",
                            "message": f"cap is {SERVER_IMPORT_CAP} rows"}), 400
    created, skipped, bad = _import_members(poll_id, cfg, members, ident, "gcs_csv")
    manifest_uri = None
    if created:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        dest = f"{stem}_code_manifest_{secrets.token_hex(4)}.csv"
        try:
            manifest_uri = _write_manifest_gcs(bucket, dest, created)
        except Exception as e:
            return jsonify({"error": "manifest_write_failed", "message": type(e).__name__,
                            "note": "codes were minted; re-void or contact tech"}), 502
    return jsonify({"created_count": len(created), "skipped": skipped, "bad": bad,
                    "manifest_gcs": manifest_uri}), 200


# The roll query is warehouse-specific. Supply your own via the
# ROLL_IMPORT_QUERY env var; contract: SELECT one STRING column `member_id`,
# may use the @chapter parameter, and `{roll}` expands to "<project>.<dataset>".
# The built-in default is a neutral example schema.
# GCP project ids and BigQuery dataset ids: letters, digits, - and _, plus the
# single ':' legacy domain-scoped project ids use. Deliberately excludes the
# backtick, quote, whitespace, parenthesis and semicolon characters that would
# let a caller-supplied value escape the interpolated table reference below.
GCP_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}(:[A-Za-z0-9][A-Za-z0-9_-]{0,62})?$")

ROLL_IMPORT_QUERY = os.environ.get("ROLL_IMPORT_QUERY", """
SELECT CAST(member_id AS STRING) AS member_id
FROM `{roll}.members`
WHERE status = 'active' AND chapter = @chapter
""")


@app.post("/admin/api/polls/<poll_id>/voters/import_bigquery")
def admin_import_voters_bigquery(poll_id):
    """Import from a membership data warehouse (national admins only). The
    eligibility query is operator-configured (ROLL_IMPORT_QUERY env); rows
    whose roll chapter matches are minted codes.
    Manifest: inline for small rolls, or written to manifest_gcs
    (gs://bucket/file.csv) — required above the inline cap."""
    ident = require_admin(national_only=True)
    if not ident:
        return jsonify({"error": "forbidden"}), 403
    cfg = chapter_or_none(poll_id)
    if not cfg:
        return jsonify({"error": "unknown_poll"}), 404
    data = request.get_json(silent=True) or {}
    chapter = str(data.get("chapter") or cfg.get("roll_chapter") or cfg.get("name") or "").strip()
    if not chapter:
        return jsonify({"error": "bad_request", "message": "chapter required"}), 400
    project = str(data.get("roll_project") or os.environ.get("ROLL_PROJECT", "")).strip()
    dataset = str(data.get("roll_dataset") or os.environ.get("ROLL_DATASET", "")).strip()
    if not project or not dataset:
        return jsonify({"error": "bad_request",
                        "message": "roll_project and roll_dataset required "
                                   "(or ROLL_PROJECT/ROLL_DATASET env)"}), 400
    job_project = str(data.get("job_project") or "").strip() or None
    # The `chapter` filter is a bound query parameter, but the TABLE REFERENCE
    # is string-interpolated into the SQL — so an unvalidated project/dataset
    # breaks out of the backticks and injects arbitrary SQL, executed with this
    # service's BigQuery credentials (e.g. `x`.t` UNION ALL SELECT email FROM
    # `hr.people` --` returns whatever the caller asks for as "member_id").
    # Constrain both to the identifier charsets GCP actually allows, so nothing
    # that could terminate the quoting ever reaches the query string.
    for _label, _val in (("roll_project", project), ("roll_dataset", dataset),
                         ("job_project", job_project)):
        if _val and not GCP_IDENT_RE.match(_val):
            return jsonify({"error": "bad_request",
                            "message": f"{_label} must match "
                                       f"{GCP_IDENT_RE.pattern} (letters, digits, "
                                       "-, _, and at most one : for legacy "
                                       "domain-scoped project ids)"}), 400
    try:
        from google.cloud import bigquery
        # jobs bill/run in this service's project unless job_project overrides;
        # the roll is read cross-project
        client = bigquery.Client(project=job_project)
        job = client.query(
            ROLL_IMPORT_QUERY.format(roll=f"{project}.{dataset}"),
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("chapter", "STRING", chapter)]))
        members = [{"member_id": str(row["member_id"]), "chapter": chapter}
                   for row in job.result()]
    except Exception as e:
        return jsonify({"error": "bigquery_failed", "message": type(e).__name__,
                        "note": "the service account needs BigQuery read on the "
                                "roll project"}), 502
    if not members:
        return jsonify({"error": "empty_roll",
                        "message": f"no eligible members with roll chapter {chapter!r}"}), 404
    if len(members) > SERVER_IMPORT_CAP:
        return jsonify({"error": "too_many", "message": f"cap is {SERVER_IMPORT_CAP}"}), 400
    mb, mp = _parse_gcs_uri(data.get("manifest_gcs"))
    if len(members) > IMPORT_CAP and not mb:
        # refuse BEFORE minting — a big manifest with nowhere to land would
        # strand plaintext codes
        return jsonify({"error": "manifest_gcs_required",
                        "message": "pass manifest_gcs (gs://bucket/file.csv) for rolls "
                                   f"over {IMPORT_CAP}"}), 400
    created, skipped, bad = _import_members(poll_id, cfg, members, ident, "bigquery")
    if mb:
        try:
            uri = _write_manifest_gcs(mb, mp, created)
        except Exception as e:
            return jsonify({"error": "manifest_write_failed", "message": type(e).__name__,
                            "note": "codes were minted; re-void or contact tech"}), 502
        return jsonify({"created_count": len(created), "skipped": skipped,
                        "bad": bad, "manifest_gcs": uri}), 200
    return jsonify({"created": created, "skipped": skipped, "bad": bad}), 200


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
    cfg = chapter_or_none(poll_id)
    if not cfg:
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

    # promote the sealed answers into the real collections — same
    # identity-linked / secret split as a coded vote.
    answers = prov.get("answers") or {}
    comments = prov.get("comments") or ({"text": prov["comment"]} if prov.get("comment") else {})
    try:
        pw = max(WEIGHT_MIN, min(WEIGHT_MAX, int(data.get("weight") or 1)))
    except (TypeError, ValueError):
        pw = 1
    try:
        _promote_txn(db.transaction(), poll_id, cfg, ref, receipt, answers,
                     comments, member_id, prov.get("chapter"), pw, ident["name"])
    except _AlreadyAdjudicated:
        return jsonify({"error": "already_adjudicated"}), 409
    _audit(poll_id, "provisional_verify", ident["name"], receipt=receipt, member_id=member_id)
    return jsonify({"ok": True, "status": "verified", "receipt": receipt}), 200


class _AlreadyAdjudicated(Exception):
    """The provisional left `pending` between our check and our write."""


@firestore.transactional
def _promote_txn(txn, poll_id, cfg, ref, receipt, answers, comments,
                 member_id, chapter, weight, admin):
    """Flip the provisional to `verified` and write its ballot record(s) in
    ONE transaction, re-checking `pending` inside it.

    Two adjudicators clicking Verify at the same moment both pass an
    unsynchronized status check and both promote — the member ends up with two
    counted ballots. Re-reading the status under the transaction makes the
    second click lose."""
    snap = ref.get(transaction=txn)
    if (snap.to_dict() or {}).get("status") != "pending":
        raise _AlreadyAdjudicated()
    txn.update(ref, {"status": "verified", "member_id": member_id,
                     "adjudicated_by": admin,
                     "adjudicated_at": firestore.SERVER_TIMESTAMP})
    _write_ballot_docs(poll_id, cfg, receipt, answers, comments,
                       {"member_id": member_id, "chapter": chapter,
                        "provisional": True}, None, weight=weight, txn=txn)


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
    if len(matches) > 1:
        # Legacy 32-bit receipts could collide. Voiding "the first match"
        # would cancel a ballot at random — refuse and make a human look.
        return jsonify({"error": "ambiguous_receipt", "matches": len(matches),
                        "message": "More than one ballot carries this receipt. "
                                   "Resolve by member_id (Ballot lookup) before "
                                   "voiding."}), 409
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

    # 2. reissue: fresh code for the same member (old code stays burned).
    # The voter's ballot WEIGHT lives on the code doc, so it has to ride
    # across — a reissued code that silently reverts a weighted delegate to
    # weight 1 would change the outcome of the election it was meant to fix.
    old_hash = md.get("code_hash")
    carried = {}
    if old_hash:
        try:
            osnap = db.collection(f"{poll_id}__codes").document(old_hash).get()
            ow = (osnap.to_dict() or {}).get("weight") if getattr(osnap, "exists", False) else None
            if ow is not None:
                carried["weight"] = max(WEIGHT_MIN, min(WEIGHT_MAX, int(ow)))
        except (TypeError, ValueError, AttributeError):
            pass
    new_code = secrets.token_urlsafe(12)
    new_hash = code_hash(new_code)
    db.collection(f"{poll_id}__codes").document(new_hash).set({
        "used": False,
        "member_id": md.get("member_id"),
        "chapter": md.get("chapter"),
        "reissued_from": md.get("code_hash"),
        **carried,
    })

    # 3. append-only audit record
    db.collection(f"{poll_id}__audit_log").document(secrets.token_hex(16)).set({
        "action": "void_reissue", "receipt": receipt, "reason": reason,
        "admin": admin, "old_code_hash": md.get("code_hash"),
        "new_code_hash": new_hash, "at": firestore.SERVER_TIMESTAMP,
    })
    # plaintext code returned ONCE for delivery to the voter; only its hash is stored
    return jsonify({"ok": True, "receipt_voided": receipt, "new_code": new_code}), 200


_LEGAL_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>{title} — RosaVote</title><link rel="icon" href="/logo.svg" type="image/svg+xml"/>
<style>body{{font:16px/1.55 Georgia,serif;background:#fff5e5;color:#111;margin:0}}
main{{max-width:640px;margin:0 auto;padding:24px 18px 60px}}
h1{{font-family:"Arial Narrow",sans-serif;text-transform:uppercase}}h2{{font-size:1.05rem}}
.banner{{background:#dd1111;color:#fff5e5;padding:12px 18px;font-family:"Arial Narrow",sans-serif;
font-weight:bold;text-transform:uppercase}} a{{color:#dd1111}}</style></head><body>
<div class="banner"><img src="/logo.svg" alt="" style="height:26px;vertical-align:-7px;margin-right:6px"/>RosaVote</div><main><h1>{title}</h1>
{body}<p><a href="/">&larr; Back to RosaVote</a></p>
<p style="font-size:.8rem;color:#666;margin-top:24px">RosaVote is free/libre software under
<a href="https://www.gnu.org/licenses/agpl-3.0.html">AGPL-3.0</a> &middot;
<a href="__SOURCE_URL__">source code</a> &middot; &copy; 2026 Walker Green</p></main></body></html>"""
# bake the AGPL §13 source-offer URL in once (leaves {title}/{body} for .format)
_LEGAL_SHELL = _LEGAL_SHELL.replace("__SOURCE_URL__", SOURCE_URL)

TERMS_BODY = """
<h2>What this service is</h2>
<p>RosaVote is open-source ranked-choice and STV election software
(AGPL-3.0, © 2026 Walker Green). This deployment is operated by RosaVote
as an independent service for organizations that run their votes here:
ballots, voter codes, tabulation, and results. Using a voting code, casting
a ballot, or administering an election here means you accept these terms.</p>
<h2>Acceptable use</h2>
<p>One member, one ballot (at the member's assigned voting weight). You may not
use a voting code that was not issued to you, attempt to vote more than once,
probe or disrupt the service, or attempt to access another member's ballot or
an administrative surface you were not granted. Test codes and the demo
sandbox exist for experimentation — use those.</p>
<h2>Votes are final</h2>
<p>Cast ballots cannot be edited or revoted. The only remedy is an
administrator's audited void-and-reissue under pre-committed reasons. Election
schedules, eligibility, and rules are set by the body running the election —
this service enforces them but does not decide them.</p>
<h2>No warranty</h2>
<p>The service is provided as-is, without warranty of any kind. Nothing here is
a guarantee of uninterrupted availability. Results are official only when
certified by the body conducting the election.</p>
<h2>Changes</h2>
<p>These terms may change; material changes will be posted on this page with
the service's version history.</p>
"""

PRIVACY_BODY = """
<p><b>RosaVote runs elections without keeping a file on you.</b> This page
says what the system stores, what it never stores, and who touches data on
the way through. It's short because the honest answer is short.</p>
<p><i>Effective August 4, 2026. Contact: support@rosavote.org.</i></p>
<h2>Who does what</h2>
<p>RosaVote is an independent open-source project (AGPL-3.0) operated by
Walker Green. It is not affiliated with DSA or any organization that votes
here. The organization running an election supplies the voter roll and owns
it. RosaVote hosts and processes that roll for one purpose: running the
election. If an organization runs its own copy of the software instead, that
organization is the data controller for its deployment.</p>
<h2>What we store</h2>
<p>Less than you'd guess. A one-time voting code for each eligible voter,
stored only as a SHA-256 hash: we can check a code, we can't recover one.
Attached to that hash: a member ID number, a chapter, and a vote weight.
That's the whole voter record.</p>
<p>Ballots depend on the section type, and the ballot itself discloses which
is which above Question 1. Secret-ballot questions (such as convention
delegates) are stored with no name and no chapter. Named sections store your
answers with your member ID and chapter, exactly as disclosed. Provisional
ballots additionally hold the contact details you provide, sealed until
adjudication. Every administrative action lands in an append-only audit log
with the admin's name.</p>
<h2>What we don't store</h2>
<p>No voter names, emails, phone numbers, or addresses in the voter records
(the one exception is sealed provisional contact info, above). When an
organization sends ballot links by email or text, the contact info passes
through at send time and is not kept. Those sends go through Mailgun, Twilio,
or Scale to Win, depending on the organization's setup. Each has its own
privacy policy worth reading.</p>
<h2>Who can see what</h2>
<p>Named answers: election administrators and your own chapter's admins.
Secret-ballot rankings: no one by name. Administrators can trace a specific
record only for troubleshooting, under audit. Each organization decides its
own publication, and secret-ballot rankings are never published by name.</p>
<h2>Ballot secrecy and verification</h2>
<p>Your receipt code confirms your ballot was stored
(<code>/p/&lt;poll&gt;/verify</code>) without revealing its content. Anyone
can download the anonymized ballots and re-run the tally. That's the point of
the project: you shouldn't have to trust us, and the data model is built so
you don't have to.</p>
<h2>Where it runs</h2>
<p>Google Cloud, in the United States, in RosaVote's own project. Like every
web server, ours writes standard request logs (IP address, browser type,
timestamps). Backups are point-in-time recovery with a 7-day window; nothing
lives in backups longer than that.</p>
<h2>Selling and sharing</h2>
<p>We don't sell data. We don't share it. No ads, no tracking pixels, no
third-party analytics.</p>
<h2>Retention and deletion</h2>
<p>Ballot records are kept as the organization's permanent election record,
and voided ballots are flagged rather than silently deleted. When the
organization deletes an election, or asks us to, its data goes with it;
backups age out within 7 days. If you're a voter with a question about your
data, start with the organization that ran your election, since they own the
roll. You can also write support@rosavote.org.</p>
<h2>The demo</h2>
<p>The demo deployment uses publicly available data. Sample ballots use
historical figures and published convention materials, and living people's
surnames are abbreviated.</p>
"""


LOGO_SVG = """<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <rect x="9" y="9" width="50" height="50" fill="#dd1111"/>
  <rect x="4" y="4" width="50" height="50" fill="#fff5e5" stroke="#000" stroke-width="2"/>
  <circle cx="29" cy="16.5" r="8" fill="#dd1111" stroke="#000" stroke-width="2.2"/>
  <path d="M29 12 c 2.7 0 4.5 1.8 4.5 4 0 2.4-2 4.2-4.5 4.2 s-4.5-1.8-4.5-4.2" fill="none" stroke="#fff5e5" stroke-width="1.8" stroke-linecap="round"/>
  <path d="M29 14.8 c 1.1 0 1.9.7 1.9 1.7" fill="none" stroke="#fff5e5" stroke-width="1.4" stroke-linecap="round"/>
  <path d="M29 24.5 V 38.5" fill="none" stroke="#000" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M28 30.5 C 25 30 22.8 28.3 22 25.7 C 25.2 26.1 27.4 27.8 28 30.5 z" fill="#dd1111" stroke="#000" stroke-width="1.6"/>
  <path d="M30 34.5 C 33 34 35.2 32.3 36 29.7 C 32.8 30.1 30.6 31.8 30 34.5 z" fill="#dd1111" stroke="#000" stroke-width="1.6"/>
  <rect x="20" y="37.5" width="18" height="4" fill="#000"/>
  <rect x="14" y="39" width="30" height="11.5" fill="#dd1111" stroke="#000" stroke-width="2.2"/>
  <path d="M18.5 45 h 21" stroke="#fff5e5" stroke-width="2" stroke-linecap="round"/>
</svg>"""


@app.get("/logo.svg")
def logo():
    return Response(LOGO_SVG, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


API_GROUPS = [
    ("Public — voter", [
        ("GET", "/p/&lt;poll&gt;/", "None", "The branded ballot page for a poll."),
        ("POST", "/p/&lt;poll&gt;/resend", "None", "Self-serve 'lost my code'. Body {contact}. Enumeration-safe (always {status:ok}), rate-limited 15 min/contact; re-sends the member's link to their on-record contacts only."),
        ("GET", "/prefs", "None", "Self-serve notification preferences page (opt a contact out / back in)."),
        ("POST", "/prefs/optout", "None", "Suppress election contact to an email/phone. Body {contact}. Enumeration-safe."),
        ("POST", "/prefs/optin", "None", "Re-enable election contact to an email/phone. Body {contact}."),
        ("GET", "/p/&lt;poll&gt;/v/&lt;code&gt;", "None", "Ballot page with a one-tap voting code embedded."),
        ("GET", "/p/&lt;poll&gt;/voted?code=…", "None", "Has this code already voted? → {voted: bool}. Malformed/unknown codes are rejected before any DB read."),
        ("POST", "/p/&lt;poll&gt;/vote", "Voting code (in body)", "Cast a ballot: {code, answers:{questionKey:value}}. One code = one vote, enforced atomically. → {status:'recorded', receipt}."),
        ("POST", "/p/&lt;poll&gt;/provisional", "None", "Cast a sealed provisional ballot with identifying info for later adjudication."),
        ("GET", "/p/&lt;poll&gt;/verify?receipt=…", "None", "Confirm a ballot was stored → {found, status}. No identity, no answers — safe to share."),
        ("GET", "/p/&lt;poll&gt;/results", "None", "Public results page (only after finalize + publish). Aggregates, plus a by-name roll call for any question the poll declares visibility:public."),
        ("GET", "/p/&lt;poll&gt;/rollcall/&lt;question&gt;.csv", "None", "Recorded roll call — member_id, chapter, and how they voted — for a visibility:public question. Named and secret questions are never served here. Requires finalize + publish."),
        ("GET", "/p/&lt;poll&gt;/verify/&lt;file&gt;", "None", "Verification artifacts after finalize + publish: ballots.csv, used_codes.csv, chain_head.txt, and &lt;question&gt;.blt for each secret ranked contest (anonymous rankings + weights, no identity — so a secret election is still publicly recountable)."),
    ]),
    ("Admin — auth", [
        ("GET", "/admin/api/whoami", "X-Admin-Token", "Resolve the caller's identity → {name, role, polls}."),
        ("POST", "/admin/api/admins", "National", "Mint a scoped admin token (plaintext returned once). Body: {name, role, polls, token?}."),
    ]),
    ("Admin — elections", [
        ("GET", "/admin/api/polls", "Any admin", "List polls visible to this token (chapter tokens see only their own)."),
        ("POST", "/admin/api/polls/&lt;poll&gt;", "National", "Create or update a poll config (the whole ballot schema). unfinalize:true to reopen a certified poll (audited)."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/open", "Poll admin", "Open voting now (with or without a schedule)."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/close", "Poll admin", "Close voting now."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/archive", "National", "Archive a poll: hide it from voting/public and the normal list (ballots kept, reversible)."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/unarchive", "National", "Restore an archived poll to the active set."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/publish", "Poll admin", "Publish/unpublish the public results page (finalized polls). Body: {publish:bool}."),
        ("POST", "/admin/api/cron/closeout", "National", "Finalize every poll past its window. Called by Cloud Scheduler every 15 min; idempotent."),
    ]),
    ("Admin — results &amp; tabulation", [
        ("GET", "/admin/api/polls/&lt;poll&gt;/results", "Poll admin", "Full tallies (finalized, or national + ?live=1, audited). ?fresh=1 recomputes past the cache."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/recount_preview", "Poll admin", "Preview a ranked contest under another method (plurality|approval|borda|irv|mntv|meek), or a score contest as score|star|star_pr. Body: {question, method}."),
        ("GET", "/admin/api/polls/&lt;poll&gt;/blt/&lt;qkey&gt;", "Poll admin", "Download a contest as a standard .blt. ?recount=1 alternates; ?withdraw=IDS dropout recount."),
        ("GET", "/admin/api/polls/&lt;poll&gt;/export.zip", "Poll admin", "Results package: .txt/.csv/.json reports, printable .html, every .blt, ballots.csv, verify readme. ?anonymize=1."),
        ("POST", "/admin/api/count_blt", "Any admin", "Run Scottish/Meek STV on a posted .blt (with optional constraints). Touches no stored data."),
    ]),
    ("Admin — voters", [
        ("GET", "/admin/api/polls/&lt;poll&gt;/voters", "Poll admin", "Per-voter turnout + received-verification + integrity counter. Never how anyone voted. ?member_id=… to search."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/voters/import", "National", "Import a roll (JSON members[]). Mints hashed codes, returns the plaintext manifest once."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/voters/import_gcs", "National", "Import a roll CSV from gs://bucket/file.csv (header member_id; optional chapter, weight)."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/voters/import_bigquery", "National", "Import eligible members by roll chapter straight from the warehouse."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/voters/weight", "National", "Set a voter's ballot weight (1–1000). Resolved at tally time."),
    ]),
    ("Admin — adjudication &amp; remedy", [
        ("GET", "/admin/api/polls/&lt;poll&gt;/provisionals", "Poll admin", "Pending provisionals — identity fields only; answers stay sealed."),
        ("POST", "/admin/api/polls/&lt;poll&gt;/provisionals/&lt;receipt&gt;", "Poll admin", "Adjudicate: {action:'verify', member_id} promotes + burns codes; {action:'reject', note}."),
        ("GET", "/admin/api/polls/&lt;poll&gt;/lookup?member_id|receipt=…", "National", "Troubleshooting: identity-linked answers; secret questions show recorded-or-not, never content. Audited."),
        ("POST", "/p/&lt;poll&gt;/admin/void", "Poll admin", "Void a cast ballot (flag, never delete) and reissue a fresh code. Open-window only, audited."),
    ]),
]


def _api_docs_html():
    rows = ""
    for group, eps in API_GROUPS:
        rows += f'<h2>{group}</h2><table>'
        rows += '<tr><th>Method</th><th>Path</th><th>Auth</th><th>What it does</th></tr>'
        for m, path, auth, desc in eps:
            mc = {"GET": "#2a78d6", "POST": "#dd1111"}.get(m, "#767676")
            rows += (f'<tr><td><b style="color:{mc}">{m}</b></td>'
                     f'<td><code>{path}</code></td><td>{auth}</td><td>{desc}</td></tr>')
        rows += "</table>"
    body = f"""
<h2>Overview</h2>
<p>RosaVote is API-first: the admin console is a thin client over these
endpoints, so anything it does can be scripted. Base URL is this deployment's
origin. All responses are JSON unless noted (ballot/console pages return HTML;
BLT and ZIP exports return files).</p>
<h2>Authentication</h2>
<p>Admin endpoints take an <b>admin token</b> in the <code>X-Admin-Token</code>
header. Tokens are stored only as SHA-256 hashes. <b>National</b> tokens can do
everything; <b>chapter</b> tokens are scoped to their own polls and cannot
build elections, import rolls, set weights, or run lookups. Voter endpoints
take a one-time <b>voting code</b> in the request body, never a header. Every
administrative action is written to an append-only audit log under the token's
name.</p>
<h2>Conventions</h2>
<p><code>&lt;poll&gt;</code> is a poll id (<code>[a-z0-9_]{{3,64}}</code>).
Errors are <code>{{error, message}}</code> with a 4xx/5xx status. Rate note:
the service scales on Cloud Run behind an instance cap — floods become
slowness, not failures, and never affect an in-progress vote.</p>
{rows}
<h2>Example</h2>
<pre>curl -s -H "X-Admin-Token: $TOKEN" \\
  https://&lt;origin&gt;/admin/api/polls/my_poll/results?live=1</pre>
"""
    return _LEGAL_SHELL.replace(
        "<style>", "<style>table{{border-collapse:collapse;width:100%;margin:6px 0 20px;"
        "font-size:.86rem;font-family:system-ui,sans-serif}}"
        "th,td{{border:1px solid rgba(0,0,0,.2);padding:6px 8px;text-align:left;vertical-align:top}}"
        "th{{background:#ffe1b2;font-family:'Arial Narrow',sans-serif;text-transform:uppercase;font-size:.72rem}}"
        "td code{{font-size:.82rem;word-break:break-all}}pre{{background:#fff5e5;border:1px solid #000;"
        "padding:10px;overflow-x:auto;font-size:.82rem}}").format(title="API Reference", body=body)


@app.get("/api")
def api_docs():
    return Response(_api_docs_html(), mimetype="text/html")


# Verification results — regenerate the counts with tools/blt_regression.py and
# tools/replay_election.py after any tabulator change; these are the last
# recorded runs, shown on the public /accuracy page.
ACCURACY = {
    "opavote_anchor": {"election": "NPC At-Large 2025", "candidates": 37,
                       "seats": 23, "ballots": 1279, "match": "23 / 23 winners"},
    "regression": {"elections": 40, "ballots": 6429, "weighted_ballots": 51274,
                   "errors": 0, "deterministic": True, "seats_filled": True,
                   "score_fixtures": 2},
    "replay": {"election": "NLC Steering (19 candidates, 13 seats)", "ballots": 810,
               "cast": "810 / 810", "throughput": "48–91 votes/sec (single client, "
               "48 at 40 devices, 91 at 3,220 ballots / 100 devices)", "match": True},
    "stress": {"ballots": 3220, "devices": 100, "cast": "3,220 / 3,220",
               "failures": 0, "peak": "91 votes/sec", "match": True},
}


def _accuracy_html():
    a = ACCURACY
    body = f"""
<p style="font-size:.95rem"><b>How accurate is RosaVote at tabulating an election?</b>
The counting engine is the shipped <code>stv_tabulate.py</code> (Scottish STV
per SSI 2007/42; Meek STV per the New Zealand rules; plus Score and STAR voting
for rated ballots). Accuracy here means several separate things, each tested
independently and each reproducible by you.</p>

<h2>1 · Exact match against OpaVote</h2>
<p>The strongest check: on the real <b>{a['opavote_anchor']['election']}</b>
({a['opavote_anchor']['candidates']} candidates, {a['opavote_anchor']['seats']}
seats, {a['opavote_anchor']['ballots']:,} ballots), RosaVote's Scottish STV count
elects the <b>identical winner set</b> as OpaVote's certified count —
<b>{a['opavote_anchor']['match']}</b>, quota and all. Because RosaVote reads and
writes the same standard BLT ballot format, anyone can reproduce this: download
the ballots, run them in OpaVote, OpenSTV, or this codebase's tabulator, and
compare. Reproducibility, not a trust-us certificate, is the guarantee.</p>

<h2>2 · Regression over {a['regression']['elections']} real elections</h2>
<p>A corpus of {a['regression']['elections']} real YDSA, NCC, and chapter
election ballot files — {a['regression']['ballots']:,} ballots
({a['regression']['weighted_ballots']:,} weighted) — is counted on every build.
These exercise the full variety of real-world BLT files: skipped rankings,
overvotes, empty/abstain ballots, ballot weights above one, and races from 2 to
20 candidates. Results:</p>
<ul>
<li><b>{a['regression']['errors']} parse or count errors</b> across all
{a['regression']['elections']} files.</li>
<li><b>Fully deterministic</b> — every election counts to the identical winner
set on repeated runs (no hidden randomness in tabulation; candidate <i>display</i>
order is shuffled per voter, but that never touches the count).</li>
<li><b>Every seat filled</b> correctly for every contest.</li>
<li><b>Score and STAR voting</b> are covered too: {a['regression']['score_fixtures']}
score-ballot fixtures count deterministically under both Score (sum of 0–max
ratings) and STAR (score-then-automatic-runoff), including the DSA at-large
0/1/2 delegate method with its gender/racial-minority quota reservations.</li>
</ul>
<p>Scottish and Meek STV agree on most contests and legitimately differ on a few
close ones — that's the two methods being genuinely different rules, not an
error. Run it yourself: <code>python3 tools/blt_regression.py</code>.</p>

<h2>3 · End-to-end replay from remote devices</h2>
<p>Tabulating a file is one thing; proving the <i>live voting pipeline</i>
records ballots faithfully is another. The replay harness casts an entire real
election through the actual voting API — one voting code per ballot, POSTed
concurrently from a pool of simulated remote devices — then reads the
<b>stored</b> ballots back and counts them. On the
<b>{a['replay']['election']}</b> election, {a['replay']['cast']} ballots were
cast with <b>zero failures</b> (one-code-one-vote held under concurrency), and
the result computed from stored ballots <b>matched a direct count of the source
file exactly</b>. So the claim/validate/store/tabulate path introduces no error.
Throughput measured {a['replay']['throughput']} — client-bound, since a real
electorate is spread across thousands of devices and days rather than one test
machine. Run it: <code>python3 tools/replay_election.py &lt;file.blt&gt; --base
&lt;host&gt; --token …</code>.</p>

<h2>4 · Load / stress test</h2>
<p>Alongside accuracy, RosaVote is stress-tested for scale. In the largest run,
<b>{a['stress']['ballots']:,} ballots were cast at {a['stress']['devices']}
concurrent simulated devices</b> against the live service:
<b>{a['stress']['cast']} recorded, {a['stress']['failures']} failures</b>, and the
stored ballots still tabulated to the correct result. Throughput scaled linearly
with concurrency to a peak of <b>{a['stress']['peak']}</b> and stayed
client-bound — the single test laptop over the public internet was the
bottleneck, not the server. For context, 120,000 members voting over several days
averages well under 2 votes/sec, and even a last-hour surge is a small fraction of
what one test machine already drove without a single dropped or double-counted
ballot. The service also caps instances and serves published results from a frozen
cache, so a spike in <i>viewers</i> can never slow <i>voting</i>. This stress run
surfaced and fixed one real concurrency bug (a stale per-instance config cache on a
freshly created poll), now closed.</p>

<h2>What this does and doesn't prove</h2>
<p><b>Proven:</b> the tabulator reproduces OpaVote's result on a real
23-seat election; it counts a wide corpus of real ballots deterministically and
without error; and the live vote pipeline stores ballots that tabulate to the
same result. All of it is reproducible in software RosaVote doesn't control.</p>
<p><b>Not claimed:</b> formal government certification (that program is for
public-office systems and doesn't apply to an org's internal elections), nor a
substitute for an independent security audit before a binding election. The
honest, proportionate assurance for this use case is: open-source code + a
reproducible recount + a tamper-evident hash chain over every ballot + these
regression and replay tests. A losing candidate can recount every ballot
themselves — which is a stronger guarantee than any closed certificate.</p>
"""
    return _LEGAL_SHELL.replace("<style>",
        "<style>ul{{margin:6px 0 14px 20px}}li{{margin:0 0 5px}}").format(
        title="Accuracy &amp; Verification", body=body)


@app.get("/accuracy")
def accuracy_page():
    return Response(_accuracy_html(), mimetype="text/html")


# ---- voting-methods explainer -------------------------------------------
# Every method RosaVote can run, in plain English, with honest trade-offs.
# "official" = can be a contest's binding count; "preview" = comparison only.
METHODS = [
    {"name": "Yes / No (with Abstain)", "kind": "Single question", "role": "official",
     "how": "Each voter picks Yes, No, or Abstain. The side with more votes wins; "
            "abstentions are reported but excluded from the Yes-vs-No margin.",
     "pros": ["Unambiguous for a single motion, endorsement, or bylaws change.",
              "Abstain is recorded separately, so quorum/participation is visible."],
     "cons": ["Only answers one binary question — not for electing people or ranking options."],
     "use": "Referendums, endorsements, single ballot measures."},
    {"name": "Multi-select", "kind": "Choose-many", "role": "official",
     "how": "Voters check every option they support (or Abstain). Each check is counted; "
            "the tally shows support for each option.",
     "pros": ["Simple for 'check all that apply' — pledges, interest sign-ups, non-exclusive lists.",
              "No vote-splitting between similar options."],
     "cons": ["As an election method it's majoritarian (see MNTV below) — a coordinated "
              "majority can carry every option; RosaVote uses it for tallies, not seat allocation."],
     "use": "Pledge lists, committee interest, any non-exclusive checklist."},
    {"name": "Ranked choice — Scottish STV", "kind": "Proportional (multi-winner)", "role": "official",
     "how": "Voters rank candidates 1, 2, 3… A candidate needs a quota (Droop) to win. "
            "Surplus votes above the quota, and votes for eliminated candidates, transfer "
            "to each ballot's next choice — by fixed fractions (SSI 2007/42).",
     "pros": ["Proportional: a like-minded bloc wins seats in proportion to its support.",
              "Little vote-splitting or wasted votes; widely used and legally defined.",
              "Exactly the method behind DSA's NPC At-Large count (matches OpaVote)."],
     "cons": ["More complex to explain than a plain X-vote.",
              "Fractional transfers mean a hand recount is tedious (but fully reproducible)."],
     "use": "Delegate slates, at-large committees, any multi-seat body meant to mirror the electorate."},
    {"name": "Ranked choice — Meek STV", "kind": "Proportional (multi-winner)", "role": "official",
     "how": "Same ranked ballots as Scottish STV, but surpluses transfer continuously via "
            "iterative 'keep factors' and the quota shrinks as ballots exhaust (New Zealand rules).",
     "pros": ["The most precise STV — treats later preferences more fairly than fixed-fraction transfer.",
              "Required by YDSA for its NCC and delegate elections."],
     "cons": ["Needs a computer (iterative, not hand-countable).",
              "Can differ from Scottish STV in very close contests — same ballots, subtler math."],
     "use": "YDSA NCC/co-chairs and delegate elections; anywhere the bylaws specify Meek."},
    {"name": "Score voting", "kind": "Rated (multi-winner)", "role": "official",
     "how": "Voters rate every candidate on a scale (DSA at-large uses 0/1/2 = "
            "disapprove/neutral/approve). Scores are summed; the highest totals win.",
     "pros": ["Very expressive — you rate everyone, not just rank.",
              "Simple to tabulate and hand-verify (just add the columns).",
              "The exact method in DSA's 2023 at-large delegate rules."],
     "cons": ["Majoritarian, not proportional — a majority that scores its slate high can win every seat.",
              "Invites tactical min/max scoring (give only 0s and maxes)."],
     "use": "At-large delegate elections run under score rules; quick expressive votes."},
    {"name": "STAR voting", "kind": "Rated (single-winner)", "role": "official",
     "how": "Score Then Automatic Runoff. Round 1: sum the ratings. Round 2: the two "
            "highest-scoring candidates go to an automatic runoff, where each ballot counts "
            "for whichever finalist it scored higher. The runoff winner is elected.",
     "pros": ["Keeps score's expressiveness but the runoff blunts tactical exaggeration.",
              "Always elects a candidate a majority preferred over the runner-up.",
              "Matches the Equal Vote / starvoting.org single-winner definition exactly."],
     "cons": ["Single-winner by nature; for many seats it becomes Bloc STAR (below).",
              "Slightly more to explain than plain score."],
     "use": "Single officers (a chair, a treasurer), tie-broken expressive elections."},
    {"name": "Bloc STAR", "kind": "Rated (multi-winner, majoritarian)", "role": "official",
     "how": "STAR run once per seat: elect a STAR winner, remove them, repeat with the same ballots.",
     "pros": ["Simple multi-winner extension of STAR; good when you want the majority's whole slate."],
     "cons": ["Majoritarian — a 51% bloc can take 100% of the seats; NOT proportional.",
              "Use STAR-PR or STV instead for a representative body."],
     "use": "Small slates where proportionality isn't the goal."},
    {"name": "STAR-PR (Allocated Score)", "kind": "Rated (proportional)", "role": "official",
     "how": "Proportional multi-winner STAR. A Hare quota = ballots ÷ seats. Each round elects "
            "the highest scorer, then 'spends' exactly one quota of ballot weight from the voters "
            "who scored that winner highest — so a faction that fills a seat has less weight left "
            "for the next. This is the Equal Vote Coalition's method (and the default multi-winner "
            "mode in the larryhastings/starvote library).",
     "pros": ["Proportional like STV, but on expressive rated ballots.",
              "A cohesive minority earns its fair share of seats."],
     "cons": ["Newer and less battle-tested than STV in binding elections.",
              "Quota ballot-spending is computer-only (reproducible, not hand-countable)."],
     "use": "Multi-seat committees on score ballots where minority representation matters."},
    {"name": "Plurality / SNTV", "kind": "Comparison", "role": "preview",
     "how": "Each ballot counts for its first choice only; the top vote-getters win.",
     "pros": ["The most familiar method; trivial to count."],
     "cons": ["Severe vote-splitting; 'spoiler' effects; not proportional."],
     "use": "Recount comparison only — see how a contest would look under plain plurality."},
    {"name": "Approval", "kind": "Comparison", "role": "preview",
     "how": "Every candidate a voter marked (here, ranked at all) counts as approved; most approvals win.",
     "pros": ["Simple; no vote-splitting between similar candidates."],
     "cons": ["Ignores strength of preference; majoritarian."],
     "use": "Recount comparison only."},
    {"name": "Borda count", "kind": "Comparison", "role": "preview",
     "how": "Points by rank: a first choice earns the most, each lower rank one point less.",
     "pros": ["Rewards broad consensus candidates."],
     "cons": ["Easy to game with strategic ranking; rarely used for binding elections."],
     "use": "Recount comparison only."},
    {"name": "Instant-Runoff (IRV / RCV)", "kind": "Comparison", "role": "preview",
     "how": "Single-winner ranked choice: the lowest candidate is eliminated and their ballots "
            "transfer until someone has a majority. For multiple seats RosaVote runs it "
            "sequentially (elect, remove, repeat).",
     "pros": ["Familiar 'ranked-choice voting' as used in many US public elections.",
              "Guarantees a majority winner for a single seat."],
     "cons": ["Sequential/for-many-seats it is majoritarian, NOT proportional (unlike STV)."],
     "use": "Recount comparison; single-seat ranked contests where a majority winner is the goal."},
    {"name": "MNTV / block plurality", "kind": "Comparison", "role": "preview",
     "how": "Each voter casts up to as many equal votes as there are seats (here, their top "
            "preferences); the highest vote-getters fill the seats.",
     "pros": ["Simple and familiar for multi-seat races on a single ballot."],
     "cons": ["Strongly majoritarian — a coordinated majority sweeps every seat; not proportional."],
     "use": "Recount comparison only — the OpaVote-documented block-voting baseline."},
]


def _methods_html():
    rows = []
    for m in METHODS:
        badge = ("<span class='role o'>Official count</span>" if m["role"] == "official"
                 else "<span class='role p'>Comparison preview</span>")
        pros = "".join(f"<li>{_esc(p)}</li>" for p in m["pros"])
        cons = "".join(f"<li>{_esc(c)}</li>" for c in m["cons"])
        rows.append(
            f"<div class='m'><h2>{_esc(m['name'])} <small>{_esc(m['kind'])}</small> {badge}</h2>"
            f"<p>{_esc(m['how'])}</p>"
            f"<div class='pc'><div><p class='k'>Pros</p><ul class='pro'>{pros}</ul></div>"
            f"<div><p class='k'>Cons</p><ul class='con'>{cons}</ul></div></div>"
            f"<p class='use'><b>Best for:</b> {_esc(m['use'])}</p></div>")
    body = (
        "<p>RosaVote runs several voting methods. <b>Official count</b> methods can be a "
        "contest's binding result; <b>comparison preview</b> methods let an administrator see "
        "how the same ballots would turn out under another rule (never the official result). "
        "Proportional methods (STV, Meek, STAR-PR) give a like-minded group seats in proportion "
        "to its support; majoritarian methods can let a 51% bloc take every seat. Every method "
        "here is documented, tested, and reproducible — see the "
        "<a href='/accuracy'>accuracy &amp; tests</a> page.</p>"
        + "".join(rows)
        + "<p style='margin-top:18px'><b>Quota requirements</b> (diversity minimums/maximums, "
        "e.g. the YDSA NCC's ‘at least 5 non-cis-men, at least 4 people of color’) can be layered "
        "onto any STV, Meek, or Score/STAR contest: candidates barred by a maximum are passed over, "
        "and seats are reserved so a minimum is never stranded — across a whole body of contests "
        "when needed.</p>")
    extra = ("<style>.m{{border:1px solid #000;box-shadow:4px 4px 0 0 #000;background:#fff;"
             "padding:12px 14px;margin:0 0 16px}}.m h2{{font-size:1.02rem;margin:0 0 6px}}"
             ".m h2 small{{font-weight:400;color:#555;font-size:.8rem}}"
             ".role{{font-size:.62rem;font-weight:700;text-transform:uppercase;padding:2px 6px;"
             "border:1px solid #000;white-space:nowrap}}.role.o{{background:#dd1111;color:#fff}}"
             ".role.p{{background:#ffe1b2}}.pc{{display:flex;gap:16px;flex-wrap:wrap}}"
             ".pc>div{{flex:1;min-width:180px}}.k{{font-weight:700;font-size:.72rem;"
             "text-transform:uppercase;margin:4px 0 2px}}.m ul{{margin:2px 0 6px 18px}}"
             ".m li{{margin:0 0 3px;font-size:.9rem}}ul.pro li{{list-style:'✓ '}}"
             "ul.con li{{list-style:'✕ '}}.use{{font-size:.88rem;margin:6px 0 0}}</style>")
    return _LEGAL_SHELL.replace("</head>", extra + "</head>").format(
        title="Voting Methods", body=body)


@app.get("/methods")
def methods_page():
    return Response(_methods_html(), mimetype="text/html")


# ---- Why RosaVote (vs. OpaVote, DSA context) ----------------------------
# OpaVote is a good partner to DSA; this page frames RosaVote as the
# preferred alternative — stronger, more flexible, customizable, and more
# affordable — WITHOUT forcing a migration. Some chapters will keep OpaVote
# for high-stakes elections (an independent third party adds trust there).
_VS_ROWS = [
    ("Cost at DSA scale",
     "About $0.08 per voter, per election — reasonable for years, but it "
     "multiplies across 200+ chapters each running their own vote.",
     "Can run on cloud infrastructure DSA already operates — a fraction of "
     "the cost, at any scale."),
    ("Customization",
     "One general-purpose ballot template.",
     "Fully customizable: multi-section ballots, chapter-unique questions, "
     "randomized slates, and per-section visibility on a single page."),
    ("DSA-specific rules",
     "Diversity quotas and bylaw timing are handled by hand, around the tool.",
     "Diversity-quota reservations, Article V timing, delegate alternates, and "
     "national + chapter-scoped admin built in."),
    ("Flexibility & control",
     "Proprietary and hosted — you work within its limits.",
     "Open source (AGPL-3.0) — anyone can host, audit, or extend it; no "
     "vendor lock-in."),
    ("Data ownership",
     "Ballots are hosted by the vendor and removed about 12 weeks after the "
     "election.",
     "Ballots stay on infrastructure the organization controls, retained on "
     "its own terms."),
    ("Voter verification",
     "Published results and a downloadable ballot file.",
     "Per-voter receipts, a public hash chain, and an in-browser verifier — "
     "so members check their own vote and anyone can recount."),
    ("Counting methods",
     "Scottish STV, Borda, IRV, Condorcet — a solid, proven set.",
     "All of those plus Meek STV and Score/STAR/STAR-PR — open and "
     "reproducible, reading the same standard ballot files."),
    ("Support & independence",
     "Commercial support and a neutral, third-party platform — a real plus "
     "for high-stakes or contested races.",
     "Maintained by DSA members and open to contributions from the whole "
     "movement."),
]


def _vs_opavote_html():
    rows = "".join(
        f"<tr><th scope='row'>{_esc(feat)}</th>"
        f"<td data-l='OpaVote'>{_esc(op)}</td>"
        f"<td class='rv' data-l='RosaVote'>{_esc(rv)}</td></tr>"
        for feat, op, rv in _VS_ROWS)
    body = (
        "<p>OpaVote is a good partner to DSA — an affordable, dependable service "
        "that has run our elections for years, and it stays a solid choice, "
        "especially for high-stakes or contested races where an independent "
        "third-party platform adds trust.</p>"
        "<p><b>RosaVote is an independent, open-source alternative</b>: stronger, "
        "more flexible, fully customizable, and more affordable — open source, "
        "built by DSA members, for DSA-style elections. Nothing forces a migration — "
        "chapters choose "
        "what fits each race; RosaVote is simply the better default for most of "
        "them.</p>"
        "<table class='vs'><thead><tr><th></th>"
        "<th>OpaVote</th><th class='rv'>RosaVote</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<h2>What chapters gain</h2>"
        "<ul><li><b>Lower cost.</b> Elections run for a fraction of per-chapter "
        "vendor fees, on cloud infrastructure DSA already pays for.</li>"
        "<li><b>Ballots that fit our bylaws.</b> Quotas, Article V timing, "
        "alternates and custom multi-section ballots — not workarounds bolted on "
        "the side.</li>"
        "<li><b>Real verifiability.</b> Members confirm their own vote and anyone "
        "can independently recount from a public hash chain.</li>"
        "<li><b>Ownership.</b> The organization running the election controls its "
        "data and infrastructure, and the code is open for anyone to audit, "
        "host, or extend.</li></ul>"
        "<h2>When OpaVote still makes sense</h2>"
        "<p>OpaVote is a good partner, and nothing pushes a chapter off it. For "
        "an especially high-stakes or contested election, some will prefer the "
        "independence of a neutral, third-party vendor with commercial support — "
        "and OpaVote remains an excellent choice for exactly that. RosaVote is the "
        "preferred default everywhere else, and it's here whenever a chapter is "
        "ready.</p>"
        "<p class='fine' style='margin-top:14px'>RosaVote is an independent "
        "open-source project — not affiliated with or endorsed by the Democratic "
        "Socialists of America (DSA) or by OpaVote; OpaVote is a trademark of its "
        "owners. Descriptions here reflect OpaVote's public pricing/documentation "
        "and its June 2026 DSA case study, and corrections are welcome via the "
        "source repository.</p>")
    extra = ("<style>table.vs{{border-collapse:collapse;width:100%;margin:8px 0 4px;"
             "font-size:.9rem}}table.vs th,table.vs td{{border:1px solid #000;"
             "padding:8px 10px;text-align:left;vertical-align:top}}"
             "table.vs thead th{{background:#111;color:#fff;font-size:.8rem}}"
             "table.vs thead th.rv{{background:#111;"
             "border-bottom:3px solid #dd1111}}"
             "table.vs th[scope=row]{{background:#f4f4f4;width:20%;font-weight:700}}"
             "table.vs td{{background:#fafafa}}table.vs td.rv{{background:#fff5f5}}"
             "h2{{font-size:1.02rem;margin:18px 0 4px}}"
             "@media(max-width:640px){{table.vs,table.vs tbody,table.vs tr,"
             "table.vs td,table.vs th{{display:block;width:auto!important}}"
             "table.vs thead{{display:none}}table.vs tr{{margin:0 0 12px;"
             "border:2px solid #000}}"
             "table.vs td::before{{content:attr(data-l);font-weight:700;"
             "display:block;font-size:.7rem;text-transform:uppercase;"
             "color:#dd1111;margin-bottom:2px}}}}</style>")
    return _LEGAL_SHELL.replace("</head>", extra + "</head>").format(
        title="Why RosaVote", body=body)


@app.get("/vs-opavote")
def vs_opavote_page():
    return Response(_vs_opavote_html(), mimetype="text/html")


@app.get("/terms")
def terms_page():
    return Response(_LEGAL_SHELL.format(title="Terms of Use", body=TERMS_BODY),
                    mimetype="text/html")


@app.get("/privacy")
def privacy_page():
    return Response(_LEGAL_SHELL.format(title="Privacy Policy", body=PRIVACY_BODY),
                    mimetype="text/html")


@app.get("/healthz")
@app.get("/health")
def healthz():
    # note: Google's frontend intercepts /healthz on run.app URLs (returns
    # its own 404), so external checks must use /health; /healthz still
    # works for container-internal probes.
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
