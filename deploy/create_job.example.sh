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

gcloud run jobs deploy vod-archive-nightly \
  --image="$IMAGE" \
  --region="$REGION" \
  --task-timeout=4h \
  --memory=4Gi \
  --cpu=2 \
  --max-retries=1 \
  --set-env-vars="GCS_BUCKET=${GCS_BUCKET},GCS_PREFIX=vods,TWITCH_CHANNEL=lolambrosek,RIOT_ID=lolAmbrosek#twtv,RIOT_REGION=americas,WORK_DIR=/tmp/vod-work" \
  --set-secrets="RIOT_API_KEY=RIOT_API_KEY:latest,TWITCH_CLIENT_ID=TWITCH_CLIENT_ID:latest,TWITCH_CLIENT_SECRET=TWITCH_CLIENT_SECRET:latest" \
  --command=python \
  --args="cloud_job.py,nightly,--limit,3,--cleanup"

echo "Execute dry-run with:"
echo "  gcloud run jobs execute vod-archive-nightly --region=$REGION --wait"
