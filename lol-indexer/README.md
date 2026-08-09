# LoL Match Event Indexer

Small Python CLI that retroactively indexes League of Legends matches via Riot’s official API and emits clip-ready timestamps for kills, deaths, assists, and major objectives.

Use it after a stream to map League event times onto an OBS recording and pick candidate clips.

## Requirements

- Python 3.11+
- A Riot developer API key (`RIOT_API_KEY`)
- Standard library only (no pip packages required)

## Setup

From the **repo root**, copy `.env.example` → `.env` and fill in values:

```bash
cp .env.example .env
# edit .env with RIOT_API_KEY, RIOT_ID, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
```

Scripts auto-load `.env` from the repo root.

```bash
cd lol-indexer
python lol_indexer.py --count 10
```

| Env var | Required | Description |
|---------|----------|-------------|
| `RIOT_API_KEY` | yes | Riot developer key |
| `RIOT_ID` | yes* | Default Riot ID (`GameName#TAG`) |
| `RIOT_REGION` | no | Regional routing (default `americas`) |

\*Or pass `--riot-id` on the CLI. CLI flags override env.

Never commit or print your API key.

## Usage

```bash
python lol_indexer.py --count 10
# or override the account for one run:
python lol_indexer.py --riot-id "SomeoneElse#NA1" --count 5
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--riot-id` | `RIOT_ID` env | `GameName#TAG` |
| `--count` | `10` | Recent matches to fetch (max 100) |
| `--region` | `RIOT_REGION` / `americas` | Riot regional routing |
| `--start-time` | — | ISO-8601 window start |
| `--end-time` | — | ISO-8601 window end |
| `--output` | `events.json` | JSON output path |
| `--obs-start` | — | OBS recording start (ISO-8601) |
| `--pre-roll` | `15` | Seconds before event for clip start |
| `--post-roll` | `10` | Seconds after event for clip end |

### Map onto an ingested Twitch VOD (primary workflow)

After `ingest_vod.py` has created `data/<VIDEO_ID>/`:

```bash
python lol_indexer.py \
  --vod-dir ../data/2833454760 \
  --output events.json
```

This reads the VOD start from `metadata.json` (`timestamp`), filters League matches to that stream window, and stamps every KDA event with:

- `vodOffsetSeconds` / `vodTime` — position inside the Twitch VOD
- `clipStart` / `clipEnd` — suggested cut range (`--pre-roll` / `--post-roll`)

### OBS mapping example

```powershell
python lol_indexer.py `
  --count 20 `
  --obs-start "2026-08-09T13:02:15-04:00" `
  --output events.json
```

Use either `--vod-dir` or `--obs-start`, not both.

## Output

### Console

```text
GameName#TAG

NA1_123456789
LeBlanc — 12/3/8 — WIN

03:42  ASSIST
06:31  KILL
09:18  DEATH
```

### JSON (`events.json`)

```json
{
  "player": {
    "riotId": "GameName#TAG",
    "puuid": "..."
  },
  "generatedAt": "2026-08-09T19:00:00Z",
  "matches": [
    {
      "matchId": "NA1_123456789",
      "champion": "Leblanc",
      "win": true,
      "kills": 12,
      "deaths": 3,
      "assists": 8,
      "gameStartTimestamp": 1786300000000,
      "gameDurationSeconds": 1842,
      "events": [
        {
          "type": "KILL",
          "gameTimeMs": 391234,
          "gameTime": "06:31"
        }
      ]
    }
  ]
}
```

Optional objective event types (when the player’s team is responsible): `DRAGON`, `BARON`, `HERALD`, `HORDE`, `TOWER`, `INHIBITOR`.

## How it works

1. Resolve Riot ID → PUUID (`Account-V1`)
2. Fetch recent match IDs (`Match-V5`)
3. For each match, load details + timeline
4. Find the player’s `participantId`
5. Emit `KILL` / `DEATH` / `ASSIST` (and optional objectives) with `mm:ss` game times
6. Optionally map onto an OBS recording timeline

## Notes

- Regional routing must match the account’s shard cluster (`americas` for NA/BR/LAN/LAS, `europe` for EUW/EUNE/TR/RU, etc.).
- Time filters use Riot’s `startTime` / `endTime` (epoch **seconds**) when provided, then refine with match metadata.
- Timeline failures are reported per match; indexing continues for the rest.
- Rate limits (`429`) retry using `Retry-After` with a small bounded retry count.
