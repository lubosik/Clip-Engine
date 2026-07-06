# Railway Deployment Runbook — Clip Engine

## Services

| Service name    | Dockerfile         | Role |
|-----------------|--------------------|------|
| `web`           | deploy/Dockerfile.web   | FastAPI review PWA + API |
| `producer`      | deploy/Dockerfile.worker | Producer cron (daily) |
| `scheduler`     | deploy/Dockerfile.worker | Schedule cron (every 15 min) |
| `analytics`     | deploy/Dockerfile.worker | Analytics cron (weekly) |
| `postiz`        | Postiz Docker image | Social posting hub |
| `postgres`      | Railway Postgres    | Shared database |

## Shared volume

Create one Railway volume named `clips-storage` and mount it at `/data/clips` on:
- `web`
- `producer`
- `scheduler`
- `analytics`

All services read/write the same clip files via STORAGE_DIR=/data/clips.

## Environment variables (set in Railway → Variables per service)

Required on ALL worker + web services:

```
DATABASE_URL=             # Railway provides this automatically for Postgres
APIFY_TOKEN=              # Apify console → Settings → Integrations → API tokens
POSTIZ_API_URL=           # e.g. https://postiz.<your-domain>.railway.app
POSTIZ_API_KEY=           # Postiz UI → Settings → Developers → Public API
LLM_API_KEY=              # Anthropic Console → API keys
LLM_MODEL=                # e.g. claude-opus-4-7  or  claude-sonnet-4-6
STORAGE_DIR=/data/clips
WEB_ADMIN_PASSWORD=       # Strong random password for the review PWA
TZ=America/New_York
```

Required on `web` only:
```
PORT=8000
```

Required on `postiz` service:
```
DATABASE_URL=             # Can share the same Postgres or use a separate DB
NEXT_PUBLIC_BACKEND_URL=https://postiz.<your-domain>.railway.app
IS_GENERAL=true           # Single-tenant mode
BACKEND_INTERNAL_URL=http://postiz.railway.internal:3000
```

## Postiz base URL fallback

If `GET {POSTIZ_API_URL}/public/v1/integrations` returns 404, try:
`{POSTIZ_API_URL}/api/public/v1/integrations`

Update POSTIZ_API_URL to include `/api` if your Postiz instance uses that path.

## Postiz OAuth setup (one-time manual steps)

Platform OAuth is configured ONCE manually in Postiz.  Do not attempt to automate.

1. Log into the Postiz UI at POSTIZ_API_URL.
2. Go to Settings → Providers.
3. For each platform (TikTok, Instagram, X/Twitter):
   a. Enter the platform app credentials (Client ID + Secret from the platform developer portal).
   b. Click Connect / Authorize and complete the OAuth flow.
   c. The connected account will appear as an integration with an `identifier` (tiktok/instagram/x).
4. Note the integration `name` and `profile` values — these must match the
   `destinations.postiz_channels` values in your campaign YAML files.
5. Re-authenticate when OAuth tokens expire (~60 days for TikTok, ~90 for IG).

## TikTok upload requirement

TikTok requires the uploaded media URL to be publicly reachable over HTTPS.
Ensure Railway's volume-backed STORAGE_DIR is served via the `web` service
(it is — /api/clips/{id}/video is a public-accessible HTTPS endpoint).

## Database migrations

Run Alembic migrations before first deploy:

```bash
# From the repo root with DATABASE_URL set:
alembic upgrade head
```

On Railway, add a one-off migration command or run it from the `web` service
start script:

```bash
alembic upgrade head && uvicorn web.api:app --host 0.0.0.0 --port $PORT
```

## Health check

Railway health check on `web`: GET /api/stats (requires Authorization header).
Configure in Railway → web service → Health Check → Path: /

Alternatively the Dockerfile HEALTHCHECK calls /api/stats directly via Python
(note: this will fail with 401 if health-check header is not sent; you may want
a separate unauthenticated /healthz endpoint or configure the Railway health
check to skip auth by hitting the root / instead).

## Rate limits reminder

- Postiz create-post: 90 requests/hour (self-hosted default).
  If you have > 90 approved clips pending, the scheduler will take > 1 hour.
  This is by design — clips are scheduled across future slots, not posted in bulk.
- Apify: depends on your plan.

## Rolling restarts

Railway redeploys do not terminate running producer jobs gracefully.
The producer is idempotent (used_ranges + dedupe) so a mid-run restart is safe —
it will re-process from scratch on next cron tick.
