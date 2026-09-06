FROM python:3.12-slim

# System libs needed by psycopg2-binary, Pillow, and reportlab
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    libpng-dev \
    libssl-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime — default to 8000 for local Docker runs.
# WEB_CONCURRENCY controls the number of uvicorn worker PROCESSES (each with
# its own event loop + its own DB connection pool, see app/database.py) —
# see the perf audit's P0 finding: a single worker serializes every request
# behind the slowest one currently in flight. Default of 2 is conservative;
# raising it multiplies DB connections used (pool_size+max_overflow per
# worker) — check against the Supabase plan's max connections before tuning up.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2}"]
