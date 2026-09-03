FROM python:3.14.7-slim
WORKDIR /app

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Create a non-privileged user and group
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

# Install app dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create the data directory for SQLite and set ownership
RUN mkdir -p /app/data && chown -R appuser:appgroup /app

# Copy application source code
COPY --chown=appuser:appgroup . .

# Switch to non-root user
USER appuser

EXPOSE 8080

# Run app using gunicorn
CMD ["gunicorn", \
     "--workers=2", \
     "--threads=2", \
     "--worker-class=gthread", \
     "--bind=0.0.0.0:8080", \
     "--forwarded-allow-ips=172.28.0.50", \
     "--access-logfile=-", \
     "--error-logfile=-", \
     "main:app"]