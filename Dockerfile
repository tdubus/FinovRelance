# ===== Stage 1: Builder =====
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build

# Dependances systeme pour compiler les wheels (psycopg2, cryptography, Pillow, reportlab)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== Stage 2: Runtime =====
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependances runtime uniquement (pas de gcc ni outils de compilation)
# - libpq5 : runtime pour psycopg2
# - libjpeg62-turbo, zlib1g : runtime pour Pillow
# - curl : healthchecks
# - postgresql-client : pg_isready dans docker-entrypoint.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libjpeg62-turbo \
    zlib1g \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copier les packages Python compiles depuis le builder
COPY --from=builder /install /usr/local

COPY . .

COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

EXPOSE 5000

# Security: run as non-root user
RUN useradd -r -s /bin/false -d /app appuser && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["./docker-entrypoint.sh"]
