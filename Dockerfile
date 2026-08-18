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
# listen on it — gunicorn.conf.py below reads that. Migrations run once on
# each container start rather than as a separate release step (Cloud Run
# has no built-in equivalent to that): safe since Django migrations are
# idempotent, already-applied ones are just a no-op.
CMD python manage.py migrate --noinput && gunicorn cufr.wsgi --bind 0.0.0.0:${PORT:-8080} --workers 2 --log-file -
