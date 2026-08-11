#!/usr/bin/env bash
# Example only — edit PROJECT_ID / GCS_BUCKET before running.
# Does not touch local data/ downloads.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-poststream-assistant}"
REGION="${REGION:-us-east1}"
GCS_BUCKET="${GCS_BUCKET:-poststream-assistant-archive}"
REPO="${REPO:-poststream-assistant}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/vod-job:latest"

gcloud config set project "$PROJECT_ID"
gcloud builds submit --tag "$IMAGE" .

# Long VODs (10–12h+): use 24h timeout, 4 CPU / 16Gi. Gen2 gives ~2× memory
# as container ephemeral disk (enough for ~12–30GB source + clips).
# Do NOT mount GCS FUSE as the yt-dlp download target.
gcloud run jobs deploy vod-archive-nightly \
  --image="$IMAGE" \
  --region="$REGION" \
  --execution-environment=gen2 \
  --task-timeout=24h \
  --memory=16Gi \
  --cpu=4 \
  # No auto-retry: a failed download must not silently re-pull the whole VOD.
  --max-retries=0 \
  # Note: YTDLP_FORMAT defaults in cloud_job.py (720p). Do not put commas in
  # --set-env-vars values without a custom delimiter (^@^...); gcloud splits on ",".
  --set-env-vars="GCS_BUCKET=${GCS_BUCKET},GCS_PREFIX=vods,TWITCH_CHANNEL=lolambrosek,RIOT_ID=twtv lolAmbrosek#twtv,RIOT_REGION=americas,WORK_DIR=/tmp/vod-work,TRANSCRIPT_SNAP=1" \
  --set-secrets="RIOT_API_KEY=RIOT_API_KEY:latest,TWITCH_CLIENT_ID=TWITCH_CLIENT_ID:latest,TWITCH_CLIENT_SECRET=TWITCH_CLIENT_SECRET:latest" \
  --command=python \
  --args="cloud_job.py,nightly,--limit,3,--cleanup"

echo "Execute dry-run with:"
echo "  gcloud run jobs execute vod-archive-nightly --region=$REGION --wait"
