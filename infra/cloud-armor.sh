#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Walker Green
# Cloud Armor edge protection for RosaVote (production), for the case where you
# front Cloud Run with a Google Cloud external HTTPS load balancer instead of
# (or in addition to) Cloudflare. Creates a security policy with:
#   * Layer-7 DDoS adaptive protection
#   * a global per-IP rate limit (throttle, not full ban, so a shared-NAT
#     chapter doesn't get locked out mid-vote)
#   * a stricter rate limit on the admin surface
#
#   ./infra/cloud-armor.sh
#
# This does NOT create the load balancer itself — see STAGING_AND_EDGE.md §4 for
# the serverless NEG + URL map + managed cert steps. Attach this policy to the
# LB backend service once it exists.
#
# NOTE: If you use Cloudflare's proxy (orange cloud) in front, you get L3/4 +
# basic L7 DDoS + rate limiting there and may not need Cloud Armor at all. Use
# ONE edge, not two fighting each other. Cloud Armor is the "all-Google" path.
set -euo pipefail

PROJECT="${PROJECT:-dsa-org-tools}"
POLICY="${POLICY:-rosavote-armor}"
BACKEND="${BACKEND:-}"          # set to your LB backend service to auto-attach

gcloud config set project "${PROJECT}"

# 1. security policy with adaptive (ML) L7 DDoS protection
gcloud compute security-policies create "${POLICY}" \
  --description "RosaVote edge protection" || true
gcloud compute security-policies update "${POLICY}" \
  --enable-layer7-ddos-defense

# 2. global per-IP rate limit — THROTTLE (not ban). 600 req/min/IP is generous
#    for a voter (a ballot is a handful of requests) but chokes a flood.
gcloud compute security-policies rules create 1000 \
  --security-policy "${POLICY}" \
  --expression "true" \
  --action throttle \
  --rate-limit-threshold-count 600 \
  --rate-limit-threshold-interval-sec 60 \
  --conform-action allow \
  --exceed-action deny-429 \
  --enforce-on-key IP \
  --description "global per-IP throttle"

# 3. stricter limit on the admin surface (login/token endpoints get brute-forced)
gcloud compute security-policies rules create 900 \
  --security-policy "${POLICY}" \
  --expression "request.path.startsWith('/admin')" \
  --action throttle \
  --rate-limit-threshold-count 120 \
  --rate-limit-threshold-interval-sec 60 \
  --conform-action allow \
  --exceed-action deny-429 \
  --enforce-on-key IP \
  --description "tighter throttle on /admin"

# 4. optional: attach to an existing LB backend service
if [[ -n "${BACKEND}" ]]; then
  gcloud compute backend-services update "${BACKEND}" \
    --global --security-policy "${POLICY}"
  echo "Attached ${POLICY} to backend ${BACKEND}."
else
  echo "Policy ${POLICY} created. Attach it with:"
  echo "  gcloud compute backend-services update <BACKEND> --global --security-policy ${POLICY}"
fi
