#!/usr/bin/env bash
# Push local assets/brand|stings|music to gs://$GCS_BUCKET/assets/
# (STANDARD class, so Cloud Run is not reading Coldline).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export GCS_BUCKET="${GCS_BUCKET:-poststream-assistant-archive}"
export GCS_ASSETS_PREFIX="${GCS_ASSETS_PREFIX:-assets}"

python cloud_job.py upload-assets
echo
echo "Cloud Run process-portraits restores this prefix at job start."
echo "  ${GCS_ASSETS_BUCKET:-$GCS_BUCKET}/${GCS_ASSETS_PREFIX}/"
