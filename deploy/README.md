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
gs://$GCS_BUCKET/vods/{dayKey}/{vodId}/   # e.g. vods/aug09_2026/2841932660/
  source.mp4
  metadata.json
  lol_events.json
  transcript.json              # event-window ASR (when TRANSCRIPT_SNAP=1)
  lol_events_snapped.json
  lol_clips/…
  lol_compilations/…           # per-game weaves (process-clips)
  lol_compilations_portrait/…  # 9:16 facecam+KDA (process-portraits)
  archive_manifest.json

gs://$GCS_BUCKET/work/{dayKey}/{vodId}/   # resume copies

gs://$GCS_BUCKET/assets/                  # portrait pack (STANDARD, not Coldline)
  brand/…                      # faces, heart_hands, streamers.json
  stings/inbox|suggested|intro # wav/mp3 (gitignored locally)
  music/catalog.json + suggested/*.mp3
```

Wav/mp3 stay out of git. Sync once (or after you change a sting):

```bash
python cloud_job.py upload-assets
```

`process-portraits` restores that prefix into `/app/assets` at job start. `--skip-assets` uses whatever is already on disk. Optional dedicated bucket: `GCS_ASSETS_BUCKET`.

`dayKey` comes from the VOD `timestamp` in America/New_York as `aug10_2026` (override with `GCS_DAY_KEY`). Legacy flat `vods/{vodId}/` is still readable for resume.
### Resume behavior (Cloud Run)

Cloud Run **local `/tmp` dies with the container**. Durable resume uses GCS:

1. Download from Twitch → local `/tmp/vod-work/{id}`
2. **Immediately upload** `source.mp4` (+ sidecars) to `vods/` and `work/`
3. Index + cut clips + write `archive_manifest.json`
4. On retry: if `source.mp4` exists in GCS, **skip Twitch** and restore into `/tmp`, then continue

Do **not** write yt-dlp HLS fragments through GCS FUSE (unreliable). FUSE is optional for reads; downloads use local disk, then `google-cloud-storage` upload.

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
  --execution-environment=gen2 \
  --task-timeout=24h \
  --memory=16Gi \
  --cpu=4 \
  --max-retries=2 \
  --set-env-vars="GCS_BUCKET=${GCS_BUCKET},GCS_PREFIX=vods,TWITCH_CHANNEL=lolambrosek,RIOT_ID=twtv lolAmbrosek#twtv,RIOT_REGION=americas,WORK_DIR=/tmp/vod-work" \
  --set-secrets="RIOT_API_KEY=RIOT_API_KEY:latest,TWITCH_CLIENT_ID=TWITCH_CLIENT_ID:latest,TWITCH_CLIENT_SECRET=TWITCH_CLIENT_SECRET:latest" \
  --command=python \
  --args="cloud_job.py,nightly,--limit,3,--cleanup"
```

> Long VODs (~12h): keep `--task-timeout=24h`, gen2 + ≥24Gi. `YTDLP_FORMAT` defaults to **1080p** in `cloud_job.py` (override with env). Cloud ingest uses `--skip-audio`.
>
> **Transcript snap (default ON):** after Riot index, cloud runs `transcribe_event_windows.py` (whisper only around KILL/DEATH/ASSIST, not the full VOD) → `snap_clips_to_transcript.py` → cut from snapped windows. Set `TRANSCRIPT_SNAP=0` to skip. Outputs `transcript.json` + `lol_events_snapped.json` to GCS with the archive.
>
> **Default archive path:** full 1080p HLS download → GCS checkpoint → Riot index → event-window ASR → **center-on-event snap** (~8s pre / 10s post, max 22s, overlap-merge nearby fights, frame-accurate reencode cuts) → cut. Prefer this over segmented downloads (section re-encode is far slower than HLS).
>
> **Recut existing archives** (no Twitch re-download): `python cloud_job.py recut-clips --vod-id <id> --cleanup` reuses `source.mp4` + `transcript.json` from GCS, replaces `lol_clips/`.
>
> **Daily compilation:** `python cloud_job.py process-daily --day-key aug12_2026 --top-k 12` restores every VOD’s `lol_clips/` for that day, ranks globally, keeps the top 12 (max 3 per game), stitches `daily_top12.mp4`, uploads to `vods/{dayKey}/_daily/`. Local: `--dataset-dir data/VOD_ID --vod-id VOD_ID` (no upload unless `--upload`).
>
> **Portrait layout job** (dry 9:16): `python cloud_job.py process-portraits --vod-id <id> --cleanup` downloads `lol_compilations/gam*.mp4`, restores assets, renders facecam+KDA+blur bars (`--intro none --no-outro --music off`) into `lol_compilations_portrait/`. Job: `vod-portrait-process`.
>
> **Portrait decorate job** (same Cloud Run image, different args): `python cloud_job.py process-decorate-portraits --vod-id <id> --cleanup` adds combos, captions, road-to-Challenger intro, rank-card outro → `*_portrait_decorated.mp4` (no music). Viewer: **Decorate portraits**. Mix music afterward with `mix_portrait_music.py` or the review **Music** tab.
>
> **Chain archive → clips** (no portraits):  
> `gcloud workflows run archive-clip-portrait --location=us-east1 --data='{"vodId":"YOUR_VOD"}'`.  
> Portraits are manual after weaves land: `gcloud run jobs execute vod-portrait-process --region=us-east1`  
> (or `clip-then-portrait` if you want stitch + portraits together).

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

## Local archive viewer

```bash
python -m pip install -r requirements-viewer.txt
python -m viewer
```

http://127.0.0.1:8787 — local `data/` + GCS bucket browser, keep/skip, requeue `vod-clip-process` / `vod-portrait-process` / recut.

## Local commands (safe)

```bash
python cloud_job.py list-vods --limit 5
python cloud_job.py nightly --limit 5 --dry-run          # needs GCS_BUCKET + creds
python cloud_job.py gcs-list
```

Never point `nightly` at the repo `data/` folder while a download is in progress — default `WORK_DIR` is `/tmp/vod-work`.
