# RosaVote — Domain, Staging & Edge Protection

Everything needed to put RosaVote on a real domain with a staging environment
and DDoS/rate-limit protection. Two edge options are covered — **Cloudflare**
(simplest) and **Google Cloud load balancer + Cloud Armor** (all-Google). Pick
**one** edge; don't stack both.

Recommended domains: **`rosavote.org`** (primary) + **`rosavote.com`** (redirects
to `.org`). Hosts: `vote.rosavote.org` (prod), `staging.rosavote.org` (staging).

---

## 0. The `.com → .org` redirect is already handled in the app

The Flask app does a 301 from any non-canonical host to `CANONICAL_HOST`
(`vote_service.py`, `_canonical_host_redirect`). So the redirect works no matter
which edge you use — just:

1. Point **both** `rosavote.com` and `rosavote.org` (and `www`) at the same
   service, and
2. Set `CANONICAL_HOST=vote.rosavote.org` in the prod service's env
   (`--set-env-vars CANONICAL_HOST=vote.rosavote.org`).

Then `https://rosavote.com/anything` → `301` → `https://vote.rosavote.org/anything`.
You can *also* do the redirect at the edge (below) to save a round-trip; doing
both is fine and harmless.

---

## 1. Staging environment

Staging is a second Cloud Run service with its **own Firestore database** and
**own admin token**, so staging votes never touch production data.

```sh
# one-time: isolated Firestore database
gcloud firestore databases create --database=staging \
  --location=us-east1 --type=firestore-native

# one-time: staging admin token
printf '%s' "$(openssl rand -hex 32)" | \
  gcloud secrets create ballot-admin-token-staging --data-file=-

# deploy (repeatable) — from the repo root
./deploy-staging.sh
```

`deploy-staging.sh` wires `FIRESTORE_DATABASE=staging` (the app reads it in
`_LazyDB`) and a `max-instances 5` cap. Point `staging.rosavote.org` at this
service and set its `CANONICAL_HOST=staging.rosavote.org` if you want the app to
canonicalize there too (optional for staging).

CI suggestion: deploy `staging` from a `staging` git branch and `prod` from
`main`, both gated on `python3 tools/smoke_test.py` passing.

---

## 2. Option A — Cloudflare (simplest; recommended to start)

Cloudflare gives you free DNS, TLS, L3/4 + basic L7 DDoS protection, and
redirect/rate-limit rules without standing up a load balancer.

### 2.1 DNS
Move the domains' nameservers to Cloudflare (Cloudflare Registrar or transfer),
then add records (proxied = "orange cloud" ON):

| Type  | Name (rosavote.org) | Target                              | Proxy |
|-------|---------------------|-------------------------------------|-------|
| CNAME | `vote`              | `ghs.googlehosted.com`              | ON    |
| CNAME | `staging`           | `ghs.googlehosted.com`              | ON    |

For **Cloud Run custom domains**, either use Cloud Run **domain mappings**
(`gcloud run domain-mappings create --service member-ballot-v3 --domain
vote.rosavote.org`) which give you the exact DNS target to enter, or front Cloud
Run with a load balancer (Option B) and point the CNAME at the LB IP.

On `rosavote.com`, add the same `vote`/root records **or** just rely on the app
redirect (point `rosavote.com` root at the prod service and let Flask 301 it).

### 2.2 `.com → .org` redirect rule (edge, optional — app already does it)
Cloudflare → the `rosavote.com` zone → **Rules → Redirect Rules → Create**:
- **When**: `Hostname` `equals` `rosavote.com` (add `www.rosavote.com` too)
- **Then**: Dynamic redirect →
  `concat("https://vote.rosavote.org", http.request.uri.path)`
- **Status**: `301`, **Preserve query string**: ON

### 2.3 Rate limiting (L7)
Cloudflare → prod zone → **Security → WAF → Rate limiting rules**:
- **Global**: if requests to `*` exceed **~600 / 1 min** per IP → **Managed
  Challenge** (not block — a shared-NAT chapter shouldn't be hard-blocked).
- **Admin**: if `URI Path starts with /admin` exceeds **~120 / 1 min** per IP →
  **Block** (or Managed Challenge) for 10 min. Protects the token surface.

### 2.4 Other Cloudflare toggles
- SSL/TLS mode: **Full (strict)**.
- **Always Use HTTPS**: ON. **Bot Fight Mode**: ON.
- Leave **caching** default; the app already serves published results from an
  in-process frozen cache, so you don't need edge caching of dynamic pages
  (and must NOT cache authenticated/admin responses).

> Cloudflare's frontend absorbs volumetric (L3/4) attacks for free and the rate
> rules cover L7. With Cloudflare you generally do **not** also need Cloud Armor.

---

## 3. Option B — Google Cloud load balancer + Cloud Armor (all-Google)

Use this if you want to stay entirely in GCP or need Cloud Armor's adaptive
protection. This fronts Cloud Run with an external HTTPS load balancer.

### 3.1 Serverless NEG + backend + LB
```sh
REGION=us-east1
gcloud compute network-endpoint-groups create rosavote-neg \
  --region=$REGION --network-endpoint-type=serverless \
  --cloud-run-service=member-ballot-v3

gcloud compute backend-services create rosavote-backend --global \
  --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services add-backend rosavote-backend --global \
  --network-endpoint-group=rosavote-neg --network-endpoint-group-region=$REGION

gcloud compute url-maps create rosavote-lb --default-service rosavote-backend

# managed TLS cert for both hosts (add rosavote.com if terminating it here)
gcloud compute ssl-certificates create rosavote-cert --global \
  --domains=vote.rosavote.org,staging.rosavote.org

gcloud compute target-https-proxies create rosavote-https \
  --url-map=rosavote-lb --ssl-certificates=rosavote-cert
gcloud compute forwarding-rules create rosavote-fr --global \
  --target-https-proxy=rosavote-https --ports=443
```
Then point the `vote` / `staging` DNS records (any registrar — Cloudflare
DNS-only/grey cloud, or Porkbun) at the forwarding rule's global IP:
`gcloud compute forwarding-rules describe rosavote-fr --global --format='value(IPAddress)'`.

### 3.2 Cloud Armor
```sh
./infra/cloud-armor.sh          # creates the policy (rate limits + L7 DDoS)
gcloud compute backend-services update rosavote-backend --global \
  --security-policy rosavote-armor
```

### 3.3 `.com → .org` at the LB (optional — app already does it)
Add a URL-map redirect for the `rosavote.com` host, or simply let the Flask
`CANONICAL_HOST` redirect handle it (point `.com` at the same LB).

---

## 4. Porkbun (registrar-only) note

If you buy at **Porkbun** and don't use Cloudflare:
- Use Porkbun's **URL Forwarding** for the simplest `rosavote.com → rosavote.org`
  redirect (301, wildcard path) — or just rely on the app redirect.
- Point `vote` / `staging` **CNAME/ALIAS** records at your Cloud Run domain
  mapping target or the load-balancer IP.
- Porkbun gives you DNS + WHOIS privacy but **no DDoS/WAF** — so with Porkbun
  alone, add Option B (Cloud Armor) for edge protection before a binding vote.

---

## 5. Go-live checklist

- [ ] Domains bought; `vote.` and `staging.` resolve to the right services.
- [ ] `CANONICAL_HOST` set on prod; `rosavote.com` 301s to `.org` (test it).
- [ ] Staging on its own Firestore DB + token; `smoke_test.py` green in CI.
- [ ] One edge chosen (Cloudflare **or** Cloud Armor) with global + `/admin`
      rate limits.
- [ ] HTTPS enforced; managed cert valid for all hosts.
- [ ] Independent pen test done (see the `/accuracy` page's assurance model).
- [ ] Export snapshot taken at each election's close (belt-and-suspenders on top
      of Firestore's 7-day point-in-time recovery).
