#!/bin/sh
# Entrypoint for the all-in-one Railway service.
# 1. Sanity-check DATABASE_URL.
# 2. Apply DB migrations (retrying while Postgres comes up).
# 3. Start supercronic (producer/scheduler/analytics crons) in the background.
# 4. Run the web API in the foreground (container lives and dies with it).
set -e

case "${DATABASE_URL:-}" in
  "")
    echo "FATAL: DATABASE_URL is not set. In Railway, add a Postgres database" \
         "to the project and set DATABASE_URL on this service to the reference" \
         '${{Postgres.DATABASE_URL}} (Variables -> New Variable -> Add Reference).'
    exit 1
    ;;
  *placeholder*)
    echo "FATAL: DATABASE_URL contains 'placeholder' - it is not pointing at a" \
         "real database. In Railway, set it to the reference" \
         '${{Postgres.DATABASE_URL}} of your Postgres service.'
    exit 1
    ;;
esac

attempt=0
until alembic upgrade head; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 10 ]; then
    echo "FATAL: migrations failed after 10 attempts; giving up."
    exit 1
  fi
  echo "Database not reachable yet (attempt ${attempt}/10); retrying in 5s..."
  sleep 5
done

supercronic /app/deploy/crontab &

exec uvicorn web.api:app --host 0.0.0.0 --port "${PORT:-8000}"
