#!/bin/sh
# Container start: bring the schema up to date, then serve.
#
# Migrations run here rather than as a separate release step because Cloud
# Run has no equivalent of one. That is safe (Django migrations are
# idempotent; an already-applied set is a no-op) but it has a failure mode
# that only appears under load: a traffic spike cold-starts many instances
# at once, every one of them opens a SESSION-pooler connection to migrate,
# and that pooler is capped at 15. Past the cap the connection is refused
# — and because the old command was `migrate && gunicorn`, a container
# that lost that race never served at all. Instances failing to start is
# the worst possible response to a spike.
#
# Retrying absorbs it: the contention lasts only as long as the other
# instances' migrate calls, which is about a second each when there is
# nothing to apply. Backing off and trying again turns a dead container
# into a slightly slower start.
set -e

ATTEMPTS="${MIGRATE_ATTEMPTS:-5}"
DELAY="${MIGRATE_RETRY_DELAY:-3}"
n=1
while [ "$n" -le "$ATTEMPTS" ]; do
    if DATABASE_URL="${MIGRATION_DATABASE_URL:-$DATABASE_URL}" \
       python manage.py migrate --noinput; then
        break
    fi
    if [ "$n" -eq "$ATTEMPTS" ]; then
        # Out of retries. Fail loudly rather than serving against a schema
        # we could not confirm — a wrong schema corrupts data, where a
        # failed start just means Cloud Run tries another instance.
        echo "migrate failed after ${ATTEMPTS} attempts; refusing to start" >&2
        exit 1
    fi
    echo "migrate attempt ${n}/${ATTEMPTS} failed; retrying in ${DELAY}s" >&2
    sleep "$DELAY"
    n=$((n + 1))
done

exec gunicorn cufr.wsgi \
    --bind "0.0.0.0:${PORT:-8080}" \
    --worker-class gthread \
    --workers "${WEB_WORKERS:-2}" \
    --threads "${WEB_THREADS:-16}" \
    --timeout 60 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile -
