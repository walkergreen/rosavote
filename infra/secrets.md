# RosaVote — where secrets live, and how to set them

The rule: **secrets are never in code, the image, or git.** They live in exactly
three places, by kind.

| Secret | Where it lives | How it's protected |
|---|---|---|
| Database access (Firestore, BigQuery, GCS) | **nowhere** — IAM identity | Cloud Run runs as the least-priv service account `rosavote-run@rosavote-app.iam.gserviceaccount.com`; the clients auth as that identity. No key exists to leak. |
| National admin token (`ADMIN_TOKEN`) | **Secret Manager** (`ballot-admin-token`) | mounted via `--set-secrets`; versioned + access-audited |
| Chapter/national admin tokens | **Firestore `config__admins`**, keyed by SHA-256(token) | only the hash is stored — plaintext never persisted |
| Voting codes | **Firestore `{poll}__codes`**, SHA-256 hashed | plaintext never persisted |
| Integration API keys (Mailgun, Twilio, Scale to Win) | **Secret Manager** (see below) | mounted via `--set-secrets`, NOT `--set-env-vars` |

## Golden rule for integration keys

Route **every** integration credential through Secret Manager with `--set-secrets`,
never `--set-env-vars`. Plain env vars are visible to anyone with Cloud Run
*viewer* access (console + `gcloud run services describe`); Secret Manager keeps
them out of that surface, versioned, and IAM-gated. The app reads them from the
environment either way — this is purely a deploy-flag choice, and Secret Manager
is the only right one.

## One-time: create the secrets

```sh
PROJECT=rosavote-app
# Mailgun (transactional email)
printf '%s' "$MAILGUN_API_KEY" | gcloud secrets create mailgun-api-key --data-file=- --project $PROJECT
# Twilio (transactional one-off SMS)
printf '%s' "$TWILIO_AUTH_TOKEN" | gcloud secrets create twilio-auth-token --data-file=- --project $PROJECT
# Scale to Win (bulk SMS API, if used)
printf '%s' "$STW_API_KEY" | gcloud secrets create stw-api-key --data-file=- --project $PROJECT
```

Grant the runtime SA read access to each (once):

```sh
for S in mailgun-api-key twilio-auth-token stw-api-key; do
  gcloud secrets add-iam-policy-binding "$S" --project $PROJECT \
    --member="serviceAccount:rosavote-run@rosavote-app.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

## Deploy: mount secrets + set the NON-secret config as env

Secret *values* → `--set-secrets`. Non-secret identifiers (domain, from-number,
API base) → `--set-env-vars`. They're not secret and can live in plain config.

```sh
gcloud run deploy member-ballot-v3 --source ballot-v2 --region us-east1 \
  --allow-unauthenticated --min-instances 0 --max-instances 20 \
  --service-account rosavote-run@rosavote-app.iam.gserviceaccount.com \
  --set-secrets ADMIN_TOKEN=ballot-admin-token:latest,\
MAILGUN_API_KEY=mailgun-api-key:latest,\
TWILIO_AUTH_TOKEN=twilio-auth-token:latest,\
STW_API_KEY=stw-api-key:latest \
  --set-env-vars MAILGUN_DOMAIN=mg.rosavote.org,MAILGUN_FROM='RosaVote <ballots@mg.rosavote.org>',\
TWILIO_ACCOUNT_SID=ACxxxxxxxx,TWILIO_FROM=+15551234567,\
SOURCE_URL=https://github.com/…,CANONICAL_HOST=vote.rosavote.org
```

> `TWILIO_ACCOUNT_SID` is an identifier, not a secret — env is fine. The **auth
> token** is the secret. Same split for Mailgun: `MAILGUN_DOMAIN`/`MAILGUN_FROM`
> are config; `MAILGUN_API_KEY` is the secret.

## Rotate a secret

```sh
printf '%s' "$NEW_VALUE" | gcloud secrets versions add mailgun-api-key --data-file=-
gcloud run services update member-ballot-v3 --region us-east1 \
  --set-secrets MAILGUN_API_KEY=mailgun-api-key:latest   # picks up the new version on next revision
```

## Never do

- ❌ `--set-env-vars MAILGUN_API_KEY=…` (plaintext in the service config)
- ❌ commit a `.env`, a service-account key JSON, or any token to git
- ❌ download a service-account key file — use the runtime SA identity instead
- ❌ log a secret, an API key, or a voter's code/contact (tools print counts only)
