# Railway Deployment Runbook — Clip Engine

## Layout (updated 2026-07-07)

Two Railway services. Postiz is NOT self-hosted — the project uses Postiz
Cloud (https://api.postiz.com) with an API key.

| Service    | Source                        | Role |
|------------|-------------------------------|------|
| `clip-engine` | GitHub repo, root Dockerfile (auto-detected by Railway) | Web PWA + API, plus producer/scheduler/analytics crons via supercronic |
| `postgres` | Railway Postgres plugin       | Database |

Why one app service: Railway volumes attach to exactly ONE service
(https://docs.railway.com/reference/volumes), so the original 4-service
layout with a shared volume is impossible. The all-in-one container runs
uvicorn in the foreground and supercronic for the crons (see
deploy/crontab — producer daily 14:00 UTC, scheduler every 15 min,
analytics Mondays 09:00 UTC). supercronic skips a tick if the previous
run of the same job is still running, so overlap is safe.

Migrations run automatically on boot (deploy/start.sh: `alembic upgrade head`).

## Volume

Create one Railway volume named `clips-storage`, mounted at `/data/clips`
on the `clip-engine` service. STORAGE_DIR=/data/clips is baked into the image.

## Environment variables (clip-engine service)

```
DATABASE_URL=             # reference the Railway Postgres service variable
APIFY_TOKEN=              # Apify console → Settings → Integrations → API tokens
POSTIZ_API_URL=https://api.postiz.com
POSTIZ_API_KEY=           # Postiz Cloud → Settings → Developers → Public API
LLM_API_KEY=              # Anthropic key (sk-ant-...) OR OpenRouter key (sk-or-...)
LLM_MODEL=claude-sonnet-4-6           # with OpenRouter: anthropic/claude-sonnet-4.6
# LLM_BASE_URL=           # optional override; sk-or- keys auto-route to
                          # https://openrouter.ai/api (Anthropic-compatible /v1/messages)
WEB_ADMIN_PASSWORD=       # strong random password for the review PWA
TZ=America/New_York
PORT=8000
```

## Postiz Cloud channels

Connected integrations are matched from campaign YAML
`destinations.postiz_channels` by id, identifier, name, or profile.
Current account channels (profile viciresearch):

- `instagram-standalone` — Instagram @viciresearch
- `x` — X @viciresearch
- TikTok: not yet connected. Connect in Postiz Cloud UI, then add
  its identifier to the campaign YAML.

Re-authenticate when OAuth tokens expire (~60 days TikTok, ~90 IG).

## TikTok upload requirement

TikTok requires the uploaded media URL to be publicly reachable over HTTPS.
The web service serves clips at /api/clips/{id}/video over the Railway
public domain, which satisfies this.

## Health check

railway.json sets healthcheckPath to /healthz (unauthenticated liveness
endpoint). The Dockerfile HEALTHCHECK hits the same path.

## Rate limits reminder

- Postiz Cloud create-post: ~100 requests/hour. With > 100 approved clips
  pending, the scheduler takes > 1 hour by design — clips are scheduled
  across future slots, not posted in bulk.
- Apify: depends on your plan.

## Rolling restarts

Railway redeploys do not terminate a mid-run producer gracefully. The
producer is idempotent (used_ranges + dedupe), so a mid-run restart is
safe — it re-processes from scratch on the next cron tick.
