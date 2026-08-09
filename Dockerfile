# Cloud Run Job image for Twitch VOD archive + LoL indexing.
# Build context excludes data/ via .dockerignore (won't touch local downloads).

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORK_DIR=/tmp/vod-work

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY . .

# Default: dry-run nightly discovery (override on the Job)
CMD ["python", "cloud_job.py", "nightly", "--limit", "5", "--dry-run"]
