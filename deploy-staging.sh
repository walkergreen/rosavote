#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
# Deploy the RosaVote STAGING service — a second Cloud Run service, isolated
# from production by its own Firestore database and its own admin-token secret.
#
#   ./deploy-staging.sh
#
# Prereqs (one-time — see infra/STAGING_AND_EDGE.md for the full walkthrough):
#   * a named Firestore database "staging" in the same project
#         gcloud firestore databases create --database=staging \
#           --location=us-east1 --type=firestore-native
#   * a staging admin-token secret
#         printf '%s' "$(openssl rand -hex 32)" | \
#           gcloud secrets create ballot-admin-token-staging --data-file=-
#
# Run from the repo root (the dir that CONTAINS ballot-v2), like the prod deploy.
set -euo pipefail

PROJECT="${PROJECT:-dsa-org-tools}"
REGION="${REGION:-us-east1}"
SERVICE="${SERVICE:-rosavote-staging}"
SOURCE="${SOURCE:-ballot-v2}"
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-staging}"
# Set CANONICAL_HOST once staging DNS exists (e.g. staging.rosavote.org). Empty
# = serve on the Cloud Run URL with no redirect.
CANONICAL_HOST="${CANONICAL_HOST:-}"

echo "Deploying ${SERVICE} (db=${FIRESTORE_DATABASE}, canonical='${CANONICAL_HOST:-<none>}')"

gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --source "${SOURCE}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 5 \
  --set-secrets "ADMIN_TOKEN=ballot-admin-token-staging:latest" \
  --set-env-vars "FIRESTORE_DATABASE=${FIRESTORE_DATABASE},CANONICAL_HOST=${CANONICAL_HOST}"

echo "Done. Staging URL:"
gcloud run services describe "${SERVICE}" --project "${PROJECT}" \
  --region "${REGION}" --format="value(status.url)"
echo
echo "Smoke-test it:  curl -s \$(gcloud run services describe ${SERVICE} \\"
echo "                  --region ${REGION} --format='value(status.url)')/health"
