#!/usr/bin/env bash
# Create / update the lightweight clip post-process Cloud Run job.
# Separate from vod-archive-nightly (no Twitch download / no source.mp4).

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-poststream-assistant}"
REGION="${REGION:-us-east1}"
GCS_BUCKET="${GCS_BUCKET:-poststream-assistant-archive}"
REPO="${REPO:-poststream-assistant}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/vod-job:latest"
VOD_ID="${VOD_ID:-2839185169}"

gcloud config set project "$PROJECT_ID"

gcloud run jobs deploy vod-clip-process \
  --image="$IMAGE" \
  --region="$REGION" \
  --execution-environment=gen2 \
  --task-timeout=2h \
  --memory=16Gi \
  --cpu=8 \
  --max-retries=0 \
  --set-env-vars="GCS_BUCKET=${GCS_BUCKET},GCS_PREFIX=vods,TWITCH_CHANNEL=lolambrosek,RIOT_ID=twtv lolAmbrosek#twtv,RIOT_REGION=americas,WORK_DIR=/tmp/vod-work,GCS_DAY_TZ=America/New_York" \
  --set-secrets="RIOT_API_KEY=RIOT_API_KEY:latest" \
  --command=python \
  --args="cloud_job.py,process-clips,--vod-id,${VOD_ID},--cleanup,--clean-work"

echo "Execute with:"
echo "  gcloud run jobs execute vod-clip-process --region=$REGION"
echo "Override VOD:"
echo "  VOD_ID=OTHER ./deploy/create_clip_job.example.sh"
