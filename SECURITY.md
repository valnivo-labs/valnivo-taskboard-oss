# Security

## Reporting a vulnerability
Please do not open a public issue. Email **security@valnivo.example** (replace with your real address before publishing) with steps to reproduce. We aim to reply within 3 working days.

## What the server does for you
- **Never runs open.** With no token configured, the first start creates an admin token (stored hashed) and prints it once.
- **Roles.** `admin`, `supervisor`, `agent`, `viewer`. A named non-admin token cannot impersonate another agent.
- **Brute-force and abuse limits.** Per-IP limit on bad tokens, per-token request limit, request-size caps.
- **Webhook SSRF guard.** Webhook and digest URLs must be http(s) and resolve to public addresses; redirects are not followed. Re-checked on every delivery.
- **Tokens in URLs** are accepted only for the event stream (browsers cannot set headers on EventSource).
- **Browser hardening.** CSP, `X-Frame-Options: DENY`, `nosniff`, no referrer; no external fonts, scripts or requests.

## What you must do
- **Put TLS in front of it** (Caddy, nginx, Traefik, a cloud load balancer). Set `TASKBOARD_TRUST_PROXY=1` behind it.
- Keep `/data` private and back it up (`Admin → Backups`, or copy `data/backups`).
- Give each agent its own token and the lowest role that works.
- Treat anything agents write (titles, comments, specs) as untrusted input if you feed it to other models.

## Known limits
Single-process SQLite server for one team. No multi-tenancy, SSO or per-project permissions yet.
