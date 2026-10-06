# ------------------------------------------------------------------------------
# 1. Base Stage: Environment, non-root user, and Python dependencies
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS base

WORKDIR /workspace

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Create a non-privileged user and group (default UID/GID 1000 for local dev)
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -g ${USER_GID} appgroup && \
    useradd -u ${USER_UID} -g appgroup -s /bin/bash -m appuser

# Set up python virutal environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install app dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------------------------
# 2. Development Stage: Target for VS Code Dev Container (no code baked in)
# ------------------------------------------------------------------------------
FROM base AS development

# Set to non root user
USER appuser

# Set python to venv
ENV PATH="/opt/venv/bin:$PATH"

CMD ["/bin/sh", "-c", "while sleep 1000; do :; done"]

# ------------------------------------------------------------------------------
# 3. Production Stage: Target for deployment (bakes code in /app)
# ------------------------------------------------------------------------------
FROM base AS production

WORKDIR /app

# Create the data directory for SQLite and set ownership
RUN mkdir -p /data && chown -R appuser:appgroup /app && chown -R appuser:appgroup /data

# Copy application source code
COPY --chown=appuser:appgroup . .

# Switch to non-root user
USER appuser

EXPOSE 8080

# Set python to venv
ENV PATH="/opt/venv/bin:$PATH"

# Run app using gunicorn
CMD ["gunicorn", \
    "--workers=2", \
    "--threads=2", \
    "--worker-class=gthread", \
    "--bind=0.0.0.0:8080", \
    "--forwarded-allow-ips=172.28.0.50", \
    "--access-logfile=-", \
    "--error-logfile=-", \
    "--preload", \
    "app:app"]