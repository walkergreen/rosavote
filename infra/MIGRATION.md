# RosaVote — migration off DSA infrastructure

> **STATUS: EXECUTED 2026-08-04.** Data migrated (24 collections, counts
> verified), domains cut over, DSA-side resources torn down. Deviations from
> the plan below: service named `rosavote` (not member-ballot-v3); production
> data WAS migrated per owner decision (vendor framing), then purged from DSA's
> GCP; living candidates' surnames abbreviated in demo data.

Move everything from **`dsa-org-tools`** (DSA's GCP project) to a dedicated
project owned by **cliffwgreen@gmail.com**. After this, DSA's only remaining
tie to RosaVote is optional data *import* (CSV/GCS), not hosting.

## Current footprint (verified 2026-08-04)

| Piece | Today | After |
|---|---|---|
| Cloud Run `member-ballot-v3` (prod) | `dsa-org-tools` / us-east1 | new project, service `rosavote` |
| Cloud Run `rosavote-staging` | `dsa-org-tools` | new project (recreate later) |
| Firestore: default DB (prod data) + `staging` DB | `dsa-org-tools` | new project |
| Secrets `ballot-admin-token(-staging)` | `dsa-org-tools` Secret Manager | new secrets, **fresh tokens** |
| SA `rosavote-run@dsa-org-tools…` (datastore.user, secretAccessor, **bigquery.jobUser**) | `dsa-org-tools` | new SA, **no BigQuery role** |
| `rosavote.org` apex A/AAAA → `216.239.3x.21`, `app` CNAME → `ghs.googlehosted.com` (grey-cloud) | domain mappings in `dsa-org-tools` | same DNS records; mappings recreated in new project |
| `www` CNAME → `member-ballot-v3-…run.app` (proxied) | old service URL | new service URL |
| Repo, `rosavote.org` DNS/registrar, `support@` email routing | already personal | unchanged |

**BigQuery dependency:** exactly one runtime feature — `POST …/voters/import_bigquery`
(member import by roll chapter, reads `proj-tmc-mem-dsa`). It fails gracefully
(`bigquery_failed`) and CSV (`/voters/import`) + GCS (`/voters/import_gcs`)
imports remain. Intentionally NOT carried over. `tools/generate_codes.py` is an
operator CLI using the operator's own credentials — unaffected by app hosting.

## Phase 0 — prerequisites (WALKER, interactive)
1. `gcloud auth login` → **cliffwgreen@gmail.com** (project-side work)
2. `gcloud auth login` → **wgreen@dsausa.org** (Firestore export + DSA teardown)
   *(both tokens are currently expired; every later phase is blocked on this)*
3. Billing: `gcloud billing accounts list --account cliffwgreen@gmail.com` — if
   none exists, create one in the console (card entry is a you-step).
4. Decide: migrate prod Firestore data (elections, `config__admins` chapter
   tokens) or fresh start? Fresh start invalidates chapter admin tokens.

## Phase 1 — scaffold new project (agent-runnable once authed)
```bash
PROJECT=rosavote-app        # adjust if ID taken
gcloud projects create $PROJECT --account cliffwgreen@gmail.com
gcloud billing projects link $PROJECT --billing-account=<ID>
gcloud services enable run.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com --project $PROJECT
gcloud firestore databases create --location=us-east1 --project $PROJECT
gcloud firestore databases update --enable-pitr --project $PROJECT
gcloud iam service-accounts create rosavote-run --project $PROJECT
# fresh admin token (do NOT copy the one in DSA's Secret Manager)
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create ballot-admin-token \
  --data-file=- --project $PROJECT
# roles: datastore.user on the project; secretAccessor on that one secret. No BigQuery.
```

## Phase 2 — deploy + smoke test (agent-runnable)
```bash
gcloud run deploy rosavote --source . --project $PROJECT --region us-east1 \
  --service-account rosavote-run@$PROJECT.iam.gserviceaccount.com \
  --allow-unauthenticated --min-instances 0 --max-instances 20 \
  --set-secrets ADMIN_TOKEN=ballot-admin-token:latest
curl https://<new-run-url>/health
```

## Phase 3 — data migration (needs wgreen@dsausa.org)
```bash
gsutil mb -p dsa-org-tools -l us-east1 gs://rosavote-migration-tmp
gcloud firestore export gs://rosavote-migration-tmp/export --project dsa-org-tools
# hand the bucket to the personal side (grant cliffwgreen viewer, or gsutil cp via local)
gcloud firestore import gs://<accessible-copy>/export --project $PROJECT
# verify collection counts match, then delete the tmp bucket
```

## Phase 4 — cutover (short outage; managed certs take ~15–60 min)
Domain mappings are exclusive to one project, so there is a cert-provisioning
window. Prototype-scale traffic → schedule it, don't engineer around it.
1. WALKER once: verify domain for the personal account:
   `gcloud domains verify rosavote.org --account cliffwgreen@gmail.com`
   (Search Console flow; TXT record can be auto-added via Cloudflare)
2. wgreen: `gcloud run domain-mappings delete` for `rosavote.org` and
   `app.rosavote.org` in `dsa-org-tools`
3. cliff: `infra/map-domain.sh rosavote.org rosavote us-east1` and again for
   `app.rosavote.org` in the new project. Apex/`app` DNS records stay as-is
   (same Google endpoints). Wait for certs ACTIVE.
4. Cloudflare: repoint `www` CNAME to the new `rosavote-….run.app` URL.
5. Leave `CANONICAL_HOST` unset (both apex and `app` serve today; keep parity).

## Phase 5 — DSA-side teardown (wgreen; AFTER new stack verified)
```bash
gcloud run services delete member-ballot-v3 rosavote-staging --project dsa-org-tools
# older prototypes (CLAUDE.md deletion candidates — confirm first):
#   member-ballot, member-ballot-v2, referendum-prototype
gcloud secrets delete ballot-admin-token ballot-admin-token-staging --project dsa-org-tools
gcloud iam service-accounts delete rosavote-run@dsa-org-tools.iam.gserviceaccount.com
gcloud firestore databases delete --database=staging --project dsa-org-tools
```
⚠️ Do **NOT** delete the *default* Firestore database in `dsa-org-tools` — the
project may host other org-tools data. Delete RosaVote's collections only
(after the Phase 3 export is verified), or leave them dormant.
Also sweep `proj-tmc-mem-dsa` IAM for any grant to the old SA.

## Phase 6 — repo updates (agent-runnable, no cloud auth)
- `CLAUDE.md`: project/service/URLs → new values
- `deploy-staging.sh`: `PROJECT` default → new project
- `infra/secrets.md`, `infra/map-domain.sh` defaults, staging service recreate
- Admin UI: hide the BigQuery import button (endpoint stays, degraded)

## Cost note
Personal billing takes over: Cloud Run scale-to-zero + prototype-scale
Firestore ≈ dollars/month at most; Firestore free tier likely covers it.
