# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — dependency builder
# Installs all packages (including native extensions) into /install
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip --no-cache-dir \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 — lean runtime image
# Copies only the installed packages; no build tools, no cache
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN groupadd -r oneai && useradd -r -g oneai -d /app -s /sbin/nologin oneai

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py .
COPY templates/ templates/
COPY rag/ rag/

# Create writable runtime directories
RUN mkdir -p /app/rag_data /app/logs && \
    chown -R oneai:oneai /app

USER oneai
EXPOSE ${PORT}

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 --access-logfile - --error-logfile -"]
