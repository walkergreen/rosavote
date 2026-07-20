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
            _LazyDB._client = firestore.Client()
        return getattr(_LazyDB._client, name)


db = _LazyDB()

# ---- generic ballot schema ------------------------------------------------
# Every poll is an ordered list of QUESTIONS. Types:
#   yesno  — YES / NO (+ Abstain unless allow_abstain=False)
#   ranked — Scottish STV; options [{id,name,sub?}], seats, alternates
#            (alternates>0 => two-count method + over-seat rank styling),
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
    return (real + f"<b>How this is counted (two-count method):</b> delegates are "
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


def secret_keys(cfg) -> list:
    return [q["key"] for q in poll_questions(cfg) if q.get("secret")]


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
        elif typ == "text":
            comments[key] = str(v or "")[: q.get("max", TEXT_MAX_DEFAULT)]
    return answers, comments


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
    if cfg.get("finalized"):
        return "closed"          # finalized never reopens implicitly
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
    role_attr = f' role="{role}" aria-checked="false"' if role else ""
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
                   + (f'<p class="vk-sect-s">{sect.get("sub")}</p>' if sect.get("sub") else "")
                   + "</div>")
    label = f' &middot; {_esc(q["label"])}' if q.get("label") else ""
    out.append('<div class="vk-measure"' + (' style="margin-top:20px;"' if n > 1 else "") + ">"
               f'<p class="vk-measure-no">Question {n} of {total}{label}</p>'
               f'<p class="vk-measure-q">{_esc(q["title"])}</p>'
               + "".join(f'<p class="vk-measure-t">{para}</p>' for para in (q.get("text") or []))
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
        elif q["type"] in ("ranked", "multi"):
            names = {o["id"]: o["name"] for o in q["options"]}
            names.setdefault("ABSTAIN", "Abstain")
        qdef.append({"key": q["key"], "type": q["type"], "n": i + 1,
                     "title": q["title"], "names": names,
                     "seats": q.get("seats", 0) if q.get("alternates") else 0,
                     "required": q.get("required", q["type"] in ("yesno", "ranked"))})

    html = TEMPLATE.replace("__POLL_ID__", poll_id)
    html = html.replace("__CHAPTER_NAME__", cfg["name"])
    from urllib.parse import quote
    html = html.replace("__HELP_SUBJECT__", quote(f"[BALLOT26] {cfg['name']} — Can't find my code"))
    html = html.replace("__QUESTIONS_HTML__", "".join(parts))
    html = html.replace("__QDEF__", json.dumps(qdef))
    html = html.replace("__N_Q__", str(total))
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


def _write_ballot_docs(poll_id: str, cfg: dict, receipt: str, answers: dict,
                       comments: dict, identity: dict, code_h, weight: int = 1):
    """Write the main (identity-linked) record and, when the ballot has
    secret questions, the separate secret-ballot record. Shared by coded
    votes and provisional promotion so the storage split can't drift.
    `weight` is only stamped for codeless (provisional) ballots — coded
    ballots resolve their CURRENT weight from the code doc at tally time."""
    skeys = set(secret_keys(cfg))
    main_answers = {k: v for k, v in answers.items() if k not in skeys}
    nonce = secrets.token_hex(8)
    ac = canon_answers(main_answers)
    rh = make_record_hash(receipt, ac, nonce)
    wfield = {"weight": weight} if (code_h is None and weight != 1) else {}
    db.collection(f"{poll_id}__ballots").document(rh).set({
        "receipt": receipt, "answers": main_answers, "answers_canon": ac,
        "nonce": nonce, "record_hash": rh,
        "code_hash": code_h,
        "comment": comments.get("text", ""),   # legacy field for older tools
        "comments": comments,
        "day_bucket": firestore.SERVER_TIMESTAMP,
        **wfield,
        **identity,
    })
    if skeys:
        dq = {k: (answers.get(k) or []) for k in sorted(skeys)}
        dnonce = secrets.token_hex(8)
        dcanon = canon_answers(dq)
        drh = make_record_hash(receipt, dcanon, dnonce)
        db.collection(f"{poll_id}__delegate_ballots").document(drh).set({
            "receipt": receipt, **dq, "answers_canon": dcanon,
            "nonce": dnonce, "record_hash": drh,
            "code_hash": code_h,   # ADMIN-ONLY troubleshooting trace — never chapter-visible
            "day_bucket": firestore.SERVER_TIMESTAMP,
            **wfield,
            **({"provisional": True} if identity.get("provisional") else {}),
        })
    return rh


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
    txn = db.transaction()
    claim = _claim_code(txn, codes, ch)
    if claim is None:
        return {"status": "already_voted"}
    receipt = secrets.token_hex(4).upper()
    _write_ballot_docs(poll_id, cfg, receipt, answers, comments,
                       {"member_id": claim.get("member_id"),
                        "chapter": claim.get("chapter")}, ch)
    return {"status": "recorded", "receipt": receipt}


def cast_provisional(poll_id: str, answers: dict, comments: dict, info: dict) -> dict:
    """Sealed provisional ballot: stored separately, NOT counted, no code used.
    Adjudicated by staff against membership after the fact. (Identity-linked
    by design until adjudication, so the comment lives with it.)"""
    prov = db.collection(f"{poll_id}__provisional")
    receipt = "P" + secrets.token_hex(4).upper()[:7]
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


RECEIPT_RE = re.compile(r"^[A-Z0-9-]{4,16}$")


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
        elif q["type"] == "multi":
            vmax = max(list(q["counts"].values()) + [1])
            inner = "".join(_pub_bar(n, v, vmax) for n, v in q["counts"].items())
        elif q["type"] == "text":
            inner = (f'<p class="win">{q["responses"]} written response(s) received '
                     '(content reviewed by election administration).</p>')
        cards.append(f'<div class="card"><h2>{_esc(q["title"])}</h2>{inner}</div>')
    meta = (f'<div class="card"><p class="win">{res["ballots_counted"]} ballots counted'
            + (" · weighted election (ballots count at each voter's assigned weight)"
               if res.get("weighted") else "")
            + '. Voters can confirm their own ballot was recorded at any time with '
            'their receipt code.</p></div>')
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
        answers, comments = validate_answers(data.get("answers"), cfg)
    except ValueError as bad:
        return jsonify({"error": "invalid_answers", "field": str(bad)}), 400
    if not info.get("first") or not info.get("last") or not info.get("chapter"):
        return jsonify({"error": "missing_info"}), 400
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", info.get("emails", "")):
        return jsonify({"error": "missing_info", "field": "emails"}), 400
    return jsonify(cast_provisional(poll_id, answers, comments, info)), 200


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
  <div class="tagline">Democratic Socialists of America</div>
</div>
<header class="band"><div class="band-in">
  <div class="wm">RosaVote<small>Chapter Member Ballot &middot; Democratic Socialists of America</small></div>
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
      <p class="fine" style="margin-top:8px">Just exploring? The console&rsquo;s sign-in page has a
      <b>one-tap demo sign-in</b> (scoped to the shared Demo Sandbox poll only) &mdash;
      no credentials needed.</p>
      <details style="margin-top:12px">
        <summary>How to get access</summary>
        <div class="dbody">
        <p>The console needs an <b>admin token</b> (entered on its sign-in
        screen; nothing here works without one). <b>The chapter TEST codes at
        the top of this page are voting codes, not admin tokens</b> &mdash;
        they open ballots, never the console. <b>National (root) token:</b>
        held in Secret Manager as <code>ballot-admin-token</code> in the
        <code>dsa-org-tools</code> project &mdash; staff with project access
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
<footer><b>RosaVote</b> &middot; Prototype build &mdash; not a live election &middot; <a href="/terms" style="color:inherit">Terms</a> &middot; <a href="/privacy" style="color:inherit">Privacy</a><br/>Questions? Email <b>orgtools@dsausa.org</b><br/>Built with 🌹 by Walker Green</footer>
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
QUESTION_TYPES = ("yesno", "ranked", "multi", "text")


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

        if typ in ("ranked", "multi"):
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
            if q.get("require_full"):
                nq["require_full"] = True
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
            nq["secret"] = bool(q.get("secret")) or bool(q.get("delegate"))
            nq["delegate"] = bool(q.get("delegate"))
            nq["shuffle"] = bool(q.get("shuffle", nq["secret"]))
            for f in ("real_delegates", "real_alternates"):
                if q.get(f):
                    nq[f] = int(q[f])
            if nq["options"] and len(nq["options"]) <= nq["seats"] + nq["alternates"]:
                warnings.append(f"question {key!r} uncontested: {len(nq['options'])} "
                                f"option(s) for {nq['seats']} seat(s) + "
                                f"{nq['alternates']} alternate(s)")
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


def compute_results(poll_id: str, cfg: dict) -> dict:
    """WEIGHTED tallies per question (each ballot counts at its voter's
    current weight; default 1). Ranked contests use the shipped Scottish STV
    tabulator; delegate-style contests also run the alternates recount
    (two-count method). Text answers are counted, never displayed."""
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
                 "secret": bool(q.get("secret"))}
        rows = secret_rows if q.get("secret") else main_rows
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
            cons = q.get("constraints") or None
            group = q.get("quota_group")
            gpre, glater_seats, glater_supply = None, 0, None
            if group and group in qgroups:
                cons = qgroups[group]
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
            res = counter(_blt_text(rows, key, options, seats),
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
                recount = counter(_blt_text(rows, key, options, seats + alts),
                                  constraints=cons, cand_tags=ctags,
                                  pre_elected=gpre, later_seats=glater_seats,
                                  later_supply=glater_supply)
                entry["alternates"] = [w for w in recount["winners"]
                                       if w not in res["winners"]]
            if cons:
                # the admin-facing comparison: same ballots, quotas off
                res_u = counter(_blt_text(rows, key, options, seats))
                entry["unconstrained"] = {"winners": res_u["winners"],
                                          "quota": res_u["quota"],
                                          "stages": res_u["stages"]}
                if alts:
                    ru = counter(_blt_text(rows, key, options, seats + alts))
                    entry["unconstrained"]["alternates"] = [
                        w for w in ru["winners"] if w not in res_u["winners"]]
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
    return {"poll_id": poll_id, "name": cfg.get("name"),
            "finalized_at": cfg.get("finalized_at"),
            "final_counts": cfg.get("final_counts"),
            # secret-ballot-only polls keep their ballots in the secret collection
            "ballots_counted": max(len(main_rows), len(secret_rows)), "weighted": weighted,
            "questions": out}


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
    if not chapter_or_none(poll_id):
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
    _audit(poll_id, "ballot_lookup", ident["name"],
           member_id=member_id or None, receipt=receipt or None,
           found=len(matches))
    return jsonify({"found": len(matches), "ballots": [{
        "receipt": d.get("receipt"), "member_id": d.get("member_id"),
        "chapter": d.get("chapter"),
        "answers": d.get("answers"), "comments": d.get("comments") or {},
        "voided": bool(d.get("voided")), "void_reason": d.get("void_reason"),
        "provisional": bool(d.get("provisional")),
        "record_hash": d.get("record_hash"),
        "secret_ballot_recorded": d.get("receipt") in secret_receipts,
    } for d in matches]}), 200


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
    if method not in stv_tabulate.ALT_METHODS:
        return jsonify({"error": "bad_method",
                        "message": f"method must be one of {stv_tabulate.ALT_METHODS}"}), 400
    qkey = str(data.get("question") or "").strip()
    q = next((x for x in poll_questions(cfg)
              if x["key"] == qkey and x["type"] == "ranked"), None)
    if not q:
        return jsonify({"error": "not_a_ranked_question"}), 404
    main_rows, _, secret_rows = _tally_rows(poll_id)
    rows = secret_rows if q.get("secret") else main_rows
    blt = _blt_text(rows, qkey, q["options"], int(q.get("seats", 1)),
                    title=q["title"])
    res = stv_tabulate.count_alternative(blt, method)
    res["official_method"] = "Scottish STV"
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
    publish = bool((request.get_json(silent=True) or {}).get("publish", True))
    db.collection(CONFIG_COLL).document(poll_id).set(
        cfg_to_doc(dict(cfg, results_published=publish)))
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
        out.append(q["title"] + ("   [secret ballot]" if q.get("secret") else ""))
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
                out.append(f"  ALTERNATES (two-count recount): {', '.join(q['alternates'])}")
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
                row += [str(d.get("member_id") or ""),
                        '"' + str(d.get("chapter") or "").replace('"', '""') + '"']
            lines.append(",".join(row))
        z.writestr("ballots.csv", "\n".join(lines) + "\n")
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


def _import_members(poll_id, cfg, members, ident, source):
    """Mint one hashed code doc per NEW member. Returns (created_manifest,
    skipped, bad); plaintext codes exist only in the returned manifest."""
    codes = db.collection(f"{poll_id}__codes")
    existing = set()
    for snap in codes.stream():
        mid = (snap.to_dict() or {}).get("member_id")
        if mid:
            existing.add(str(mid))
    created, skipped, bad = [], 0, 0
    base = request.url_root.rstrip("/")
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
        codes.document(code_hash(code)).set(doc)
        created.append({"member_id": mid, "chapter": chapter, "weight": weight,
                        "code": code, "vote_link": f"{base}/p/{poll_id}/v/{code}"})
    _audit(poll_id, "voters_import", ident["name"], source=source,
           created=len(created), skipped=skipped, bad=bad)
    return created, skipped, bad


def _manifest_csv(created) -> str:
    lines = ["member_id,chapter,weight,code,vote_link"]
    for c in created:
        lines.append(",".join([c["member_id"],
                               '"' + str(c["chapter"]).replace('"', '""') + '"',
                               str(c.get("weight", 1)), c["code"], c["vote_link"]]))
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


ROLL_IMPORT_QUERY = """
WITH eligible AS (
  SELECT actionkit_id, dedup_group_id
  FROM `{roll}.ak_primary_id`
  WHERE is_primary AND membership_status = 'Member in Good Standing'
)
SELECT e.actionkit_id AS member_akid
FROM eligible e
JOIN `{roll}.ak_user_fields_pivoted` f ON f.user_id = e.actionkit_id
WHERE f.chapter = @chapter
"""


@app.post("/admin/api/polls/<poll_id>/voters/import_bigquery")
def admin_import_voters_bigquery(poll_id):
    """Import straight from the membership warehouse (national admins only):
    eligible primaries (Member in Good Standing) whose roll chapter matches.
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
    project = str(data.get("roll_project") or "proj-tmc-mem-dsa")
    dataset = str(data.get("roll_dataset") or "main")
    job_project = str(data.get("job_project") or "dsa-org-tools")
    try:
        from google.cloud import bigquery
        # jobs bill/run in our project; the roll is read cross-project
        client = bigquery.Client(project=job_project)
        job = client.query(
            ROLL_IMPORT_QUERY.format(roll=f"{project}.{dataset}"),
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("chapter", "STRING", chapter)]))
        members = [{"member_id": f"AK{row['member_akid']}", "chapter": chapter}
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
    _write_ballot_docs(poll_id, cfg, receipt, answers, comments,
                       {"member_id": member_id, "chapter": prov.get("chapter"),
                        "provisional": True}, None, weight=pw)
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


_LEGAL_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>{title} — RosaVote</title><link rel="icon" href="/logo.svg" type="image/svg+xml"/>
<style>body{{font:16px/1.55 Georgia,serif;background:#fff5e5;color:#111;margin:0}}
main{{max-width:640px;margin:0 auto;padding:24px 18px 60px}}
h1{{font-family:"Arial Narrow",sans-serif;text-transform:uppercase}}h2{{font-size:1.05rem}}
.banner{{background:#dd1111;color:#fff5e5;padding:12px 18px;font-family:"Arial Narrow",sans-serif;
font-weight:bold;text-transform:uppercase}} .draft{{background:#ffe1b2;border:1px solid #000;
padding:8px 12px;font-size:.85rem}} a{{color:#dd1111}}</style></head><body>
<div class="banner"><img src="/logo.svg" alt="" style="height:26px;vertical-align:-7px;margin-right:6px"/>RosaVote</div><main><h1>{title}</h1>
<p class="draft">DRAFT — prototype language pending review by DSA staff and counsel.
This service is a prototype and not a live election unless explicitly announced.</p>
{body}<p><a href="/">&larr; Back to RosaVote</a></p></main></body></html>"""

TERMS_BODY = """
<h2>What this service is</h2>
<p>RosaVote is open-source ranked-choice and STV election software
(AGPL-3.0, © 2026 Walker Green). This deployment is operated by the
Democratic Socialists of America's staff for chapter and national votes:
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
<h2>What we collect</h2>
<p><b>Voters:</b> your ballot answers, a receipt code, timestamps, and — for
named sections — your member ID and chapter, exactly as disclosed on the
ballot above Question 1. Secret-ballot questions (such as convention
delegates) are stored with no name and no chapter. Voting codes are stored
only as SHA-256 hashes. Provisional ballots additionally hold the contact
details you provide, sealed until adjudication.</p>
<h2>Who can see what</h2>
<p>Named answers: election administrators and your own chapter's admins.
Secret-ballot rankings: no one by name — administrators can only trace a
specific record for troubleshooting, under audit. National does not publish a
chapter's results; each chapter decides its own publication (never
secret-ballot rankings). Every administrative action is written to an
append-only audit log.</p>
<h2>What we don't do</h2>
<p>We do not sell or share member data, run ads, use tracking pixels or
third-party analytics, or send messages from this service. Election reminders
go through DSA's existing communication platforms.</p>
<h2>Where data lives — and who holds it</h2>
<p>All election data is stored in and controlled by the <b>Democratic
Socialists of America</b>, inside DSA's own Google Cloud project
(<code>dsa-org-tools</code>) alongside DSA's membership data warehouse — the
same infrastructure DSA staff already operate. The author of the RosaVote
software (Walker Green) does <b>not</b> host, receive, or have access to your
ballots, contacts, or any election data; the code is open source (AGPL-3.0)
but the data belongs to DSA. Data is processed only to run the election:
eligibility, one-member-one-vote, tabulation, auditing, and the
tamper-evidence chain. Ballot records are retained by DSA as the permanent
election record; voided ballots are flagged, never deleted. If a chapter or
national committee runs its own copy of the software, that body — not the
author — is the data controller for its deployment.</p>
<h2>Verification</h2>
<p>Your receipt code lets you confirm your ballot was stored
(<code>/p/&lt;poll&gt;/verify</code>) without revealing content. Exported
ballot files are anonymous.</p>
<h2>Contact</h2>
<p>Questions or concerns: orgtools@dsausa.org.</p>
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
