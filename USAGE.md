# Pipeline usage

**Default export is reviewed picks.** Archive and cut every KDA clip, rate godly / excellent in the review browser, then kick **Portraits → Decorate → Music** there (local, after a sync). Auto stitch is the optional full-VOD pass.

```text
Twitch VOD / OBS
        │
        ▼
  1. Archive + cut          lol_clips/gNN_Champ/c*.mp4
        │
        ├──────────────────────────────┐
        ▼                              ▼
  2. Review stitch (default)     2b. Auto stitch
      (godly + excellent)            (scored top-k)
      lol_compilations_picks/        lol_compilations/
        │                              │
        ▼                              ▼
  3. Dry portrait                  3. Dry portrait
      lol_compilations_picks_portrait/  lol_compilations_portrait/
        │                              │
        ▼                              ▼
  4. Decorate (no music)           4. Decorate (no music)
      *_portrait_decorated.mp4       *_portrait_decorated.mp4
        │
        ▼
  5. Music (choose a track)
      post/*_portrait_music.mp4
        │
        ▼
  6. Post (private / draft)
      YouTube private · TikTok inbox
```

`keep` is a review rating only. Compilations do **not** use it unless you pass `--rating keep` yourself.

---

## Folders

Dataset root is `data/{dayKey}_{vodId}/` locally, or `gs://$GCS_BUCKET/vods/{dayKey}/{vodId}/` in GCS.

| Folder | Who writes it | What it is |
|---|---|---|
| `source.mp4`, `lol_events.json` | Archive | Full VOD + LoL timeline |
| `lol_clips/` | Archive / recut | Every KILL/DEATH/ASSIST clip, per game |
| `lol_compilations_picks/` | Review stitch | Godly + excellent weaves only |
| `lol_compilations_picks_portrait/` | Picks portraits | Dry 9:16 of reviewed games |
| `lol_compilations/` | Auto stitch | Ranked weaves (default top 5 / game) |
| `lol_compilations_portrait/` | Auto portraits | Dry 9:16 of auto weaves |
| `*_portrait_decorated.mp4` | Decorate job | Combos + captions + intro/outro (no music) |
| `*_portrait_music.mp4` | Music job | Decorated + chosen pool track |
| `post/{stem}.post.json` | Post job | Upload title / tags + YouTube and TikTok ids |

Review ratings live in `data/_viewer/{dayKey}_{vodId}/approved/{godly,excellent,keep,manual_edit,rejected}.json`. The mp4s stay in `lol_clips/`.

---

## Shared trunk

### 1. Archive + cut

Ingest, index LoL events, cut KDA clips.

**Viewer:** Archive VOD · Recut clips (no Twitch re-download)

```bash
python cloud_job.py process-vod --vod-id VIDEO_ID --cleanup --fast
# already archived:
python cloud_job.py recut-clips --vod-id VIDEO_ID --cleanup --fast
```

Local ingest without Cloud Run:

```bash
python ingest_vod.py "https://www.twitch.tv/videos/VIDEO_ID"
```

Output: `lol_clips/g14_Leblanc_vsQiyana/c01_….mp4` (and the rest of the KDA set).

---

## Default — reviewed (godly + excellent)

Same `lol_clips/` as auto. You rate clips in the review UI, then stitch **only** those ratings. Default queues: **godly + excellent**. `keep` is parked, not compiled.

This path is **local for now**: sync clips into `data/`, review, then run portraits, decorate, and music from the review page.

### 2. Review

```text
http://127.0.0.1:8787/review/{dayKey}/{vodId}?filter=unreviewed
```

Keys: `1` reject · `2` keep · `3` excellent · `5` godly · `4` manual edit · `0` clear.

If clips are still on GCS, **Sync local** (current filter) or **Sync picks** (godly + excellent only). Portraits stays disabled until those picks exist on disk.

### 3. Dry portrait

**Review page:** Portraits

Stitches godly + excellent into `lol_compilations_picks/`, then dry 9:16 into `lol_compilations_picks_portrait/` (facecam, gameplay, blur bars, cam-hole fill, champion tracking, KDA PIP). No intro, outro, or music.

```bash
python review_export.py --dataset-dir data/{day}_{vodId}
# stitch only:
python review_export.py --dataset-dir data/{day}_{vodId} --skip-portrait
# one game:
python review_export.py --dataset-dir data/{day}_{vodId} --only g14 --force
```

### 4. Decorate portrait

**Review page:** Decorate

Order: combos (from the landscape weave) → captions → road-to-Challenger intro + rank-card outro. No music. Review Decorate only touches `lol_compilations_picks_portrait/`.

```bash
python decorate_portrait.py --dataset-dir data/{day}_{vodId} --from-picks --only gam14
# or after export:
python review_export.py --dataset-dir data/{day}_{vodId} --portrait-only --decorate
```

Skip a layer while testing: `--skip-combos` · `--skip-captions` · `--skip-wrap`.

### 5. Music

**Review page:** Music

Pick a pool track, then mix it under decorated portraits. Writes `*_portrait_music.mp4` next to the decorated file. Batch skips files that already have music; **Mix** / **Remix** on a card always remakes that game.

```bash
python mix_portrait_music.py --dataset-dir data/{day}_{vodId} --from-picks --track a-game
python mix_portrait_music.py --dataset-dir data/{day}_{vodId} --from-picks --track dance-zero-nc --only gam14 --force
python music_pool.py                                    # list ids
```

### 6. Post

**Review page:** Post

Uploads the music mix in `post/` and stops short of publishing: YouTube lands **private**, TikTok lands in your **draft inbox**. You do the final look and the actual post from your phone.

Title comes from the Hooks tab selection if there is one, otherwise it falls back to the matchup and lane from Riot data (`LeBlanc vs Fizz Mid`). Click a title in the Post table to type your own; a hand-typed title sticks and later runs will not overwrite it. Each upload writes `post/{stem}.post.json` with the title, description, hashtags and the returned ids, so re-runs skip anything already sent.

```bash
pip install -r requirements-post.txt
python post_short.py --login youtube                    # once, opens a browser
python post_short.py --login tiktok                     # once, paste the redirect URL
python post_short.py --status                           # what is configured / authorized

python post_short.py --dataset-id VIDEO_ID --from-picks --dry-run          # titles only
python post_short.py --dataset-id VIDEO_ID --from-picks --youtube --tiktok
python post_short.py --dataset-id VIDEO_ID --from-picks --youtube --only gam14 --force
python post_short.py --input path/to/gam14_..._portrait_music.mp4 --youtube --title "..."
```

Drop the Google OAuth client json into `secrets/` and it is found automatically. TikTok keys go in `.env` (see `.env.example`). Cached tokens land next to the client json. `secrets/` and `.secrets/` are both gitignored.

| | YouTube | TikTok |
|---|---|---|
| Credential | `secrets/client_secret*.json`, **Desktop app** — an `AIza…` API key cannot upload | Client key + secret in `.env`, `video.upload` scope |
| Lands as | Private video, category Gaming, `madeForKids: false` | Draft in your inbox |
| Prefilled | Title, description, tags | Video only — TikTok drafts take the caption in-app |
| Finish in | YouTube Studio | TikTok app |
| Limits | 1600 quota units per upload of 10,000/day, so ~6 uploads/day | 5 pending inbox uploads per 24h, 6 init calls/min |

Flags worth knowing: `--privacy unlisted|public` · `--category-id` · `--made-for-kids` · `--limit N` · `--meta-only` (write sidecars, upload nothing).

---

## Optional — auto (all clips, ranked)

Uses **every** cut clip, then **scores** them and keeps the best N per game (default **5**). This is not “stitch the entire folder unfiltered.”

`--top-k 0` or `--no-rank` stitches every clip (true all-clips).

### 2b. Stitch

**Archive viewer:** Stitch weaves (auto)

```bash
python cloud_job.py process-clips --vod-id VIDEO_ID --cleanup --clean-work
# local:
python stitch_game_clips.py --dataset-dir data/{day}_{vodId}
```

Writes `lol_compilations/gam14_leblanc_vs_qiyana_win.mp4` plus lobby PNG/meta sidecars.

### 3b. Dry portrait

**Archive viewer:** Portraits (auto)

```bash
python cloud_job.py process-portraits --vod-id VIDEO_ID --cleanup --clean-work --force \
  --track-champion --game-zoom 0.65 --cam-hole fill --intro none --no-outro --music off
# local:
python render_portrait.py \
  --input data/{day}_{vodId}/lol_compilations/gam14_leblanc_vs_qiyana_win.mp4 \
  --output data/{day}_{vodId}/lol_compilations_portrait/gam14_leblanc_vs_qiyana_win_portrait.mp4 \
  --intro none --no-outro --music off --track-champion --game-zoom 0.65 --cam-hole fill
```

### 4b. Decorate portrait

**Archive viewer:** Decorate (auto)

Combos + captions + wrap. No music.

```bash
python cloud_job.py process-decorate-portraits --vod-id VIDEO_ID --cleanup --clean-work --force
python decorate_portrait.py --dataset-dir data/{day}_{vodId} --only gam14
python mix_portrait_music.py --dataset-dir data/{day}_{vodId} --track a-game --only gam14
```

---

## How the two avenues differ

| | Reviewed (default) | Auto |
|---|---|---|
| Clip source | All KDA cuts | Same cuts |
| Which clips enter the weave | You picked godly + excellent | Score, top 5 / game |
| Kickoff | Review page, local | Archive viewer / Cloud Run |
| Stitch command | `review_export.py` | `process-clips` |
| Weave folder | `lol_compilations_picks/` | `lol_compilations/` |
| Portrait folder | `lol_compilations_picks_portrait/` | `lol_compilations_portrait/` |

Run review when you have rated a session and only want the bangers in the Short. Run auto for a full-VOD pass without sitting in review.

`Clips → portraits (auto)` in the archive viewer is auto stitch then dry portrait. Decorate is still a separate click.

---

## Viewer job map

| Where | Button | Step |
|---|---|---|
| Review | Clips | Rate already-cut KDA clips |
| Review | Stitched | Godly+excellent weaves; **Stitch** if missing |
| Review | Portraits | Dry 9:16; **Make portraits** if missing |
| Review | Decorate | Combos + captions + wrap (no music) |
| Review | Music | Mix a chosen pool track onto decorated |
| Review | Post | Private YouTube upload + TikTok draft |
| Review | Sync local / Sync picks | Pull `lol_clips/` into `data/` |
| Archive | Archive VOD | 1 ingest + cut |
| Archive | Recut clips | 1 recut from `source.mp4` |
| Archive | Stitch weaves (auto) | Auto stitch |
| Archive | Portraits (auto) | Dry layout of auto weaves |
| Archive | Decorate (auto) | Decorate auto dry portraits |
| Archive | Clips → portraits (auto) | Auto stitch then dry portrait |

---

## One-game smoke test

```bash
# reviewed picks (needs ratings):
python review_export.py --dataset-dir data/aug17_2026_2849217240 --only g14 --force
python decorate_portrait.py --dataset-dir data/aug17_2026_2849217240 --from-picks --only gam14
python mix_portrait_music.py --dataset-dir data/aug17_2026_2849217240 --from-picks --track a-game --only gam14

# auto weaves:
python stitch_game_clips.py --dataset-dir data/aug17_2026_2849217240 --only g14 --force
python cloud_job.py process-portraits --vod-id 2849217240 \
  --dataset-dir data/aug17_2026_2849217240 --skip-assets --only gam14 --force
python decorate_portrait.py --dataset-dir data/aug17_2026_2849217240 --only gam14
python mix_portrait_music.py --dataset-dir data/aug17_2026_2849217240 --track a-game --only gam14
```

Watch the dry file first (`*_portrait.mp4`), then decorated (`*_portrait_decorated.mp4`), then music (`*_portrait_music.mp4`).
