#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Adversarial security suite — offline, no GCP, no network:

    python3 tools/security_test.py

smoke_test.py proves the app does what it should. This proves it REFUSES what
it shouldn't, and is written so the refusals keep being checked as the app
grows. Six sections:

  A. AUTHORIZATION MATRIX — enumerates app.url_map and drives every route with
     no token / a foreign chapter token / an in-scope chapter token / national.
     Routes are classified by an explicit allowlist, and an UNCLASSIFIED ROUTE
     IS A FAILURE. That is the load-bearing part: a newly added endpoint cannot
     ship without someone declaring whether it is public, poll-scoped, or
     national-only, so "forgot the auth check" fails the build instead of
     shipping.
  B. INJECTION — hostile strings through every admin-settable config field into
     every rendered sink (ballot, splash, results, exports), asserting nothing
     comes back as live markup, an unbalanced CSV row, or a spreadsheet formula.
  C. ROBUSTNESS — malformed/hostile payloads at every unauthenticated entry
     point; the contract is "never 5xx", because a stack trace is both an
     availability bug and an information leak.
  D. BALLOT SECRECY — secret-question content must not appear in any output a
     chapter admin or the public can reach.
  E. VOTE INTEGRITY — one code one vote, closed windows refuse, test codes
     never persist.
  F. ABUSE RESISTANCE — per-IP throttles key on a hop the client cannot forge.

NOT a substitute for an independent audit or a real pentest. It tests the
classes of bug we know about, against the code as written; it cannot find a
vulnerability nobody has thought of, and it does not test the deployed
infrastructure (IAM, network, Cloud Armor, secret handling).
"""

import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ADMIN_TOKEN", "sec-test-national-token")

from _stubs import DOCS, GCS  # noqa: E402  (installs the GCP stubs)

import vote_service  # noqa: E402

app = vote_service.app
c = app.test_client()

NATIONAL = {"X-Admin-Token": "sec-test-national-token"}
CHAPTER_OWN = {"X-Admin-Token": "chapter-token-owns-secpoll"}
CHAPTER_OTHER = {"X-Admin-Token": "chapter-token-owns-otherpoll"}
NO_TOKEN: dict = {}
BAD_TOKEN = {"X-Admin-Token": "not-a-real-token-at-all"}

passed = 0
failures: list = []


def ok(cond, label):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(label)


def section(name):
    print(f"\n--- {name}")


# =========================================================================
# fixtures
# =========================================================================
import hashlib  # noqa: E402

for tok, polls in (("chapter-token-owns-secpoll", ["secpoll"]),
                   ("chapter-token-owns-otherpoll", ["otherpoll"])):
    DOCS[("config__admins", hashlib.sha256(tok.encode()).hexdigest())] = {
        "name": f"chapter admin {polls[0]}", "role": "chapter",
        "polls": polls, "active": True}

SEC_QS = [
    {"key": "motion", "type": "yesno", "title": "Shall we act?"},
    {"key": "officers", "type": "ranked", "title": "Elect officers", "seats": 1,
     "visibility": "secret", "shuffle": False,
     "options": [{"id": "AA", "name": "Ada"}, {"id": "BB", "name": "Ben"}]},
    {"key": "roll", "type": "yesno", "title": "Recorded vote", "visibility": "public"},
]
for pid in ("secpoll", "otherpoll"):
    r = c.post(f"/admin/api/polls/{pid}", headers=NATIONAL,
               json={"name": f"Poll {pid}", "questions": SEC_QS})
    assert r.status_code == 200, f"fixture poll {pid}: {r.get_json()}"

# a cast ballot in secpoll so exports have content
DOCS[("secpoll__codes", vote_service.code_hash("SECPOLLCODE01"))] = {
    "used": False, "member_id": "AK-SEC-1", "chapter": "Test Chapter"}
_orig_claim = vote_service._claim_code
vote_service._claim_code = lambda txn, coll, ch: {"member_id": "AK-SEC-1",
                                                  "chapter": "Test Chapter"}
c.post("/p/secpoll/vote", json={"code": "SECPOLLCODE01",
                                "answers": {"motion": "YES", "roll": "NO",
                                            "officers": ["AA", "BB"]}})


# =========================================================================
# A. AUTHORIZATION MATRIX
# =========================================================================
section("A. authorization matrix (auto-discovered from app.url_map)")

# How each route is allowed to answer an ANONYMOUS caller.
#   "public"        — anyone, by design
#   "any_admin"     — any VALID admin token; carries no poll_id to scope against
#                     (identity echo, scope-filtered listing, stateless counter)
#   "poll_admin"    — any admin of that poll (chapter token in scope, or national)
#   "national"      — national tokens only
# A route absent from this map fails the suite: adding an endpoint forces a
# deliberate decision about who may call it.
ROUTE_CLASS = {
    # ---- public by design ----
    "/": "public", "/about": "public", "/terms": "public", "/privacy": "public",
    "/methods": "public", "/accuracy": "public", "/vs-opavote": "public",
    "/api": "public", "/healthz": "public", "/health": "public",
    "/logo.svg": "public", "/prefs": "public",
    "/prefs/optout": "public", "/prefs/optin": "public",
    "/admin/": "public",                      # static shell; its APIs are gated
    "/p/<poll_id>/": "public",
    "/p/<poll_id>/v/<code>": "public",
    "/p/<poll_id>/voted": "public",
    "/p/<poll_id>/vote": "public",
    "/p/<poll_id>/provisional": "public",
    "/p/<poll_id>/resend": "public",
    "/p/<poll_id>/verify": "public",
    "/p/<poll_id>/verify/<fname>": "public",
    "/p/<poll_id>/verify-vote": "public",
    "/p/<poll_id>/results": "public",
    "/p/<poll_id>/rollcall/<qkey>.csv": "public",
    # ---- poll-scoped admin ----
    "/p/<poll_id>/admin/void": "poll_admin",
    "/admin/api/whoami": "any_admin",
    "/admin/api/polls": "any_admin",
    "/admin/api/count_blt": "any_admin",
    "/admin/api/polls/<poll_id>/open": "poll_admin",
    "/admin/api/polls/<poll_id>/close": "poll_admin",
    "/admin/api/polls/<poll_id>/publish": "poll_admin",
    "/admin/api/polls/<poll_id>/results": "poll_admin",
    "/admin/api/polls/<poll_id>/recount_preview": "poll_admin",
    "/admin/api/polls/<poll_id>/blt/<qkey>": "poll_admin",
    "/admin/api/polls/<poll_id>/export.zip": "poll_admin",
    "/admin/api/polls/<poll_id>/voters": "poll_admin",
    "/admin/api/polls/<poll_id>/provisionals": "poll_admin",
    "/admin/api/polls/<poll_id>/provisionals/<receipt>": "poll_admin",
    # ---- national only ----
    "/admin/api/admins": "national",
    "/admin/api/cron/closeout": "national",
    "/admin/api/polls/<poll_id>": "national",
    "/admin/api/polls/<poll_id>/archive": "national",
    "/admin/api/polls/<poll_id>/unarchive": "national",
    "/admin/api/polls/<poll_id>/lookup": "national",
    "/admin/api/polls/<poll_id>/voters/weight": "national",
    "/admin/api/polls/<poll_id>/voters/import": "national",
    "/admin/api/polls/<poll_id>/voters/import_gcs": "national",
    "/admin/api/polls/<poll_id>/voters/import_bigquery": "national",
}

SUBST = {"poll_id": "secpoll", "code": "SECPOLLCODE01", "fname": "ballots.csv",
         "qkey": "officers", "receipt": "P1234567"}


def concrete(rule):
    path = rule.rule
    for arg in rule.arguments:
        path = path.replace(f"<{arg}>", SUBST.get(arg, "x"))
        for conv in ("string:", "int:", "path:"):
            path = path.replace(f"<{conv}{arg}>", SUBST.get(arg, "x"))
    return path


def call(rule, headers):
    path, methods = concrete(rule), rule.methods - {"HEAD", "OPTIONS"}
    m = "POST" if "POST" in methods else "GET"
    fn = c.post if m == "POST" else c.get
    return fn(path, headers=headers, **({"json": {}} if m == "POST" else {}))


rules = [r for r in app.url_map.iter_rules() if r.endpoint != "static"]
unclassified = sorted({r.rule for r in rules} - set(ROUTE_CLASS))
ok(not unclassified,
   "every route is classified in ROUTE_CLASS "
   f"(unclassified: {unclassified})")

DENIED = (401, 403)
for rule in sorted(rules, key=lambda r: r.rule):
    kind = ROUTE_CLASS.get(rule.rule)
    if kind is None:
        continue
    label = f"{rule.rule}"
    if kind == "public":
        # only assert it is not an auth error; content is smoke_test's job
        ok(call(rule, NO_TOKEN).status_code not in DENIED,
           f"[public] {label} reachable anonymously")
        continue
    # every gated route must refuse anonymous + garbage tokens
    ok(call(rule, NO_TOKEN).status_code in DENIED, f"[authz] {label} denies no token")
    ok(call(rule, BAD_TOKEN).status_code in DENIED,
       f"[authz] {label} denies an unknown token")
    # ...and, where the route is poll-scoped, a chapter token for ANOTHER poll
    if kind in ("poll_admin", "national"):
        ok(call(rule, CHAPTER_OTHER).status_code in DENIED,
           f"[authz] {label} denies a foreign chapter token")
    if kind == "national":
        ok(call(rule, CHAPTER_OWN).status_code in DENIED,
           f"[authz] {label} is national-only (rejects in-scope chapter token)")

section("A2. token handling")
ok(vote_service._admin_identity("") is None, "empty token rejected")
ok(vote_service._admin_identity("x" * 129) is None, "over-long token rejected")
ok(vote_service._admin_identity(None) is None, "None token rejected")
_id = vote_service._admin_identity("sec-test-national-token")
ok(_id and _id["role"] == "national", "valid national token resolves")
# tokens are looked up by hash, so the stored doc never holds plaintext
ok(not any("sec-test-national-token" in json.dumps(v)
           for (coll, _), v in DOCS.items() if coll == "config__admins"),
   "no admin token stored in plaintext")


# =========================================================================
# B. INJECTION
# =========================================================================
section("B. injection (XSS / CSV) through every admin-settable string")

XSS = '</script><img src=x onerror=alert(1)>"\'><svg onload=alert(2)>'
# Only unambiguously-live markup. Substrings like "onerror=alert" also appear
# inside correctly escaped text (&lt;img ... onerror=alert(1)&gt;), where they
# are inert — matching on those produces false positives, not findings.
LIVE_MARKUP = ("<img src=x", "<svg onload", "</script><")

xcfg = {"name": XSS, "opens_at": None, "closes_at": None, "questions": [
    {"key": "q1", "type": "ranked", "title": XSS, "seats": 1, "alternates": 0,
     "shuffle": False,
     "label": XSS,
     "section": {"style": 1, "kicker": XSS, "title": XSS, "sub": XSS},
     "options": [{"id": "AA", "name": XSS, "sub": XSS}]},
    {"key": "q2", "type": "yesno", "title": XSS,
     "option_subs": {"YES": XSS, "NO": XSS}},
    {"key": "q3", "type": "text", "title": XSS, "max": 100},
]}
page = vote_service.render_ballot("xsspoll", xcfg, "")
for bad in LIVE_MARKUP:
    ok(bad not in page, f"ballot page inert: {bad!r} absent")
qdef = page.split("var QDEF=")[1].split(";", 1)[0]
ok("</script>" not in qdef and "\\u003c" in qdef,
   "QDEF cannot close its <script> element")
ok('value="&lt;/script&gt;' in page or 'value="' + XSS not in page,
   "chapter name escaped in attribute context")

# the same strings through the real config-save path and out to the splash
r = c.post("/admin/api/polls/xsspoll", headers=NATIONAL,
           json={"name": XSS, "test_code": "XSSTESTCODE01", "questions": xcfg["questions"]})
ok(r.status_code == 200, "hostile-but-valid config saves (sanitized, not rejected)")
vote_service.load_polls(force=True)
splash = c.get("/").data.decode()
for bad in LIVE_MARKUP:
    ok(bad not in splash, f"splash inert: {bad!r} absent")

# admin prose is sanitized, not echoed
ok("<script>" not in vote_service.sanitize_html("<script>alert(1)</script>"),
   "sanitize_html strips <script>")
ok("javascript:" not in vote_service.sanitize_html('<a href="javascript:alert(1)">x</a>'),
   "sanitize_html strips javascript: URLs")
ok("onerror" not in vote_service.sanitize_html('<img src=x onerror=alert(1)>'),
   "sanitize_html strips event handlers")

section("B2. CSV writers")
CSV_HOSTILE = ['AK1,999', '=HYPERLINK("http://evil/")', '+cmd', '-2+3', '@SUM(A1)',
               'quote"inside', 'newline\nrow']
for v in CSV_HOSTILE:
    cell = vote_service._csv_cell(v)
    ok(cell.startswith('"') and cell.endswith('"'), f"_csv_cell quotes {v!r}")
    ok(len(next(__import__("csv").reader([cell]))) == 1,
       f"_csv_cell keeps {v!r} in one column")
    if v[:1] in "=+-@":
        ok(cell[1:2] == "'", f"_csv_cell defuses formula {v!r}")

# end-to-end: a hostile member_id must not misalign the export
DOCS[("secpoll__ballots", "hostilerow")] = {
    "receipt": "HOSTILE1", "record_hash": "hh", "answers": {"motion": "YES"},
    "member_id": '=cmd|calc!A1,shift', "chapter": 'Chap,ter"x', "code_hash": "zz"}
DOCS[("config__polls", "secpoll")]["finalized"] = True
vote_service.load_polls(force=True)
z = c.get("/admin/api/polls/secpoll/export.zip", headers=NATIONAL)
ok(z.status_code == 200, "export.zip downloads")
bal = zipfile.ZipFile(io.BytesIO(z.data)).read("ballots.csv").decode()
rows = list(__import__("csv").reader(bal.splitlines()))
ok(len({len(r) for r in rows if r}) == 1,
   f"every export ballots.csv row has the header's column count "
   f"(widths seen: {sorted({len(r) for r in rows if r})})")
ok(not any(cell[:1] in "=+-@" for r in rows for cell in r),
   "no export cell begins with a spreadsheet formula character")

# and the public roll call
c.post("/admin/api/polls/secpoll/publish", headers=NATIONAL, json={"publish": True})
rc = c.get("/p/secpoll/rollcall/roll.csv")
if rc.status_code == 200:
    rrows = list(__import__("csv").reader(rc.data.decode().splitlines()))
    ok(len({len(r) for r in rrows if r}) == 1,
       "roll-call CSV rows all match the header width")
    ok(not any(cell[:1] in "=+-@" for r in rrows for cell in r),
       "no roll-call cell begins with a formula character")


# =========================================================================
# C. ROBUSTNESS — unauthenticated input must never 5xx
# =========================================================================
section("C. malformed input at unauthenticated entry points")

HOSTILE_BODIES = [
    None, {}, {"code": None}, {"code": 12345}, {"code": ["list"]},
    {"code": "A" * 500}, {"code": "\x00\x01\x02"}, {"code": "../../etc/passwd"},
    {"code": "SECPOLLCODE01", "answers": None},
    {"code": "SECPOLLCODE01", "answers": "string-not-dict"},
    {"code": "SECPOLLCODE01", "answers": {"motion": {"nested": "obj"}}},
    {"code": "SECPOLLCODE01", "answers": {"officers": ["AA"] * 5000}},
    {"code": "SECPOLLCODE01", "answers": {"motion": "  "}},
    {"answers": {"motion": "YES"}, "info": {"first": "\x00", "last": "x",
                                            "chapter": "x", "emails": "a@b.co"}},
]
for path in ("/p/secpoll/vote", "/p/secpoll/provisional", "/p/secpoll/resend",
             "/prefs/optout", "/prefs/optin"):
    for body in HOSTILE_BODIES:
        r = c.post(path, json=body)
        ok(r.status_code < 500, f"[robust] {path} no 5xx on {str(body)[:44]}")
    r = c.post(path, data="not json at all", content_type="application/json")
    ok(r.status_code < 500, f"[robust] {path} no 5xx on non-JSON body")

for path in ("/p/secpoll/voted?code=%00", "/p/secpoll/voted?code=" + "A" * 500,
             "/p/secpoll/verify?receipt=" + "Z" * 300,
             "/p/secpoll/verify?receipt=../../x",
             "/p/secpoll/verify/../../../etc/passwd",
             "/p/secpoll/verify/evil.blt", "/p/secpoll/rollcall/nosuch.csv",
             "/p/%2e%2e/", "/p/secpoll/v/" + "A" * 500):
    ok(c.get(path).status_code < 500, f"[robust] GET {path[:52]} no 5xx")

# poll ids that look like traversal or injection resolve to 404, never 5xx
for pid in ("../config__polls", "%2e%2e%2f", "a" * 300, "'; DROP--"):
    ok(c.get(f"/p/{pid}/").status_code in (404, 400, 301, 308),
       f"[robust] hostile poll_id {pid[:24]!r} rejected cleanly")


# =========================================================================
# D. BALLOT SECRECY
# =========================================================================
section("D. secret-ballot content stays out of reachable outputs")

SECRET_MARKERS = ("Ada", "AA")   # the secret contest's option id / name


def body_of(resp):
    return resp.data.decode(errors="replace")


# chapter admin (in scope) must not get the raw secret ballots
ok(c.get("/admin/api/polls/secpoll/blt/officers",
         headers=CHAPTER_OWN).status_code == 403,
   "chapter admin denied the secret contest's raw BLT")
zc = c.get("/admin/api/polls/secpoll/export.zip", headers=CHAPTER_OWN)
if zc.status_code == 200:
    names = set(zipfile.ZipFile(io.BytesIO(zc.data)).namelist())
    ok("officers.blt" not in names,
       "chapter export package omits the secret contest ballots")
    ok("motion.blt" in names or "roll.blt" in names,
       "chapter export still contains the non-secret contests")

# the public ballots.csv never carries secret answers
pubcsv = body_of(c.get("/p/secpoll/verify/ballots.csv"))
ok("officers" not in pubcsv, "public ballots.csv has no secret-question column")

# the roll call is only ever served for `public` questions
ok(c.get("/p/secpoll/rollcall/officers.csv").status_code == 404,
   "roll call refuses a secret question")
ok(c.get("/p/secpoll/rollcall/motion.csv").status_code == 404,
   "roll call refuses a `named` question")

# admin lookup never returns secret content, in any mode
DOCS[("config__polls", "secpoll")]["admin_sees_answers"] = True
vote_service.load_polls(force=True)
lk = c.get("/admin/api/polls/secpoll/lookup?member_id=AK-SEC-1", headers=NATIONAL)
lkb = body_of(lk)
ok(lk.status_code == 200, "national lookup works")
ok('"officers"' not in json.dumps((lk.get_json() or {}).get("ballots", [{}])[0]
                                  .get("answers", {})),
   "lookup never returns secret answers even with admin_sees_answers")
DOCS[("config__polls", "secpoll")].pop("admin_sees_answers", None)
vote_service.load_polls(force=True)

# the secret record itself carries no identity
sec_docs = [d for (coll, _), d in DOCS.items() if coll == "secpoll__delegate_ballots"]
ok(sec_docs, "a secret ballot record exists")
for d in sec_docs:
    ok("member_id" not in d and "chapter" not in d,
       "secret ballot record carries no member_id/chapter")


# =========================================================================
# E. VOTE INTEGRITY
# =========================================================================
section("E. vote integrity")

vote_service._claim_code = _orig_claim
DOCS[("intpoll__codes", vote_service.code_hash("INTEGRITYCODE1"))] = {
    "used": False, "member_id": "AK-INT", "chapter": "X"}
c.post("/admin/api/polls/intpoll", headers=NATIONAL,
       json={"name": "Integrity", "test_code": "INTTESTCODE001",
             "questions": [{"key": "m", "type": "yesno", "title": "M?"}]})
vote_service.load_polls(force=True)
r1 = c.post("/p/intpoll/vote", json={"code": "INTEGRITYCODE1", "answers": {"m": "YES"}})
r2 = c.post("/p/intpoll/vote", json={"code": "INTEGRITYCODE1", "answers": {"m": "NO"}})
ok(r1.status_code == 200 and r1.get_json().get("status") == "recorded",
   "first vote records")
ok(r2.get_json().get("status") == "already_voted", "a code cannot vote twice")
ok(c.post("/p/intpoll/vote",
          json={"code": "NEVERISSUEDCODE", "answers": {"m": "YES"}}).status_code == 404,
   "an unissued code cannot vote")

before = len([1 for (coll, _) in DOCS if coll == "intpoll__ballots"])
c.post("/p/intpoll/vote", json={"code": "INTTESTCODE001", "answers": {"m": "YES"}})
after = len([1 for (coll, _) in DOCS if coll == "intpoll__ballots"])
ok(before == after, "test-code votes are never stored")

DOCS[("config__polls", "intpoll")]["closes_at"] = 1
vote_service.load_polls(force=True)
DOCS[("intpoll__codes", vote_service.code_hash("INTEGRITYCODE2"))] = {
    "used": False, "member_id": "AK-INT2", "chapter": "X"}
ok(c.post("/p/intpoll/vote",
          json={"code": "INTEGRITYCODE2", "answers": {"m": "YES"}}).status_code == 403,
   "a closed poll refuses votes")

# weights cannot be pushed out of bounds
for w in (0, -5, 10 ** 9, "abc", None):
    rr = c.post("/admin/api/polls/secpoll/voters/weight", headers=NATIONAL,
                json={"member_id": "AK-SEC-1", "weight": w})
    ok(rr.status_code >= 400, f"out-of-range weight {w!r} refused")


# =========================================================================
# F. ABUSE RESISTANCE
# =========================================================================
section("F. abuse resistance")


def ip_for(xff, peer="198.51.100.7"):
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": peer},
                                  headers=({"X-Forwarded-For": xff} if xff else {})):
        return vote_service._client_ip()


ok(ip_for("1.2.3.4, 198.51.100.7") == "198.51.100.7",
   "spoofed X-Forwarded-For prefix ignored")
ok(ip_for("a, b, c, d, 198.51.100.7") == "198.51.100.7",
   "many spoofed hops cannot walk past the trusted one")
ok(ip_for(None) == "198.51.100.7", "no XFF falls back to the peer")

vote_service._prov_hits.clear()
rotating = sum(vote_service._prov_throttled(ip_for(f"spoof{i}, 198.51.100.7"))
               for i in range(vote_service.PROV_MAX_PER_IP * 3))
vote_service._prov_hits.clear()
ok(rotating > 0, "rotating X-Forwarded-For cannot bypass the per-IP throttle")

section("F2. warehouse identifiers cannot escape the interpolated query")
# `chapter` is a bound parameter, but the TABLE REFERENCE is interpolated, so
# roll_project/roll_dataset must never carry anything that terminates the
# backtick quoting. A backtick in an accepted value is the whole ballgame.
_R = vote_service.GCP_IDENT_RE
for good in ("rosavote-app", "proj-tmc-mem-dsa", "main", "my_dataset_1",
             "a--b", "example:legacy"):
    ok(_R.match(good), f"legitimate GCP identifier accepted: {good!r}")
for bad in ("main`.members` UNION ALL SELECT email FROM `hr.x` --", "x`",
            "a b", "a;DROP", "a'b", "a)b", "`", "a`.b`", "a/*x*/b", "a\nb"):
    ok(not _R.match(bad), f"injection attempt rejected: {bad[:34]!r}")
ok(not any(_R.match(v) for v in ("`", "a`", "`a", "a`b")),
   "no accepted identifier can contain a backtick")
for field in ("roll_project", "roll_dataset"):
    rr = c.post("/admin/api/polls/secpoll/voters/import_bigquery", headers=NATIONAL,
                json={"chapter": "X", "roll_project": "p", "roll_dataset": "d",
                      field: "evil`.t` UNION ALL SELECT 1 --"})
    ok(rr.status_code == 400,
       f"import_bigquery rejects an injected {field} before querying")

ok(app.config.get("MAX_CONTENT_LENGTH"), "a request body cap is configured")
# needs an OPEN poll: a closed/finalized one answers 403 before the body is
# ever read, which would pass this check for the wrong reason.
c.post("/admin/api/polls/bodycap", headers=NATIONAL,
       json={"name": "Body cap", "questions": [{"key": "m", "type": "yesno",
                                                "title": "M?"}]})
vote_service.load_polls(force=True)
ok(vote_service.window_state(vote_service.chapter_or_none("bodycap")) == "open",
   "body-cap fixture poll is open")
big = c.post("/p/bodycap/vote", data="x" * (app.config["MAX_CONTENT_LENGTH"] + 1024),
             content_type="application/json")
ok(big.status_code in (400, 413) and big.status_code < 500,
   f"oversized body rejected, not buffered (got {big.status_code})")

# enumeration-safety: identical response whether or not a contact is known
a = c.post("/p/secpoll/resend", json={"contact": "definitely-not-a-member@example.com"})
b = c.post("/p/secpoll/resend", json={"contact": "someone-else@example.com"})
ok(a.status_code == b.status_code and a.data == b.data,
   "resend is enumeration-safe (identical response for known/unknown contacts)")
o1 = c.post("/prefs/optout", json={"contact": "a@example.com"})
o2 = c.post("/prefs/optout", json={"contact": "zzz@example.com"})
ok(o1.status_code == o2.status_code and o1.data == o2.data,
   "opt-out is enumeration-safe")

# security headers on every response
h = c.get("/").headers
for header, want in (("Content-Security-Policy", "connect-src 'self'"),
                     ("X-Content-Type-Options", "nosniff"),
                     ("X-Frame-Options", "DENY"),
                     ("Referrer-Policy", "same-origin")):
    ok(want in (h.get(header) or ""), f"{header} set ({want})")
ok("frame-ancestors 'none'" in (h.get("Content-Security-Policy") or ""),
   "CSP forbids framing")


# =========================================================================
print()
if failures:
    print(f"SECURITY SUITE: {passed} passed, {len(failures)} FAILED")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"SECURITY SUITE: all {passed} checks passed")
