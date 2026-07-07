#!/bin/sh
# Entrypoint for the all-in-one Railway service.
# 1. Apply DB migrations.
# 2. Start supercronic (producer/scheduler/analytics crons) in the background.
# 3. Run the web API in the foreground (container lives and dies with it).
set -e

alembic upgrade head

supercronic /app/deploy/crontab &

exec uvicorn web.api:app --host 0.0.0.0 --port "${PORT:-8000}"
