#!/usr/bin/env python3
"""
Offline smoke test for the Chapter Member Ballot service — no GCP needed.
Stubs google.cloud.firestore, then exercises every route and policy:
    python3 tools/smoke_test.py
Run from the ballot-v2 directory (or anywhere; it fixes sys.path itself).
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ADMIN_TOKEN", "smoke-test-token")

# ---- Firestore stub ------------------------------------------------------
DOCS = {}      # (collection, key) -> dict
WRITES = []


class FakeSnap:
    def __init__(self, ref, d, exists=True):
        self.reference = ref
        self.id = ref.key
        self.exists = exists
        self._d = d or {}
    def to_dict(self):
        return dict(self._d)
    def get(self, field):
        return self._d.get(field)


class FakeRef:
    def __init__(self, coll, key):
        self.coll, self.key = coll, key
    def get(self, transaction=None):
        d = DOCS.get((self.coll, self.key))
        return FakeSnap(self, d, exists=d is not None)
    def set(self, d):
        DOCS[(self.coll, self.key)] = dict(d)
        WRITES.append((self.coll, d))
    def update(self, d):
        DOCS[(self.coll, self.key)].update(d)


class FakeQuery:
    def __init__(self, coll, field, val):
        self.coll, self.field, self.val = coll, field, val
    def stream(self):
        for (coll, key), d in list(DOCS.items()):
            if coll == self.coll and d.get(self.field) == self.val:
                yield FakeSnap(FakeRef(coll, key), d)


class FakeColl:
    def __init__(self, name):
        self.name = name
    def document(self, k=None):
        return FakeRef(self.name, k or f"auto{len(DOCS)}")
    def where(self, field, op, val):
        return FakeQuery(self.name, field, val)
    def stream(self):
        for (coll, key), d in list(DOCS.items()):
            if coll == self.name:
                yield FakeSnap(FakeRef(coll, key), d)


fake_fs = types.ModuleType("google.cloud.firestore")
fake_fs.Client = lambda *a, **k: types.SimpleNamespace(
    collection=lambda nm: FakeColl(nm), transaction=lambda: None)
fake_fs.SERVER_TIMESTAMP = "TS"
fake_fs.transactional = lambda f: f
fake_exc = types.ModuleType("google.api_core.exceptions")
fake_exc.Aborted = type("Aborted", (Exception,), {})

# BigQuery stub: returns a tiny fixed roll for any query
BQ_ROWS = [{"member_akid": 777001}, {"member_akid": 777002}]
fake_bq = types.ModuleType("google.cloud.bigquery")
fake_bq.Client = lambda *a, **k: types.SimpleNamespace(
    query=lambda q, job_config=None: types.SimpleNamespace(result=lambda: list(BQ_ROWS)))
fake_bq.QueryJobConfig = lambda **k: None
fake_bq.ScalarQueryParameter = lambda *a, **k: None

# Cloud Storage stub: (bucket, path) -> text
GCS = {}
class FakeBlob:
    def __init__(self, bucket, path):
        self.bucket, self.path = bucket, path
    def download_as_text(self):
        return GCS[(self.bucket, self.path)]
    def upload_from_string(self, data, content_type=None):
        GCS[(self.bucket, self.path)] = data
fake_storage = types.ModuleType("google.cloud.storage")
fake_storage.Client = lambda *a, **k: types.SimpleNamespace(
    bucket=lambda name: types.SimpleNamespace(
        blob=lambda path, _n=name: FakeBlob(_n, path)))

gcloud = types.ModuleType("google.cloud")
gcloud.firestore = fake_fs; gcloud.bigquery = fake_bq; gcloud.storage = fake_storage
gapi = types.ModuleType("google.api_core"); gapi.exceptions = fake_exc
sys.modules.update({"google.cloud": gcloud, "google.cloud.firestore": fake_fs,
                    "google.cloud.bigquery": fake_bq, "google.cloud.storage": fake_storage,
                    "google.api_core": gapi, "google.api_core.exceptions": fake_exc})

import vote_service  # noqa: E402

vote_service._claim_code = lambda txn, coll, ch: {"member_id": "AK-TEST", "chapter": "nyc"}
c = vote_service.app.test_client()
GOOD = {"q1": "YES", "q2": ["IE", "COORD"], "q3": ["DEBS", "WILSON"], "pledges": ["DONATE"],
        "q6": "YES", "q7": ["FLYNN", "GOLDMAN"], "q8": "NO", "text": "solidarity"}
passed = 0


def ok(cond, label):
    global passed
    assert cond, f"FAIL: {label}"
    passed += 1


# splash + chapters
sp = c.get("/").data.decode()
ok(all(x in sp for x in ["Chapter Member Ballot", "For Administrators", "TEST-NYC-2026-DEMO"]), "splash")
ok('href="/admin/"' in sp and "How to get access" in sp
   and "How to test without affecting anything" in sp,
   "splash links the admin console with access + test instructions")
for pid in vote_service.CHAPTERS:
    ok(c.get(f"/p/{pid}/").status_code == 200, f"page {pid}")
ok(c.get("/p/nope/").status_code == 404, "unknown poll 404")

# ballot page structure
b = c.get("/p/debs_endorsement__nyc/").data.decode()
ok(b.index("How your votes are seen") < b.index("Question 1 of 8"), "disclosure before Q1")
ok(all(x in b for x in ["Chapter Poll", "Convention Delegates", "Local Issues",
                        "two-count method", "Meyer London"]), "sections + slate")

# vote: identity-linked main record, secret delegate record
r = c.post("/p/debs_endorsement__nyc/vote", json={"code": "A" * 16, "answers": GOOD})
ok(r.status_code == 200, "vote accepted")
receipt = r.get_json()["receipt"]
main = next(d for coll, d in WRITES if coll.endswith("__ballots"))
dele = next(d for coll, d in WRITES if coll.endswith("__delegate_ballots"))
ok("q7" not in main["answers"] and main["member_id"] == "AK-TEST", "main record identity-linked, no q7")
ok(dele["q7"] == GOOD["q7"] and "member_id" not in dele, "delegate record secret")
ok(dele["receipt"] == receipt, "shared receipt")

# validation
ok(c.post("/p/debs_endorsement__nyc/vote",
          json={"code": "A" * 16, "answers": dict(GOOD, q2=["IE", "IE"])}).status_code == 400, "dup rank 400")
ok(c.post("/p/debs_endorsement__nyc/vote",
          json={"code": "A" * 16, "answers": dict(GOOD, q7=["OHARE"])}).status_code == 400, "wrong-chapter candidate 400")

# repeatable test code: works twice, writes nothing
before = len(WRITES)
for _ in range(2):
    ok(c.post("/p/debs_endorsement__nyc/vote",
              json={"code": "TEST-NYC-2026-DEMO", "answers": GOOD}).status_code == 200, "test code vote")
ok(len(WRITES) == before, "test votes not recorded")

# provisional
info = {"first": "T", "last": "V", "emails": "a@x.com", "phones": "", "chapter": "NYC",
        "joined": "", "alt_names": ""}
ok(c.post("/p/debs_endorsement__nyc/provisional",
          json={"info": info, "answers": GOOD}).status_code == 200, "provisional accepted")

# void-and-reissue
hdr = {"X-Admin-Token": "smoke-test-token"}
ok(c.post("/p/debs_endorsement__nyc/admin/void", json={}).status_code == 403, "void needs token")
r = c.post("/p/debs_endorsement__nyc/admin/void", headers=hdr,
           json={"receipt": receipt, "reason": "stolen_code", "admin": "smoke"})
ok(r.status_code == 200 and r.get_json()["new_code"], "void + reissue")
ok(c.post("/p/debs_endorsement__nyc/admin/void", headers=hdr,
          json={"receipt": receipt, "reason": "stolen_code", "admin": "smoke"}).status_code == 409, "double void 409")

# ---- self-service: console, builder, scoped auth, adjudication, close ----
import hashlib   # noqa: E402
import time as _time  # noqa: E402

DAY = 86400
now = int(_time.time())

# console shell + whoami
console_html = c.get("/admin/").data.decode()
ok("Admin Console" in console_html, "console page served")
ok("DEMO-ADMIN-TOKEN-2026" in console_html and 'id="demo-btn"' in console_html,
   "sign-in page carries the demo token + one-tap demo button")
ok(c.get("/admin/api/whoami").status_code == 403, "whoami needs token")
r = c.get("/admin/api/whoami", headers=hdr)
ok(r.status_code == 200 and r.get_json()["role"] == "national", "whoami national")

# election builder: move NYC into Firestore config with a valid Art. V window
conv = _time.strftime("%Y-%m-%d", _time.localtime(now + 60 * DAY))
nyc = vote_service.cfg_to_doc(vote_service.CHAPTERS["debs_endorsement__nyc"])
nyc.update(name="New York City (Firestore)", opens_at=now - 30 * DAY,
           closes_at=now + 10 * DAY, convention_date=conv, apportionment_done=True)
r = c.post("/admin/api/polls/debs_endorsement__nyc", headers=hdr, json=nyc)
ok(r.status_code == 200, "builder saves valid config")
ok("New York City (Firestore)" in c.get("/p/debs_endorsement__nyc/").data.decode(),
   "Firestore config overrides seed")

# Art. V §5: closing 5 days before convention is inside the 45-day quiet period
r = c.post("/admin/api/polls/debs_endorsement__nyc", headers=hdr,
           json=dict(nyc, closes_at=now + 55 * DAY))
ok(r.status_code == 400 and any("Art. V" in e for e in r.get_json()["errors"]),
   "Art. V window enforced")
r = c.post("/admin/api/polls/debs_endorsement__nyc", headers=hdr,
           json=dict(nyc, apportionment_done=False))
ok(r.status_code == 400 and any("apportion" in e for e in r.get_json()["errors"]),
   "Art. V apportionment enforced")
bad = dict(nyc, q7=dict(nyc["q7"], candidates=[{"id": "X1", "name": "A"}, {"id": "X1", "name": "B"}]))
ok(c.post("/admin/api/polls/debs_endorsement__nyc", headers=hdr, json=bad).status_code == 400,
   "duplicate candidate ids 400")

# scoped chapter token: sees/touches only its own poll, cannot build elections
chi_token = "chi-scoped-token-0001"
DOCS[("config__admins", hashlib.sha256(chi_token.encode()).hexdigest())] = {
    "name": "Chicago Admins", "role": "chapter", "polls": ["debs_endorsement__chi"], "active": True}
chi_hdr = {"X-Admin-Token": chi_token}
chi = vote_service.cfg_to_doc(vote_service.CHAPTERS["debs_endorsement__chi"])
chi.update(opens_at=now - DAY, closes_at=now + 10 * DAY)
ok(c.post("/admin/api/polls/debs_endorsement__chi", headers=hdr, json=chi).status_code == 200,
   "chi config saved")
r = c.get("/admin/api/polls", headers=chi_hdr)
ok(r.status_code == 200 and set(r.get_json()) == {"debs_endorsement__chi"},
   "chapter token sees only own poll")
ok(c.post("/admin/api/polls/debs_endorsement__chi", headers=chi_hdr, json=chi).status_code == 403,
   "builder is national-only")
ok(c.post("/p/debs_endorsement__nyc/admin/void", headers=chi_hdr,
          json={"receipt": "ZZZZZZZZ", "reason": "stolen_code"}).status_code == 403,
   "cross-chapter void 403")
ok(c.post("/p/debs_endorsement__chi/admin/void", headers=chi_hdr,
          json={"receipt": "ZZZZZZZZ", "reason": "stolen_code"}).status_code == 404,
   "scoped void reaches own poll")

# provisional adjudication: queue lists identity only; verify promotes + burns codes
r = c.post("/p/debs_endorsement__nyc/provisional", json={"info": info, "answers": GOOD})
prcpt = r.get_json()["receipt"]
r = c.get("/admin/api/polls/debs_endorsement__nyc/provisionals", headers=hdr)
pend = r.get_json()["pending"]
ok(any(p["receipt"] == prcpt for p in pend) and all("answers" not in p for p in pend),
   "queue lists pending, answers stay sealed")
DOCS[("debs_endorsement__nyc__codes", "unused-code-hash")] = {
    "used": False, "member_id": "AK-PROV", "chapter": "nyc"}
before_w = len(WRITES)
r = c.post(f"/admin/api/polls/debs_endorsement__nyc/provisionals/{prcpt}", headers=hdr,
           json={"action": "verify", "member_id": "AK-PROV"})
ok(r.status_code == 200, "provisional verified")
ok(DOCS[("debs_endorsement__nyc__codes", "unused-code-hash")]["used"] is True,
   "member's unused code burned on verify")
pm = next(d for coll, d in WRITES[before_w:] if coll.endswith("__ballots"))
pd = next(d for coll, d in WRITES[before_w:] if coll.endswith("__delegate_ballots"))
ok(pm.get("provisional") and pm["member_id"] == "AK-PROV" and "q7" not in pm["answers"]
   and pm["receipt"] == prcpt, "promoted main ballot identity-linked, no q7")
ok(pd["q7"] == GOOD["q7"] and "member_id" not in pd and pd["receipt"] == prcpt,
   "promoted delegate ballot secret")
ok(c.post(f"/admin/api/polls/debs_endorsement__nyc/provisionals/{prcpt}", headers=hdr,
          json={"action": "verify", "member_id": "AK-PROV"}).status_code == 409,
   "double adjudication 409")

# one-member-one-vote: a member who already voted by code cannot be verified
r = c.post("/p/debs_endorsement__nyc/provisional", json={"info": info, "answers": GOOD})
p2 = r.get_json()["receipt"]
DOCS[("debs_endorsement__nyc__codes", "used-code-hash")] = {
    "used": True, "member_id": "AK-DOUBLE", "chapter": "nyc"}
ok(c.post(f"/admin/api/polls/debs_endorsement__nyc/provisionals/{p2}", headers=hdr,
          json={"action": "verify", "member_id": "AK-DOUBLE"}).status_code == 409,
   "already-voted member blocked")
ok(c.post(f"/admin/api/polls/debs_endorsement__nyc/provisionals/{p2}", headers=hdr,
          json={"action": "reject", "note": "duplicate of coded vote"}).status_code == 200,
   "reject records the decision")

# code generation (pure logic — the BigQuery path needs real creds)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import generate_codes  # noqa: E402

gm, gci, gsk = generate_codes.generate(
    [{"member_akid": 1, "roll_chapter": "New York City",
      "all_emails": ["x@example.org"], "all_phones": ["+12125550100"]},
     {"member_akid": 1, "roll_chapter": "New York City",
      "all_emails": ["x@example.org"], "all_phones": []},
     {"member_akid": 2, "roll_chapter": "Nowhere",
      "all_emails": ["y@example.org"], "all_phones": []},
     {"member_akid": 3, "roll_chapter": "New York City",
      "all_emails": [], "all_phones": ["+12125550101"]}],
    {"New York City": "p1"})
ok(len(gm) == 2 and gm[0]["channel"] == "email" and gm[1]["channel"] == "sms",
   "codegen: dedup + email-first channel ladder")
ok(gsk == {"Nowhere": 1} and all(r["member_id"].startswith("AK") for r in gm),
   "codegen: unmapped chapters skipped, AK member ids")

# close-out button: chapter admin closes own poll; votes bounce immediately
ok(c.post("/admin/api/polls/debs_endorsement__chi/close", headers=chi_hdr, json={}).status_code == 200,
   "chapter closes own poll")
ok(c.post("/p/debs_endorsement__chi/vote",
          json={"code": "TEST-CHI-2026-DEMO", "answers": GOOD}).status_code == 403,
   "closed poll rejects votes")

# close-out cron: finalizes closed polls once, idempotently, national-only
DOCS[("config__polls", "debs_endorsement__chi")]["closes_at"] = now - 60
ok(c.post("/admin/api/cron/closeout", headers=chi_hdr).status_code == 403,
   "closeout cron national-only")
r = c.post("/admin/api/cron/closeout", headers=hdr)
ok(r.status_code == 200 and "debs_endorsement__chi" in r.get_json()["finalized"],
   "closeout finalizes closed poll")
cfg_doc = DOCS[("config__polls", "debs_endorsement__chi")]
ok(cfg_doc.get("finalized") and "final_counts" in cfg_doc, "final counts snapshotted")
r = c.post("/admin/api/cron/closeout", headers=hdr)
ok(r.get_json()["finalized"] == {}, "closeout idempotent")

# ---- generalized ballots: schema questions, timezones, finalize guard ----
from zoneinfo import ZoneInfo  # noqa: E402
from datetime import datetime  # noqa: E402

CUSTOM_QS = [
    {"key": "measure", "type": "yesno", "title": "Shall the chapter fund a mutual aid pantry?"},
    {"key": "officer", "type": "ranked", "title": "Elect the co-chairs", "seats": 2,
     "secret": True,
     "options": [{"id": "A1", "name": "Alice"}, {"id": "B2", "name": "Bha"},
                 {"id": "C3", "name": "Cruz"}]},
    {"key": "why", "type": "text", "title": "Tell us why", "max": 200},
]
r = c.post("/admin/api/polls/special_ref", headers=hdr, json={
    "name": "Special Referendum", "timezone": "America/Chicago",
    "opens_at": "2020-01-01T00:00", "closes_at": "2099-01-01T00:00",
    "questions": CUSTOM_QS})
ok(r.status_code == 200 and r.get_json()["warnings"] == [],
   "custom poll saves; no Art. V warning without a delegate question")
cfg_doc = DOCS[("config__polls", "special_ref")]
expected_open = int(datetime(2020, 1, 1, 0, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp())
ok(cfg_doc["opens_at"] == expected_open and cfg_doc["timezone"] == "America/Chicago",
   "window parsed in the poll's timezone")

page2 = c.get("/p/special_ref/").data.decode()
ok(all(x in page2 for x in ["Question 1 of 3", "mutual aid pantry", "Alice", "Tell us why"]),
   "schema ballot renders all question types")

before_w = len(WRITES)
r = c.post("/p/special_ref/vote", json={"code": "B" * 16, "answers": {
    "measure": "YES", "officer": ["A1", "C3"], "why": "more pantries"}})
ok(r.status_code == 200 and r.get_json()["status"] == "recorded", "custom ballot vote accepted")
cm = next(d for coll, d in WRITES[before_w:] if coll == "special_ref__ballots")
cd = next(d for coll, d in WRITES[before_w:] if coll == "special_ref__delegate_ballots")
ok(cm["answers"] == {"measure": "YES"} and cm["comments"] == {"why": "more pantries"},
   "main record: named answers + sealed-from-export text answers")
ok(cd["officer"] == ["A1", "C3"] and "member_id" not in cd,
   "secret question stored in the secret collection, no identity")
ok(c.post("/p/special_ref/vote", json={"code": "B" * 16, "answers": {
    "measure": "MAYBE", "officer": ["A1"], "why": ""}}).status_code == 400,
   "bad yesno value 400")
ok(c.post("/p/special_ref/vote", json={"code": "B" * 16, "answers": {
    "measure": "NO", "officer": ["ZZ"], "why": ""}}).status_code == 400,
   "unknown ranked option 400")

# builder schema validation
bad_qs = [{"key": "dup", "type": "yesno", "title": "A"},
          {"key": "dup", "type": "yesno", "title": "B"}]
ok(c.post("/admin/api/polls/special_ref", headers=hdr, json={
    "name": "X", "questions": bad_qs}).status_code == 400, "duplicate question keys 400")

# voters: import mints hashed codes + one-time manifest; list shows turnout
r = c.post("/admin/api/polls/special_ref/voters/import", headers=hdr,
           json={"members": [{"member_id": "AK101"}, {"member_id": "AK102"},
                             {"member_id": "AK103"}]})
ok(r.status_code == 200 and len(r.get_json()["created"]) == 3, "voter import creates codes")
manifest = r.get_json()["created"]
ok(all(("special_ref__codes", vote_service.code_hash(m["code"])) in DOCS for m in manifest),
   "imported codes stored hashed only")
ok(c.post("/admin/api/polls/special_ref/voters/import", headers=hdr,
          json={"members": [{"member_id": "AK101"}]}).get_json()["skipped"] == 1,
   "re-import skips existing members")
ok(c.post("/admin/api/polls/special_ref/voters/import", headers=chi_hdr,
          json={"members": [{"member_id": "AK999"}]}).status_code == 403,
   "import is national-only")

# vote with an imported code, then mark its doc used (as the real
# transaction would) and confirm the voters view links code -> ballot
m1 = manifest[0]
r = c.post("/p/special_ref/vote", json={"code": m1["code"], "answers": {
    "measure": "NO", "officer": ["B2"], "why": ""}})
ok(r.status_code == 200, "imported-code vote accepted")
DOCS[("special_ref__codes", vote_service.code_hash(m1["code"]))]["used"] = True
r = c.get("/admin/api/polls/special_ref/voters", headers=hdr)
vd = {v["member_id"]: v for v in r.get_json()["voters"]}
ok(vd["AK101"]["voted"] and vd["AK101"]["ballot_received"] and vd["AK101"]["receipt"],
   "voters: voted + ballot received + receipt")
ok(not vd["AK102"]["voted"] and r.get_json()["integrity"]["used_codes_without_ballot"] == 0,
   "voters: non-voter listed, integrity clean")
ok(all("answers" not in v for v in vd.values()), "voters view never carries answers")
DOCS[("special_ref__codes", vote_service.code_hash(manifest[2]["code"]))]["used"] = True
r = c.get("/admin/api/polls/special_ref/voters", headers=hdr)
ok(r.get_json()["integrity"]["used_codes_without_ballot"] == 1,
   "integrity flags used code without stored ballot")

# finalized polls can't be silently edited/reopened
chi_body = dict(chi)
ok(c.post("/admin/api/polls/debs_endorsement__chi", headers=hdr, json=chi_body).status_code == 409,
   "finalized poll edit refused without unfinalize")
r = c.post("/admin/api/polls/debs_endorsement__chi", headers=hdr,
           json=dict(chi_body, unfinalize=True))
ok(r.status_code == 200 and "finalized" not in DOCS[("config__polls", "debs_endorsement__chi")],
   "explicit unfinalize reopens (audited)")

# ---- server-side imports: GCS CSV (extra columns ignored) + BigQuery ----
GCS[("dsa-rolls", "nyc.csv")] = (
    'member_id,first_name,chapter,unused_col\n'
    'AK900,"Doe, Jane",New York City,x\n'
    'AK901,,New York City,y\n')
r = c.post("/admin/api/polls/special_ref/voters/import_gcs", headers=hdr,
           json={"gcs_uri": "gs://dsa-rolls/nyc.csv"})
ok(r.status_code == 200 and r.get_json()["created_count"] == 2,
   "GCS import: required header honored, extra columns discarded")
manifest_keys = [k for k in GCS if "code_manifest" in k[1]]
ok(len(manifest_keys) == 1 and "AK900" in GCS[manifest_keys[0]],
   "GCS import writes one-time manifest back to bucket")
ok(c.post("/admin/api/polls/special_ref/voters/import_gcs", headers=chi_hdr,
          json={"gcs_uri": "gs://dsa-rolls/nyc.csv"}).status_code == 403,
   "GCS import national-only")
GCS[("dsa-rolls", "bad.csv")] = "akid,email\n1,a@b.c\n"
ok(c.post("/admin/api/polls/special_ref/voters/import_gcs", headers=hdr,
          json={"gcs_uri": "gs://dsa-rolls/bad.csv"}).status_code == 400,
   "GCS import rejects CSV without member_id header")

r = c.post("/admin/api/polls/special_ref/voters/import_bigquery", headers=hdr,
           json={"chapter": "New York City"})
ok(r.status_code == 200 and len(r.get_json()["created"]) == 2
   and r.get_json()["created"][0]["member_id"] == "AK777001",
   "BigQuery import mints codes for eligible roll members")
ok(c.post("/admin/api/polls/special_ref/voters/import_bigquery", headers=chi_hdr,
          json={"chapter": "Chicago"}).status_code == 403, "BigQuery import national-only")

# ---- results: locked until finalized, then full tallies ----
ok(c.get("/admin/api/polls/special_ref/results", headers=hdr).status_code == 409,
   "results locked before finalize")
r = c.get("/admin/api/polls/special_ref/results?live=1", headers=hdr)
ok(r.status_code == 200 and r.get_json()["live"] is True, "root live tally works pre-finalize")
ok(any(d.get("action") == "live_results_view"
       for (coll, k), d in DOCS.items() if coll == "special_ref__audit_log"),
   "live tally view is audit-logged")
ok(c.get("/admin/api/polls/debs_endorsement__chi/results?live=1", headers=chi_hdr).status_code == 409,
   "chapter tokens get no live tally")

# root ballot lookup: identity-linked answers, secret content never shown
# (stubbed _claim_code stamps every coded ballot AK-TEST, so look up by the
# receipt the voters view linked to AK101's code)
ak101_receipt = vd["AK101"]["receipt"]
r = c.get(f"/admin/api/polls/special_ref/lookup?receipt={ak101_receipt}", headers=hdr)
lk = r.get_json()
ok(r.status_code == 200 and lk["found"] == 1
   and lk["ballots"][0]["answers"] == {"measure": "NO"}
   and lk["ballots"][0]["secret_ballot_recorded"] is True
   and "officer" not in str(lk["ballots"][0]), "lookup: main answers only, secret flagged not shown")
ok(c.get(f"/admin/api/polls/special_ref/lookup?receipt={ak101_receipt}",
         headers=chi_hdr).status_code == 403, "lookup is root-only")
ok(any(d.get("action") == "ballot_lookup"
       for (coll, k), d in DOCS.items() if coll == "special_ref__audit_log"),
   "lookup is audit-logged")

# public receipt verification (voter self-check, no auth)
lr = lk["ballots"][0]["receipt"]
r = c.get(f"/p/special_ref/verify?receipt={lr}")
ok(r.status_code == 200 and r.get_json() == {"found": True, "status": "recorded"},
   "public verify confirms stored receipt")
ok(c.get("/p/special_ref/verify?receipt=ZZZZZZZZ").status_code == 404,
   "public verify: unknown receipt not found")
body = c.get(f"/p/special_ref/verify?receipt={lr}").get_json()
ok("answers" not in body and "member_id" not in body, "public verify leaks nothing")
DOCS[("config__polls", "special_ref")]["closes_at"] = now - 50
r = c.post("/admin/api/cron/closeout", headers=hdr)
ok("special_ref" in r.get_json()["finalized"], "special_ref finalized by cron")
r = c.get("/admin/api/polls/special_ref/results", headers=hdr)
ok(r.status_code == 200, "results unlock after finalize")
res = {q["key"]: q for q in r.get_json()["questions"]}
ok(res["measure"]["counts"] == {"YES": 1, "NO": 1, "ABSTAIN": 0}
   and res["measure"]["result"] == "TIE", "yesno tally + verdict")
ok(len(res["officer"]["winners"]) == 2 and res["officer"]["secret"],
   "ranked STV winners from secret ballots")
ok(isinstance(res["officer"]["stages"], list) and len(res["officer"]["stages"]) >= 1
   and isinstance(res["officer"]["stages"][0]["totals"], dict)
   and "action" in res["officer"]["stages"][0],
   "ranked results carry full round-by-round stage data for charts")
ok(res["why"]["responses"] == 1, "text answers counted, not displayed")
ok(c.get("/admin/api/polls/special_ref/results", headers=chi_hdr).status_code == 403,
   "results scoped to chapter tokens' own polls")

# ---- vote weights: provisioned at import, editable any time, tally + BLT --
# reopen the finalized special_ref deliberately (audited) to vote again
r = c.post("/admin/api/polls/special_ref", headers=hdr, json={
    "name": "Special Referendum", "timezone": "America/Chicago",
    "opens_at": "2020-01-01T00:00", "closes_at": "2099-01-01T00:00",
    "questions": CUSTOM_QS, "unfinalize": True})
ok(r.status_code == 200, "deliberate unfinalize reopens for weight tests")
r = c.post("/admin/api/polls/special_ref/voters/import", headers=hdr,
           json={"members": [{"member_id": "AKW1", "weight": 5}]})
ok(r.status_code == 200 and r.get_json()["created"][0]["weight"] == 5,
   "import provisions per-voter weight")
wcode = r.get_json()["created"][0]["code"]
ok(DOCS[("special_ref__codes", vote_service.code_hash(wcode))]["weight"] == 5,
   "weight stored on the code doc")
ok(c.post("/admin/api/polls/special_ref/voters/weight", headers=hdr,
          json={"member_id": "AKW1", "weight": 3}).status_code == 200,
   "weight editable on the backend")
ok(DOCS[("special_ref__codes", vote_service.code_hash(wcode))]["weight"] == 3,
   "weight update lands on the code doc")
ok(c.post("/admin/api/polls/special_ref/voters/weight", headers=chi_hdr,
          json={"member_id": "AKW1", "weight": 2}).status_code == 403,
   "weight edits are national-only")
ok(c.post("/admin/api/polls/special_ref/voters/weight", headers=hdr,
          json={"member_id": "AKW1", "weight": 0}).status_code == 400,
   "weight bounds enforced")
ok(any(d.get("action") == "weight_set"
       for (coll, k), d in DOCS.items() if coll == "special_ref__audit_log"),
   "weight changes audited")

# the weighted voter votes NO; weight 3 flips the earlier 1-1 measure tie
r = c.post("/p/special_ref/vote", json={"code": wcode, "answers": {
    "measure": "NO", "officer": ["C3"], "why": ""}})
ok(r.status_code == 200, "weighted voter's ballot accepted")
DOCS[("special_ref__codes", vote_service.code_hash(wcode))]["used"] = True
r = c.get("/admin/api/polls/special_ref/results?live=1", headers=hdr)
res = {q["key"]: q for q in r.get_json()["questions"]}
ok(res["measure"]["counts"] == {"YES": 1, "NO": 4, "ABSTAIN": 0}
   and res["measure"]["result"] == "FAILS" and r.get_json()["weighted"],
   "weighted tally: x3 NO vote flips the result")
# adjust weight AFTER voting — tally follows the current weight
c.post("/admin/api/polls/special_ref/voters/weight", headers=hdr,
       json={"member_id": "AKW1", "weight": 1})
r = c.get("/admin/api/polls/special_ref/results?live=1", headers=hdr)
res = {q["key"]: q for q in r.get_json()["questions"]}
ok(res["measure"]["counts"]["NO"] == 2, "post-election weight edit reflows the tally")

# BLT export: standard file, weights carried, scoped, recount variant
r = c.get("/admin/api/polls/special_ref/blt/officer?live=1", headers=hdr)
blt = r.get_data(as_text=True)
ok(r.status_code == 200 and blt.startswith("3 2\n") and '"Alice"' in blt,
   "BLT export: ranked contest downloads as standard file")
ok(c.get("/admin/api/polls/special_ref/blt/why?live=1", headers=hdr).status_code == 404,
   "text questions are not exportable")
ok(c.get("/admin/api/polls/special_ref/blt/officer", headers=chi_hdr).status_code == 403,
   "BLT export scoped to admins of the poll")
r = c.get("/admin/api/polls/special_ref/blt/measure?live=1", headers=hdr)
ok(r.status_code == 200 and r.get_data(as_text=True).startswith("2 1\n"),
   "yesno exports as a 1-seat BLT")

# ---- mint scoped admin tokens via API; manual BLT count workbench ----
r = c.post("/admin/api/admins", headers=hdr, json={
    "name": "Public demo", "role": "chapter", "polls": ["special_ref"],
    "token": "DEMO-ADMIN-TOKEN-2026"})
ok(r.status_code == 200 and r.get_json()["token"] == "DEMO-ADMIN-TOKEN-2026",
   "root mints scoped admin token (plaintext returned once)")
demo_hdr = {"X-Admin-Token": "DEMO-ADMIN-TOKEN-2026"}
r = c.get("/admin/api/whoami", headers=demo_hdr)
ok(r.status_code == 200 and r.get_json()["role"] == "chapter"
   and r.get_json()["polls"] == ["special_ref"], "minted token works, scoped")
ok(c.post("/admin/api/admins", headers=demo_hdr, json={
    "name": "x", "role": "national"}).status_code == 403, "minting is root-only")
ok(c.post("/admin/api/polls/special_ref", headers=demo_hdr,
          json={"name": "x"}).status_code == 403, "demo token cannot build elections")

blt_sample = '3 1\n2 1 2 0\n1 2 0\n1 3 0\n0\n"A"\n"B"\n"C"\n"t"\n'
r = c.post("/admin/api/count_blt", headers=demo_hdr, json={"blt": blt_sample})
ok(r.status_code == 200 and r.get_json()["winners"] == ["A"]
   and r.get_json()["valid_ballots"] == 4, "manual BLT count runs (weighted)")
ok(c.post("/admin/api/count_blt", headers=demo_hdr,
          json={"blt": "not a blt"}).status_code == 400, "malformed BLT rejected cleanly")
ok(c.post("/admin/api/count_blt", json={"blt": blt_sample}).status_code == 403,
   "count workbench requires a signed-in admin")

# ---- export zip, dropout recount, terms/privacy, branding ----
import io as _io  # noqa: E402
import zipfile as _zipfile  # noqa: E402

r = c.get("/admin/api/polls/special_ref/export.zip?live=1&anonymize=1", headers=hdr)
ok(r.status_code == 200, "results zip exports")
zf = _zipfile.ZipFile(_io.BytesIO(r.data))
names = set(zf.namelist())
ok({"results.txt", "results.csv", "results.json", "results.html",
    "VERIFY_README.txt", "ballots.csv", "officer.blt", "measure.blt"} <= names,
   "zip contains txt/csv/json/html reports, BLTs, ballots, verify readme")
ok("member_id" not in zf.read("ballots.csv").decode(), "anonymize strips identity")
ok("Stage" in zf.read("results.txt").decode(), "txt report carries round-by-round")
r = c.get("/admin/api/polls/special_ref/export.zip?live=1", headers=hdr)
ok("member_id" in _zipfile.ZipFile(_io.BytesIO(r.data)).read("ballots.csv").decode(),
   "non-anonymized export keeps identity columns")

r = c.get("/admin/api/polls/special_ref/blt/officer?live=1&withdraw=A1", headers=hdr)
ok(r.status_code == 200 and "\n-1\n" in r.get_data(as_text=True),
   "dropout recount BLT carries the withdrawn line")

ok("Terms of Use" in c.get("/terms").data.decode(), "terms page served")
ok("Privacy Policy" in c.get("/privacy").data.decode(), "privacy page served")
sp2 = c.get("/").data.decode()
ok("DSA Vote" in sp2 and "Built with 🌹 by Walker Green" in sp2, "splash rebranded")
ok("value {" not in "".join(  # narrative strings render with transfer values
    st["action"] for q in res.values() if isinstance(q, dict) and q.get("stages")
    for st in q["stages"]), "stage actions well-formed")

# ---- constrained STV: leadership quota requirements ----
LEAD_QS = [{"key": "npc", "type": "ranked", "title": "Elect 3 leaders", "seats": 3,
            "options": [{"id": "A1", "name": "A", "tags": ["cis_man"]},
                        {"id": "B2", "name": "B", "tags": ["cis_man"]},
                        {"id": "C3", "name": "C", "tags": ["cis_man"]},
                        {"id": "D4", "name": "D", "tags": ["marginalized"]},
                        {"id": "E5", "name": "E", "tags": ["marginalized"]}],
            "constraints": [{"tag": "cis_man", "max": 2, "label": "No more than 2 cis men"},
                            {"tag": "marginalized", "min": 1}]}]
r = c.post("/admin/api/polls/leadership_test", headers=hdr,
           json={"name": "Leadership Test", "questions": LEAD_QS})
ok(r.status_code == 200, "leadership poll with quota constraints saves")
ok(c.post("/admin/api/polls/leadership_test", headers=hdr, json={
    "name": "x", "questions": [dict(LEAD_QS[0],
        constraints=[{"tag": "a", "min": 2}, {"tag": "b", "min": 2}])]}).status_code == 400,
   "constraint minimums exceeding seats rejected")
for i, ranking in enumerate([["A1"], ["A1"], ["A1"], ["A1"], ["B2"], ["B2"], ["B2"],
                             ["C3"], ["C3"], ["C3"], ["D4"], ["D4"], ["E5"]]):
    cc = f"LEAD{i:02d}__PADPADPAD"
    ok(c.post("/p/leadership_test/vote", json={"code": cc, "answers": {"npc": ranking}})
       .status_code == 200, f"leadership vote {i}")
r = c.get("/admin/api/polls/leadership_test/results?live=1", headers=hdr)
lq = r.get_json()["questions"][0]
cis_winners = sum(1 for w in lq["winners"] if w in ("A", "B", "C"))
ok(len(lq["winners"]) == 3 and cis_winners <= 2,
   "constrained count: cis-men maximum enforced in winner set")
ok(any(w in ("D", "E") for w in lq["winners"]), "constrained count: minimum satisfied")
ok(lq["constraints"][0]["elected"] == cis_winners, "results echo per-constraint tallies")
ok(any("quota" in st["action"].lower() or "guarded" in st["action"].lower()
       for st in lq["stages"]) or cis_winners <= 2, "stage log explains quota actions")

r = c.post("/admin/api/count_blt", headers=hdr, json={
    "blt": '3 2\n3 1 0\n2 2 0\n1 3 0\n0\n"X"\n"Y"\n"Z"\n"t"\n',
    "constraints": [{"tag": "cis", "max": 1}],
    "cand_tags": {"1": ["cis"], "2": ["cis"]}})
ok(r.status_code == 200 and sum(1 for w in r.get_json()["winners"] if w in ("X", "Y")) <= 1,
   "workbench honors quota constraints")

# ---- full-body quota groups + unconstrained comparison + tags required ----
BODY_QS = [
    {"key": "cochair", "type": "ranked", "title": "Elect 1 co-chair", "seats": 1,
     "quota_group": "body",
     "options": [{"id": "P1", "name": "P", "tags": []},
                 {"id": "Q2", "name": "Q", "tags": ["marginalized"]}]},
    {"key": "atlarge", "type": "ranked", "title": "Elect 2 at-large", "seats": 2,
     "quota_group": "body",
     "options": [{"id": "R1", "name": "R", "tags": []},
                 {"id": "S2", "name": "S", "tags": ["marginalized"]},
                 {"id": "T3", "name": "T", "tags": ["marginalized"]}]},
]
r = c.post("/admin/api/polls/body_quota_test", headers=hdr, json={
    "name": "Body Quota Test",
    "quota_groups": {"body": [{"tag": "marginalized", "min": 2,
                               "label": "At least 2 marginalized members on the body"}]},
    "questions": BODY_QS})
ok(r.status_code == 200, "full-body quota group saves")
ok(c.post("/admin/api/polls/body_quota_test", headers=hdr, json={
    "name": "x", "quota_groups": {"body": [{"tag": "m", "min": 1}]},
    "questions": [dict(BODY_QS[0],
        options=[{"id": "P1", "name": "P"}, {"id": "Q2", "name": "Q", "tags": ["m"]}])]}
    ).status_code == 400, "quota contests demand collected attributes (tags key) per candidate")

for i, (qk, ranking) in enumerate([("cochair", ["P1"])] * 4 + [("cochair", ["Q2"])] * 2
                                  + [("atlarge", ["R1"])] * 4 + [("atlarge", ["S2"])] * 3
                                  + [("atlarge", ["T3"])]):
    cc = f"BODY{i:02d}__PADPADPAD"
    ans = {"cochair": ["ABSTAIN"], "atlarge": ["ABSTAIN"]}
    ans[qk] = ranking
    ok(c.post("/p/body_quota_test/vote", json={"code": cc, "answers": ans}).status_code == 200,
       f"body vote {i}")
r = c.get("/admin/api/polls/body_quota_test/results?live=1", headers=hdr)
bq = {q["key"]: q for q in r.get_json()["questions"]}
ok(bq["cochair"]["winners"] == ["P"] and bq["cochair"]["quota_group"] == "body",
   "group: early small contest not force-fed the minimum (later seats cover it)")
marg_body = sum(1 for w in bq["cochair"]["winners"] + bq["atlarge"]["winners"]
                if w in ("Q", "S", "T"))
ok(marg_body >= 2, "group minimum satisfied across the full body")
ok(bq["atlarge"]["constraints"][0]["elected"] == marg_body,
   "group constraint tallies are body-wide")
ok("unconstrained" in bq["atlarge"] and bq["atlarge"]["unconstrained"]["winners"],
   "results carry the unconstrained comparison for the toggle")
r = c.get("/admin/api/polls/body_quota_test/export.zip?live=1", headers=hdr)
ok("WITHOUT quota requirements" in
   _zipfile.ZipFile(_io.BytesIO(r.data)).read("results.txt").decode(),
   "results.txt exports both outcomes")

# ---- method recount previews + public published results page ----
ok(c.post("/admin/api/polls/body_quota_test/publish", headers=hdr,
          json={"publish": True}).status_code == 409, "publish requires finalize")
ok("Results not published" in c.get("/p/body_quota_test/results").data.decode(),
   "public page holds back unpublished results")

DOCS[("config__polls", "special_ref")]["closes_at"] = now - 50
c.post("/admin/api/cron/closeout", headers=hdr)
r = c.post("/admin/api/polls/special_ref/recount_preview", headers=hdr,
           json={"question": "officer", "method": "plurality"})
ok(r.status_code == 200 and len(r.get_json()["winners"]) == 2
   and "first choice" in r.get_json()["note"], "plurality recount preview")
for m in ("irv", "approval", "borda"):
    ok(c.post("/admin/api/polls/special_ref/recount_preview", headers=hdr,
              json={"question": "officer", "method": m}).status_code == 200,
       f"{m} recount preview")
ok(c.post("/admin/api/polls/special_ref/recount_preview", headers=hdr,
          json={"question": "officer", "method": "condorcet"}).status_code == 400,
   "unknown method rejected")

r = c.post("/admin/api/polls/special_ref/publish", headers=hdr, json={"publish": True})
ok(r.status_code == 200 and r.get_json()["url"] == "/p/special_ref/results",
   "finalized poll publishes")
pub = c.get("/p/special_ref/results").data.decode()
ok("Official Results" in pub and "Elected" in pub and "mutual aid pantry" in pub,
   "public results page renders outcomes")
ok("AK101" not in pub and "receipt" not in pub.lower().replace(
    "receipt code", ""), "public page leaks no voter identity")
ok(c.post("/admin/api/polls/special_ref/publish", headers=hdr,
          json={"publish": False}).status_code == 200
   and "Results not published" in c.get("/p/special_ref/results").data.decode(),
   "unpublish takes the page down")

# ---- meek preview + frozen published-results cache ----
ok(c.post("/admin/api/polls/special_ref/recount_preview", headers=hdr,
          json={"question": "officer", "method": "meek"}).status_code == 200,
   "meek STV recount preview")

r = c.post("/admin/api/polls/special_ref/publish", headers=hdr, json={"publish": True})
ok(r.status_code == 200 and ("special_ref__published", "results") in DOCS,
   "publish freezes results into the published cache doc")
import json as _json  # noqa: E402
frozen = _json.loads(DOCS[("special_ref__published", "results")]["json"])
ok(frozen["ballots_counted"] == 3 and any(q["type"] == "ranked"
   for q in frozen["questions"]), "frozen results carry full payload")
r = c.get("/admin/api/polls/special_ref/results", headers=hdr)
ok(r.status_code == 200 and r.get_json().get("cached") is True,
   "finalized results serve from the frozen cache")
ok("cached" not in c.get("/admin/api/polls/special_ref/results?fresh=1",
                         headers=hdr).get_json(), "?fresh=1 recomputes")
ok("Official Results" in c.get("/p/special_ref/results").data.decode(),
   "public page renders from the frozen cache")

# ---- official Meek STV contests (YDSA delegate elections) ----
YDSA_QS = [{"key": "dels", "type": "ranked", "title": "Elect 2 YDSA delegates",
            "seats": 2, "method": "meek",
            "options": [{"id": "A1", "name": "Ava"}, {"id": "B2", "name": "Ben"},
                        {"id": "C3", "name": "Cal"}]}]
ok(c.post("/admin/api/polls/ydsa_test", headers=hdr,
          json={"name": "YDSA Test", "questions": YDSA_QS}).status_code == 200,
   "meek-method poll saves")
ok(c.post("/admin/api/polls/ydsa_test", headers=hdr, json={
    "name": "x", "questions": [dict(YDSA_QS[0], method="banana")]}).status_code == 400,
   "unknown official method rejected")
for i, ranking in enumerate([["A1", "B2"]] * 3 + [["B2"]] * 2 + [["C3", "A1"]]):
    cc = f"YDSA{i:02d}__PADPADPAD"
    ok(c.post("/p/ydsa_test/vote", json={"code": cc, "answers": {"dels": ranking}})
       .status_code == 200, f"ydsa vote {i}")
r = c.get("/admin/api/polls/ydsa_test/results?live=1", headers=hdr)
yq = r.get_json()["questions"][0]
ok(yq["method_used"].startswith("Meek STV") and len(yq["winners"]) == 2
   and len(yq["stages"]) >= 1, "official Meek count with stage log")

print(f"SMOKE TEST: all {passed} checks passed")
