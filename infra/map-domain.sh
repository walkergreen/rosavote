#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
#
# Map a custom domain to the Cloud Run service, print the DNS records to add,
# and (after DNS + cert are live) make it the canonical host.
#
# Usage:
#   infra/map-domain.sh rosavote.org                    # map apex, default service/region
#   infra/map-domain.sh rosavote.org member-ballot-v3 us-east1
#
# Notes:
#  * rosavote.org is an APEX domain -> Cloud Run returns A/AAAA records (an apex
#    cannot be a CNAME). Add them at whatever DNS host you use (Cloudflare
#    DNS-only / grey-cloud, or Porkbun). A subdomain (e.g. www.rosavote.org)
#    would get a single CNAME instead.
#  * Domain ownership must be verified once (Search Console). `gcloud domains
#    verify` opens that flow; re-runs are a no-op once verified.
#  * If you'd rather sit Cloudflare's WAF in front, use the load balancer path
#    in STAGING_AND_EDGE.md (Option B) instead of this mapping.
set -euo pipefail

DOMAIN="${1:?usage: map-domain.sh <domain> [service] [region]}"
SERVICE="${2:-member-ballot-v3}"
REGION="${3:-us-east1}"

echo "==> (1/3) Verify domain ownership for $DOMAIN"
echo "    If not already verified, this opens the Search Console flow in a browser."
gcloud domains verify "$DOMAIN" || true

echo "==> (2/3) Create the Cloud Run domain mapping ($DOMAIN -> $SERVICE / $REGION)"
gcloud run domain-mappings create \
  --service "$SERVICE" --domain "$DOMAIN" --region "$REGION" || true

echo
echo "==> DNS records to add at your DNS host (apex A/AAAA, or a CNAME for a subdomain):"
gcloud run domain-mappings describe --domain "$DOMAIN" --region "$REGION" \
  --format='table[no-heading](status.resourceRecords[].type, status.resourceRecords[].name, status.resourceRecords[].rrdata)' \
  || echo "    (re-run this describe once the mapping finishes provisioning)"

echo
echo "==> (3/3) After DNS propagates AND the managed cert shows ACTIVE, make it canonical:"
echo "    gcloud run services update $SERVICE --region $REGION \\"
echo "      --set-env-vars CANONICAL_HOST=$DOMAIN"
echo
echo "    Then point rosavote.com / www.rosavote.org at the same service (or"
echo "    forward them) and the app 301s every alias to https://$DOMAIN/... automatically."
echo
echo "    Check cert status any time with:"
echo "    gcloud run domain-mappings describe --domain $DOMAIN --region $REGION \\"
echo "      --format='value(status.conditions)'"
