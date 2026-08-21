# Twitch VOD Ingestion Starter

End-to-end usage is in **[USAGE.md](USAGE.md)**. Default export is **reviewed picks** (rate in the review browser, then local Portraits → Decorate). Auto stitch is optional.

This is the first stage of the LolAmbrosek clip pipeline:

```text
Twitch VOD URL or local OBS recording
        ↓
Full source video
        ↓
Timestamped metadata
        ↓
16 kHz mono audio
        ↓
Transcription and highlight scoring (next stage)
```

## Cloud archive (GCS + Cloud Run Job)

Scaffolding lives in `cloud_job.py`, `storage_gcs.py`, `Dockerfile`, and `deploy/README.md`.

- Local downloads under `data/` are **not** used by the cloud job (`WORK_DIR=/tmp/vod-work`).
- `.dockerignore` excludes `data/` so image builds never package in-progress VODs.
- `upload-dataset` refuses folders that still contain `*.part` files.

See [deploy/README.md](deploy/README.md). Portrait stings/music are gitignored; Cloud Run pulls them from `gs://$GCS_BUCKET/assets/` (`python cloud_job.py upload-assets`).

## Local archive viewer

Browse processed videos in `data/` (and GCS when `GCS_BUCKET` is set), play them, keep/skip, and requeue Cloud Run jobs:

```bash
python -m pip install -r requirements-viewer.txt
python -m viewer
```

Opens at http://127.0.0.1:8787. Playback prefers local files; GCS-only objects can be pulled to `data/` first. Notes and keep/skip flags live in `data/_viewer/reviews.json`.

Clip review (individual event clips, not game weaves) is a separate page:

```text
http://127.0.0.1:8787/review/{vodId}
http://127.0.0.1:8787/review/{dayKey}/{vodId}
http://127.0.0.1:8787/review/aug15_2026/2847370420?filter=unreviewed
```

Keys: `1` reject · `2` keep · `3` excellent · `5` godly · `4` manual edit · `0` clear · `j`/`k` next/prev · space play · `f` fullscreen. Ratings save immediately to `data/_viewer/{dayKey}_{vodId}/selections.json` and `approved/{godly,excellent,keep,manual_edit,rejected}.json`. Default compilations use godly + excellent only.

Sync clips locally first, then **Portraits** (stitch + dry 9:16) and **Decorate** on the review page. Same as:

```bash
python review_export.py --dataset-dir data/{day}_{vodId}
python decorate_portrait.py --dataset-dir data/{day}_{vodId} --from-picks
```

## Local soundbytes (Freesound CC0)

`assets/stings/inbox/` is the drop folder for sounds you pick. `assets/stings/suggested/` is dopamine-like CC0 picks (cash register, coin). `assets/stings/intro/` is intro / ident stings only.

```bash
python fetch_stings.py --list
python fetch_stings.py --suggest
python fetch_stings.py --adopt ~/Downloads/209578__zott820__cash-register-purchase.wav
```

## Local GIFs (Wikimedia Commons)

`assets/gifs/inbox/` is the drop folder. `assets/gifs/suggested/` is filled from Wikimedia Commons: native GIFs, or a 2.5s ffmpeg clip of a CC video. No API key. Openverse has almost no climbing GIFs, so stills are not used.

```bash
python fetch_gifs.py --list
python fetch_gifs.py --suggest
python fetch_gifs.py --adopt ~/Downloads/some.gif
```

## Music pool (automated clips)

Don't hunt 500 stock tracks. Curate **20–40 vetted songs** in `assets/music/pool.json`, tag them with mood / energy / clip categories, and let the pipeline pick per classification.

**Where to source (best → fallback):**

| Source | Use for | Notes |
|--------|---------|-------|
| [Uppbeat](https://uppbeat.io/) | Short-form exports | Creator-focused; TikTok/YouTube/Twitch/IG. Search phonk, hyperpop, trap, jersey club, glitch, cyberpunk — not "gaming". |
| [StreamBeats](https://streambeats.com/) | Free starting library | 1,700+ tracks, sync license for streams/videos. Dig Hip-Hop → EDM → Hifi → Synthwave. |
| [Pretzel Rocks](https://pretzel.rocks/) | Live stream beds | Huge Twitch-safe catalog; double-check YouTube before baking into hundreds of automated exports. |
| Mixkit | Fallback / placeholders | Skews corporate vlog / 2018 montage. Lofi beds only via `fetch_music.py`. |

**Clip type → music direction** (encoded in `music_pool.py`):

| Clip type | Direction |
|-----------|-----------|
| outplay | dark trap / phonk / aggressive electronic |
| multikill | bass / trap / EDM |
| chase | DnB / high-BPM electronic |
| mistake | goofy electronic / jersey / bounce |
| reaction | very minimal beat |
| game_end | atmospheric / melodic electronic |
| ordinary | modern lo-fi / chill trap |

Drop downloads into `assets/music/inbox/`, adopt with tags, then the render path can do: **classification → pool pick → mix → export**.

Portrait renders (`render_portrait.py`, `cloud_job.py process-portraits`) stay **dry** (facecam + gameplay + KDA + blur bars; `--intro none --no-outro --music off`). Decoration is a second job:

```bash
python decorate_portrait.py --dataset-dir data/{day}_{vodId} --only gam14
# or: python cloud_job.py process-decorate-portraits --vod-id VIDEO_ID --dataset-dir data/{day}_{vodId}
```

That adds combos (from the landscape weave), captions, and the road-to-Challenger intro + rank-card outro onto `*_portrait_decorated.mp4`. Music is a later step:

```bash
python mix_portrait_music.py --dataset-dir data/{day}_{vodId} --from-picks --track a-game
```

```bash
python music_pool.py                                    # pool status + category coverage
python music_pool.py --pick outplay                     # test selection
python music_pool.py --mix clip.mp4 --category multikill
python music_pool.py --adopt ~/Downloads/phonk.mp3 \
  --id dark-phonk-01 --source uppbeat --license uppbeat-free \
  --energy 0.85 --categories outplay,multikill --mood dark,aggressive --bpm 142
```

Legacy Mixkit lofi fetcher (optional `--music lofi` on portraits):

```bash
python fetch_music.py --suggest                         # lofi → assets/music/suggested/
```

## Do I need to download the whole VOD?

For the first version, **yes**. Downloading the complete VOD gives you a stable local timeline and lets later stages repeatedly:

- transcribe the audio;
- inspect different frame regions;
- cut candidate clips without downloading the same segments again;
- rerun scoring as your algorithm improves;
- render vertical versions at high quality.

For future streams, record locally in OBS and skip the Twitch download. Use the Twitch URL as a fallback or for older broadcasts.

Only download VODs you own or are authorized to use.

## Requirements

- Python 3.11+
- FFmpeg available on your `PATH`
- Enough disk space for the VOD
- A current version of `yt-dlp`

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Confirm FFmpeg:

```bash
ffmpeg -version
ffprobe -version
```

## Basic usage

```bash
python ingest_vod.py "https://www.twitch.tv/videos/VIDEO_ID"
```

The script creates:

```text
data/
└── VIDEO_ID/
    ├── source.mp4
    ├── audio.wav
    ├── metadata.json
    ├── ingest.json
    └── thumbnail.*
```

The actual source extension may be `.mp4` or another container depending on the selected Twitch format.

## Authenticated or restricted VODs

Start without cookies. For a VOD that your browser can view but the downloader cannot access, use cookies from your own browser profile:

```bash
python ingest_vod.py \
  "https://www.twitch.tv/videos/VIDEO_ID" \
  --cookies-from-browser chrome
```

Firefox example:

```bash
python ingest_vod.py \
  "https://www.twitch.tv/videos/VIDEO_ID" \
  --cookies-from-browser firefox
```

Do not export or commit cookie files. Browser cookies can provide account access.

## Skip downloading when using an OBS recording

```bash
python ingest_vod.py \
  --local-file "D:\OBS\2026-08-06-stream.mkv" \
  --id "2026-08-06-stream"
```

The script will copy the file into the dataset and extract its audio.

To avoid duplicating a very large local recording, use:

```bash
python ingest_vod.py \
  --local-file "D:\OBS\2026-08-06-stream.mkv" \
  --id "2026-08-06-stream" \
  --no-copy
```

With `--no-copy`, `ingest.json` points to the original file.

## What this script does

1. Validates the supplied URL or local file.
2. Creates a deterministic folder for the VOD.
3. Downloads the best available combined Twitch format with `yt-dlp`, or registers a local recording.
4. Saves the extractor metadata.
5. Uses `ffprobe` to record duration and stream information.
6. Extracts mono, 16 kHz PCM audio for transcription.
7. Writes an `ingest.json` manifest that later workers can consume.

## Recommended next stage

Add a transcription worker that reads `ingest.json` and produces:

```text
transcript.json
```

Suggested schema:

```json
{
  "segments": [
    {
      "start": 123.4,
      "end": 127.8,
      "text": "wait, he actually flashed"
    }
  ]
}
```

After that, create 10-second timeline windows and attach:

- transcript text;
- normalized microphone energy;
- keyword/reaction hits;
- Twitch clip or stream-marker timestamps;
- chat velocity;
- visual changes in the kill feed.

Do not start with full gameplay understanding. The first useful system only needs to rank moments better than random scrubbing.

## Suggested workflow

```text
OBS recording exists?
├── Yes → ingest local recording
└── No  → provide Twitch VOD URL and download it

ingest.json
    ↓
transcribe.py
    ↓
score_windows.py
    ↓
extract_candidates.py
    ↓
review dashboard
```

## Storage estimate

VOD size depends on duration, resolution, bitrate, and selected format. Check free disk space before ingesting long broadcasts. Keep the original until the final clips are approved; afterward you can apply a retention policy.

A practical policy:

```text
raw VOD: retain 14–30 days
approved source clips: retain indefinitely
audio/transcript/metadata: retain indefinitely
rejected candidates: delete after review
```

## Troubleshooting

### `ffmpeg` is not found

Install FFmpeg and add its `bin` directory to your system `PATH`.

### Twitch extraction suddenly fails

Update `yt-dlp` first:

```bash
python -m pip install --upgrade yt-dlp
```

Twitch changes can temporarily break third-party extractors.

### The file already exists

The script is resumable. Pass `--force` to redownload and recreate derived files:

```bash
python ingest_vod.py "URL" --force
```

### Audio is huge

PCM WAV is intentionally easy for transcription tools but consumes space. Later, you can use FLAC without losing quality:

```bash
ffmpeg -i source.mp4 -vn -ac 1 -ar 16000 -c:a flac audio.flac
```

## Security

- Never commit browser cookies, OAuth tokens, or `.env` files.
- Keep downloads and metadata outside the public web root.
- Sanitize any future dashboard filenames and user-provided paths.
- Only process content you own or have permission to reuse.
