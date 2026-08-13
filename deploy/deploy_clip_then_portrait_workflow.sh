#!/usr/bin/env bash
# Deploy vod-portrait-process + Cloud Workflow that runs clips then portraits.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-poststream-assistant}"
REGION="${REGION:-us-east1}"
GCS_BUCKET="${GCS_BUCKET:-poststream-assistant-archive}"
REPO="${REPO:-poststream-assistant}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/vod-job:latest"
VOD_ID="${VOD_ID:-2843745368}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com workflows.googleapis.com --project="$PROJECT_ID"

# Portrait job (9:16 TikTok/Shorts from landscape weaves)
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

# Workflow SA needs permission to run both jobs
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
WF_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud run jobs add-iam-policy-binding vod-clip-process \
  --region="$REGION" \
  --member="serviceAccount:${WF_SA}" \
  --role="roles/run.developer" >/dev/null || true
gcloud run jobs add-iam-policy-binding vod-portrait-process \
  --region="$REGION" \
  --member="serviceAccount:${WF_SA}" \
  --role="roles/run.developer" >/dev/null || true

gcloud workflows deploy clip-then-portrait \
  --location="$REGION" \
  --source="${ROOT}/deploy/workflow_clip_then_portrait.yaml" \
  --service-account="$WF_SA"

cat <<EOF

Deployed:
  job:      vod-portrait-process (default vod ${VOD_ID})
  workflow: clip-then-portrait

Run both in order (clips → portraits):
  gcloud workflows run clip-then-portrait \\
    --location=${REGION} \\
    --data='{"vodId":"${VOD_ID}"}'

Portrait only (after clips already finished):
  gcloud run jobs execute vod-portrait-process --region=${REGION}

Override VOD on portrait job:
  VOD_ID=OTHER $0
EOF
