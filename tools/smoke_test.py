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
    def __init__(self, ref, d):
        self.reference = ref
        self._d = d
    def to_dict(self):
        return dict(self._d)


class FakeRef:
    def __init__(self, coll, key):
        self.coll, self.key = coll, key
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


fake_fs = types.ModuleType("google.cloud.firestore")
fake_fs.Client = lambda *a, **k: types.SimpleNamespace(
    collection=lambda nm: FakeColl(nm), transaction=lambda: None)
fake_fs.SERVER_TIMESTAMP = "TS"
fake_fs.transactional = lambda f: f
fake_exc = types.ModuleType("google.api_core.exceptions")
fake_exc.Aborted = type("Aborted", (Exception,), {})
gcloud = types.ModuleType("google.cloud"); gcloud.firestore = fake_fs
gapi = types.ModuleType("google.api_core"); gapi.exceptions = fake_exc
sys.modules.update({"google.cloud": gcloud, "google.cloud.firestore": fake_fs,
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

print(f"SMOKE TEST: all {passed} checks passed")
