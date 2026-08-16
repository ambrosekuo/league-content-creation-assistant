#!/usr/bin/env bash
# Create / update the portrait post-process Cloud Run job.
# Reads lol_compilations/*_weave.mp4 → writes lol_compilations_portrait/*_portrait.mp4
# No Twitch download / no source.mp4.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-poststream-assistant}"
REGION="${REGION:-us-east1}"
GCS_BUCKET="${GCS_BUCKET:-poststream-assistant-archive}"
REPO="${REPO:-poststream-assistant}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/vod-job:latest"
VOD_ID="${VOD_ID:-2839185169}"

gcloud config set project "$PROJECT_ID"

gcloud run jobs deploy vod-portrait-process \
  --image="$IMAGE" \
  --region="$REGION" \
  --execution-environment=gen2 \
  --task-timeout=4h \
  --memory=16Gi \
  --cpu=8 \
  --max-retries=0 \
  --set-env-vars="GCS_BUCKET=${GCS_BUCKET},GCS_PREFIX=vods,TWITCH_CHANNEL=lolambrosek,RIOT_REGION=americas,WORK_DIR=/tmp/vod-work,GCS_DAY_TZ=America/New_York" \
  --command=python \
  --args="cloud_job.py,process-portraits,--vod-id,${VOD_ID},--cleanup,--clean-work,--force,--preset,veryfast,--crf,20,--track-champion"

echo "Execute with:"
echo "  gcloud run jobs execute vod-portrait-process --region=$REGION"
echo "Override VOD:"
echo "  VOD_ID=OTHER ./deploy/create_portrait_job.example.sh"
echo
echo "Sync stings/music/brand stills (once, or after you change a file):"
echo "  python cloud_job.py upload-assets"
echo
echo "Local (no Cloud Run):"
echo "  python cloud_job.py process-portraits --vod-id ${VOD_ID} --cleanup --clean-work --force"
echo "  # or against a local folder:"
echo "  python cloud_job.py process-portraits --vod-id ${VOD_ID} --dataset-dir /path/with/lol_compilations --skip-assets"
