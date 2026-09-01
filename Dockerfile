FROM python:3.13-slim

# psycopg2-binary needs libpq at runtime; build-essential covers anything
# else that needs compiling on install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Needed at build time for collectstatic (whitenoise doesn't touch the DB,
# so a dummy DATABASE_URL here is fine — the real one is injected at
# runtime by Cloud Run, well after this layer is built).
ENV DJANGO_SECRET_KEY=build-time-placeholder
ENV DATABASE_URL=sqlite:///build.db
RUN python manage.py collectstatic --noinput

ENV PYTHONUNBUFFERED=1

# Cloud Run injects PORT (defaults to 8080) and expects the container to
# listen on it; the entrypoint below reads it.
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Startup lives in docker-entrypoint.sh: it retries migrations (a spike
# cold-starts many instances at once and they contend for the session
# pooler's 15 connections) and then execs gunicorn with thread workers.
# WEB_WORKERS / WEB_THREADS / MIGRATE_ATTEMPTS are all env-tunable on the
# running service, so concurrency can be adjusted without a rebuild.
CMD ["/app/docker-entrypoint.sh"]
