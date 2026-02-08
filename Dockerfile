FROM python:3.12-slim

LABEL maintainer="plex-dedup"
LABEL description="Plex Dedup — Duplicate Movie/Episode Manager with Subtitle Sync"

# Set environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Stockholm

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory for logs
RUN mkdir -p /data/logs

# Expose web port
EXPOSE 8585

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8585/api/status', timeout=5)" || exit 1

# Default: launch web dashboard
# Override with: docker run ... plex-dedup python cli.py dedup --live
CMD ["python", "app.py"]
