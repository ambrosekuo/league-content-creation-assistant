# GCP Cloud Run Job deploy

Archives Twitch VODs + LoL event indexes to GCS.  
**Does not use or modify** your local `data/` download folder unless you explicitly pass `--dataset-dir`.

## Active project

| | |
|--|--|
| Project | `poststream-assistant` |
| Display name | Poststream Assistant |
| Region | `us-east1` |
| Bucket | `gs://poststream-assistant-archive` |
| Job | `vod-archive-nightly` |
| Console | https://console.cloud.google.com/run/jobs/details/us-east1/vod-archive-nightly?project=poststream-assistant |

## Layout in GCS

```text
gs://$GCS_BUCKET/$GCS_PREFIX/{vodId}/
  source.mp4
  metadata.json
  lol_events.json
  ingest.json          # if present
  clips/…              # optional later
  _upload_manifest.json
```

## One-time GCP setup

```bash
export PROJECT_ID=poststream-assistant
export REGION=us-east1
export GCS_BUCKET=poststream-assistant-archive
export REPO=poststream-assistant
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/vod-job:latest"

gcloud config set project "$PROJECT_ID"

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" || true

# Coldline-friendly bucket (adjust location as needed)
gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --location="$REGION" \
  --uniform-bucket-level-access || true

gcloud storage buckets update "gs://${GCS_BUCKET}" \
  --default-storage-class=COLDLINE || true
```

### Secrets

```bash
# Create secrets from local values (do this once; don't commit secrets)
echo -n "$RIOT_API_KEY" | gcloud secrets create RIOT_API_KEY --data-file=-
echo -n "$TWITCH_CLIENT_ID" | gcloud secrets create TWITCH_CLIENT_ID --data-file=-
echo -n "$TWITCH_CLIENT_SECRET" | gcloud secrets create TWITCH_CLIENT_SECRET --data-file=-
```

Use a Riot **Personal** key for unattended jobs (dev keys expire every 24h).

## Build & push

From **repo root** (`.dockerignore` excludes `data/`):

```bash
gcloud builds submit --tag "$IMAGE" .
```

## Create Cloud Run Job

```bash
gcloud run jobs create vod-archive-nightly \
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
```

> Large VODs (~12GB): raise memory / consider `--cpu-boost` or a Batch/GCE worker if `/tmp` space is insufficient. Cloud Run ephemeral disk can be increased in newer runtimes (`--gpu` not needed; look for `EmptyDir` / ephemeral storage flags for your gcloud version).

### Manual test (dry-run, no download)

```bash
gcloud run jobs update vod-archive-nightly \
  --region="$REGION" \
  --args="cloud_job.py,nightly,--limit,5,--dry-run"

gcloud run jobs execute vod-archive-nightly --region="$REGION" --wait
```

### Upload a finished local dataset (laptop → GCS)

Only after ingest completes (no `*.part` files):

```bash
pip install -r requirements-cloud.txt
export GCS_BUCKET=lolambrosek-stream-archive
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json   # or gcloud auth application-default login

python cloud_job.py upload-dataset --dataset-dir data/FINISHED_VOD_ID
```

The command **refuses** datasets that still have partial download files.

## Scheduler (nightly)

```bash
gcloud scheduler jobs create http vod-archive-daily \
  --location="$REGION" \
  --schedule="0 12 * * *" \
  --time-zone="America/New_York" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/vod-archive-nightly:run" \
  --http-method=POST \
  --oauth-service-account-email="PROJECT_NUMBER-compute@developer.gserviceaccount.com"
```

(Wire the invoker SA with `roles/run.invoker` on the Job.)

## Local commands (safe)

```bash
python cloud_job.py list-vods --limit 5
python cloud_job.py nightly --limit 5 --dry-run          # needs GCS_BUCKET + creds
python cloud_job.py gcs-list
```

Never point `nightly` at the repo `data/` folder while a download is in progress — default `WORK_DIR` is `/tmp/vod-work`.
