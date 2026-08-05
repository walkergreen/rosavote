#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
"""
Offline GCP stubs shared by the test suites (smoke_test.py, security_test.py).

Importing this module installs fake google.cloud.{firestore,bigquery,storage}
into sys.modules, so `import vote_service` afterwards needs no credentials and
touches no real infrastructure. Kept in ONE place so the two suites can never
drift into testing different fakes.

State the suites read/reset:
    DOCS   (collection, key) -> dict     the fake Firestore
    WRITES [(collection, payload)]       every write, in order
    GCS    (bucket, path) -> text        the fake object store
    BQ_ROWS                              rows the fake BigQuery returns
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    def delete(self):
        DOCS.pop((self.coll, self.key), None)


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


class FakeTxn:
    """Transaction handle. Applies writes immediately — enough to exercise the
    call shape (all reads before writes, ballot + code burn issued together);
    real atomicity is Firestore's job."""
    def set(self, ref, d):
        ref.set(d)
    def update(self, ref, d):
        ref.update(d)


class FakeBatch:
    def __init__(self):
        self.ops = []
    def set(self, ref, d):
        self.ops.append((ref, d))
    def commit(self):
        if len(self.ops) > 500:
            raise AssertionError("Firestore rejects a WriteBatch over 500 ops")
        for ref, d in self.ops:
            ref.set(d)
        self.ops = []


fake_fs = types.ModuleType("google.cloud.firestore")
fake_fs.Client = lambda *a, **k: types.SimpleNamespace(
    collection=lambda nm: FakeColl(nm), transaction=lambda: FakeTxn(),
    batch=lambda: FakeBatch())
fake_fs.SERVER_TIMESTAMP = "TS"
fake_fs.transactional = lambda f: f
fake_exc = types.ModuleType("google.api_core.exceptions")
fake_exc.Aborted = type("Aborted", (Exception,), {})

# BigQuery stub: returns a tiny fixed roll for any query
BQ_ROWS = [{"member_id": "AK777001"}, {"member_id": "AK777002"}]
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
