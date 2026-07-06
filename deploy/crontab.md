# Cron Schedule — Clip Engine (Railway)

Railway cron is configured per service in railway.json or via the Railway UI
(Service → Settings → Cron Schedule).

## Expressions

| Service    | Schedule             | Notes |
|------------|----------------------|-------|
| `producer` | `0 14 * * *`         | Daily at 14:00 UTC (10:00 AM ET). Runs ~2h before the 17:00 ET default post slot. |
| `scheduler`| `*/15 * * * *`       | Every 15 minutes. Picks up newly approved clips. |
| `analytics`| `0 9 * * 1`          | Every Monday at 09:00 UTC. Matches `pull_day: monday` default. |

## Commands per service

```
producer:    python -m producer.run --all
scheduler:   python -m scheduler.schedule
analytics:   python -m scheduler.analytics
```

## Timezone note

All cron expressions above are in UTC.
Campaign schedules in YAML use America/New_York (or whatever timezone the operator
sets in `destinations.schedule.timezone`).  The scheduler converts correctly — the
cron expression is just for when Railway triggers the process, not the post time.

## Adjusting for custom campaigns

If a campaign has `destinations.schedule.times: ["09:00", "17:00"]` and
`posts_per_day: 2`, no cron change is needed — the scheduler process reads the
campaign config and computes the correct UTC slot each run.

## Producer cron adjustment

If you add campaigns that post in different timezones (e.g. a UK campaign that
posts at 18:00 BST = 17:00 UTC), consider running the producer at 15:00 UTC
instead of 14:00 UTC to cover all timezones safely.

## Multiple analytics pull days

If a campaign sets `pull_day: friday`, the analytics service will only process
that campaign on Fridays.  The Monday cron will still run but will log and skip
non-Monday campaigns.  Use `--force` to override:

```bash
python -m scheduler.analytics --force --campaign <name>
```
