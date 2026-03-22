# ══════════════════════════════════════════════════════════════
# BITTU Backend — Production Dockerfile
# Multi-stage build for minimal image size and security
# ══════════════════════════════════════════════════════════════

# ── Stage 1: Build dependencies ──
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Production image ──
FROM python:3.12-slim AS production

# Security: run as non-root user
RUN groupadd -r bittu && useradd -r -g bittu -d /app -s /sbin/nologin bittu

# Install runtime dependencies only
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code
COPY . .

# Remove unnecessary files
RUN rm -rf __pycache__ .env .env.* .git .github tests/ *.md

# Set ownership
RUN chown -R bittu:bittu /app

# Switch to non-root user
USER bittu

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    APP_ENV=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

EXPOSE 8000

# Production server: Uvicorn with multiple workers
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--no-access-log", \
     "--timeout-keep-alive", "30", \
     "--limit-concurrency", "1000"]
