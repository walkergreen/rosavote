#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Local dev server with a stubbed in-memory Firestore — no GCP needed.

    python3 tools/dev_server.py            # http://localhost:8080
    ADMIN_TOKEN=dev python3 tools/dev_server.py

Same stub as smoke_test.py: data lives in a process-local dict and vanishes
on restart. Votes always "claim" successfully (member AK-DEV), so the whole
flow — ballot, admin console, builder, adjudication — is clickable offline.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ADMIN_TOKEN", "dev")

DOCS = {}


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
gcloud = types.ModuleType("google.cloud"); gcloud.firestore = fake_fs
gapi = types.ModuleType("google.api_core"); gapi.exceptions = fake_exc
sys.modules.update({"google.cloud": gcloud, "google.cloud.firestore": fake_fs,
                    "google.api_core": gapi, "google.api_core.exceptions": fake_exc})

import vote_service  # noqa: E402

vote_service._claim_code = lambda txn, coll, ch: {"member_id": "AK-DEV", "chapter": "dev"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"dev server (stub Firestore): http://localhost:{port}/  admin token: "
          f"{os.environ['ADMIN_TOKEN']!r}")
    vote_service.app.run(host="127.0.0.1", port=port, debug=False)
