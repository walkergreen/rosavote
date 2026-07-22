# RosaVote — Domain, Staging & Edge Protection

Everything needed to put RosaVote on a real domain with a staging environment
and DDoS/rate-limit protection. Two edge options are covered — **Cloudflare**
(simplest) and **Google Cloud load balancer + Cloud Armor** (all-Google). Pick
**one** edge; don't stack both.

**Canonical: `rosavote.org`** (apex — bare `rosavote.org`, no `www`).
**`rosavote.com`** and **`www.rosavote.org`** both 301 → the canonical host.
Staging: `staging.rosavote.org`.

> Registrar: with `.vote` off the table, **Cloudflare Registrar** is the best
> pick — at-cost `.org`/`.com`, free WHOIS privacy, and the domain lands right
> on the Cloudflare DNS/WAF edge below. (Porkbun is a fine alternative.)

---

## 0. One-move go-live (after you own the domain)

The redirect and every internal link are host-agnostic, so bringing a domain
online is essentially one script + one env var.

```sh
# From the repo root — maps the domain to Cloud Run and prints the DNS records.
infra/map-domain.sh rosavote.org
# ...add the A/AAAA records it prints at your DNS host, wait for the cert, then:
gcloud run services update member-ballot-v3 --region us-east1 \
  --set-env-vars CANONICAL_HOST=rosavote.org
```

That's it — `/about`, `/vs-opavote`, ballot links, etc. all serve from
`https://rosavote.org/...` and any other host 301s to it. (Prefer Cloudflare's
WAF in front? Use the load-balancer path in §3 instead of `map-domain.sh`.)

## 0b. The alias → canonical redirect is already in the app

The Flask app 301s any non-canonical host to `CANONICAL_HOST`
(`vote_service.py`, `_canonical_host_redirect`), so `rosavote.com` and
`www.rosavote.org` funnel to the canonical automatically — just:

1. Point `rosavote.com` and `www.rosavote.org` at the same service, and
2. Set `CANONICAL_HOST=rosavote.org`
   (`--set-env-vars CANONICAL_HOST=rosavote.org`).

Then `https://rosavote.com/anything` → `301` → `https://rosavote.org/anything`.
Doing the redirect at the edge too (below) is fine and saves a round-trip.

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

**Apex `rosavote.org`:** an apex can't be a CNAME. Either (a) run
`infra/map-domain.sh rosavote.org` and add the **A/AAAA** records it prints, or
(b) on Cloudflare, a proxied (orange-cloud) CNAME at the apex works via
**CNAME flattening** — point it at your domain-mapping target or the LB. A
subdomain like `staging.rosavote.org` takes a normal CNAME.

For **Cloud Run custom domains**, either use Cloud Run **domain mappings**
(`infra/map-domain.sh`, or `gcloud run domain-mappings create --service
member-ballot-v3 --domain rosavote.org --region us-east1`) which give you the
exact DNS records to enter, or front Cloud Run with a load balancer (Option B).

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

# managed TLS cert for all hosts terminated here (apex + aliases + staging)
gcloud compute ssl-certificates create rosavote-cert --global \
  --domains=rosavote.org,www.rosavote.org,rosavote.com,staging.rosavote.org

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

- [ ] Domains bought; `rosavote.org` (apex) and `staging.` resolve to the right services.
- [ ] `CANONICAL_HOST=rosavote.org` set on prod; `rosavote.com`/`www` 301 to it (test it).
- [ ] Staging on its own Firestore DB + token; `smoke_test.py` green in CI.
- [ ] One edge chosen (Cloudflare **or** Cloud Armor) with global + `/admin`
      rate limits.
- [ ] HTTPS enforced; managed cert valid for all hosts.
- [ ] Independent pen test done (see the `/accuracy` page's assurance model).
- [ ] Export snapshot taken at each election's close (belt-and-suspenders on top
      of Firestore's 7-day point-in-time recovery).
