# 🎬 Plex Dedup

**Automatically find and remove duplicate movies & TV episodes, and download missing Swedish subtitles — all from one dashboard.**

Designed to run on QNAP NAS via Container Station, but works anywhere Docker runs.

## The Problem

When using Usenet + Radarr/Sonarr + Plex, you end up with:
- **Duplicate files** — Radarr/Sonarr re-download movies and episodes, wasting disk space
- **Missing subtitles** — Swedish (or other language) .srt files aren't always included

Fixing this manually means filtering Plex for duplicates, unmonitoring in Radarr/Sonarr, deleting files, and hunting for subtitles on OpenSubtitles. Plex Dedup automates all of it.

## What This Does

### 🗂 Deduplication
1. **Scans Plex** — Finds all movies and episodes with multiple files
2. **Scores quality** — Resolution, codec, bitrate, source type (Remux > Blu-ray > WEB-DL)
3. **Keeps the best** — Configurable strategy (best quality, largest, newest)
4. **Unmonitors in Radarr/Sonarr** — Stops the re-download cycle
5. **Deletes duplicates** — Removes inferior copies from disk

### 🗒 Subtitle Sync
1. **Scans Plex** — Finds movies and episodes missing Swedish (or other) subtitles
2. **Searches OpenSubtitles** — Uses file hash + IMDB/TMDB IDs for accurate matching
3. **Downloads .srt files** — Places them next to the media file with correct naming
4. **Plex auto-detects** — Subtitles appear in Plex automatically

## Quick Start (Docker on QNAP)

### 1. Pull the image (easiest)

The image is automatically built for both `amd64` and `arm64`, so it works on Intel and ARM-based QNAP models.

```bash
docker pull ghcr.io/YOUR_GITHUB_USERNAME/plex-dedup:latest
```

Or use Docker Compose (recommended) — see step 3 below.

### 2. Configure

Copy `docker-compose.yml` and `.env.example` to your QNAP (e.g., `/share/Container/plex-dedup/`):

```bash
cp .env.example .env
nano .env
```

Fill in your Plex token, Radarr/Sonarr API keys, and OpenSubtitles credentials.

### 3. Edit volume mounts

Open `docker-compose.yml` and:
1. Replace `YOUR_GITHUB_USERNAME` in the `image:` line with your actual GitHub username
2. Update the volume mount to match your media path:

```yaml
volumes:
  # Must match the paths Plex reports for your files
  - /share/Multimedia:/share/Multimedia
```

### 4. Deploy

**Option A — QNAP Container Station UI:**
1. Open Container Station → Create → Create Application
2. Paste the contents of `docker-compose.yml`
3. Click Create

**Option B — SSH:**
```bash
cd /share/Container/plex-dedup
docker-compose up -d
```

### 5. Open Dashboard

Navigate to `http://your-qnap-ip:8585`

## CLI Usage

The tool also works as a standalone CLI (without Docker):

```bash
pip install -r requirements.txt
cp .env.example .env && nano .env

# Scan for duplicate movies and episodes
python cli.py dedup

# Scan TV shows only, live mode
python cli.py dedup --type tv --live

# Download missing Swedish subtitles
python cli.py subtitles

# Download subs for movies only, max 25 items
python cli.py subtitles --type movies --limit 25 --live

# Launch web dashboard
python cli.py web
```

## Finding Your Tokens & Keys

### Plex Token
1. Sign into Plex Web, play any media
2. Open browser Dev Tools → Network tab
3. Find `X-Plex-Token` parameter in any request to your server

Or check your Plex preferences file:
- **Linux:** `~/.local/share/plexmediaserver/Preferences.xml`
- **QNAP:** `/share/CACHEDEV1_DATA/.qpkg/PlexMediaServer/Library/Plex Media Server/Preferences.xml`

### Radarr / Sonarr API Key
Settings → General → API Key (in each app's web UI)

### OpenSubtitles API Key
1. Create an account at [opensubtitles.com](https://www.opensubtitles.com)
2. Go to [Consumers](https://www.opensubtitles.com/consumers) and create an API consumer
3. Copy the API key

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `PLEX_URL` | `http://localhost:32400` | Plex server URL |
| `PLEX_TOKEN` | — | Plex authentication token |
| `PLEX_MOVIE_LIBRARY` | `Movies` | Plex movie library name |
| `PLEX_TV_LIBRARY` | `TV Shows` | Plex TV library name |
| `RADARR_URL` | `http://localhost:7878` | Radarr URL |
| `RADARR_API_KEY` | — | Radarr API key |
| `SONARR_URL` | `http://localhost:8989` | Sonarr URL |
| `SONARR_API_KEY` | — | Sonarr API key |
| `OPENSUBTITLES_API_KEY` | — | OpenSubtitles.com API key |
| `OPENSUBTITLES_USERNAME` | — | OpenSubtitles username |
| `OPENSUBTITLES_PASSWORD` | — | OpenSubtitles password |
| `SUBTITLE_LANGUAGES` | `sv,en` | Comma-separated ISO 639-1 codes |
| `DRY_RUN` | `true` | Preview mode — no files changed |
| `KEEP_STRATEGY` | `best_quality` | `best_quality`, `largest_file`, or `newest` |
| `AUTO_UNMONITOR` | `true` | Unmonitor in Radarr/Sonarr after dedup |
| `DELETE_FILES` | `true` | Delete duplicate files from disk |
| `RECYCLE_BIN` | — | Move files here instead of deleting |
| `TZ` | `Europe/Stockholm` | Timezone |
| `WEB_PORT` | `8585` | Dashboard port |

## Quality Scoring

The `best_quality` strategy ranks files by:

| Factor | Weight | Details |
|--------|--------|---------|
| Resolution | High | 4K > 1080p > 720p > SD |
| Source | High | Remux > Blu-ray > WEB-DL > WEBRip > HDTV |
| Bitrate | Medium | Higher = better |
| Video codec | Low | AV1 > HEVC > H.264 |
| Audio codec | Low | TrueHD/Atmos > DTS-HD > DTS > EAC3 > AAC |

## Volume Mount Guide (QNAP)

The container must see your media files at the **same paths** that Plex reports. Check a movie in Plex → "Get Info" → file path. Then set up mounts accordingly:

```yaml
# If Plex shows: /share/Multimedia/Movies/Movie (2024)/movie.mkv
volumes:
  - /share/Multimedia:/share/Multimedia

# If using separate shares:
volumes:
  - /share/Movies:/share/Movies
  - /share/TV:/share/TV

# If using host network mode (services on same QNAP):
network_mode: host
```

## Web Dashboard Features

- **Tabbed interface** — Switch between Dedup and Subtitles
- **Connection indicators** — Live status for Plex, Radarr, Sonarr, OpenSubtitles
- **Scan all or selective** — Movies only, TV only, or both
- **Expandable details** — See exactly which files get kept/removed and why
- **Per-item or bulk execution** — Process one movie or select many at once
- **Dry run toggle** — Safe preview mode, always on by default
- **Subtitle scanner** — Find and download missing Swedish subtitles

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Container can't reach Plex/Radarr/Sonarr | Use host IP (not `localhost`). Try `network_mode: host`. |
| "Library not found" | Check library names match exactly (case-sensitive) |
| File paths don't match | Volume mount must mirror the paths Plex reports |
| Subtitles not appearing in Plex | Trigger a library scan. Files must be `Movie.sv.srt` format. |
| OpenSubtitles download limit | Free accounts get ~20 downloads/day. Wait or upgrade. |
| QNAP Container Station can't build | Pre-build on your PC: `docker build -t plex-dedup .` then export/import. |

## Project Structure

```
plex-dedup/
├── .env.example            # Configuration template
├── Dockerfile              # Docker image definition
├── docker-compose.yml      # QNAP-ready compose file
├── requirements.txt        # Python dependencies
├── config.py               # Configuration management
├── plex_client.py          # Plex API (movies + TV)
├── radarr_client.py        # Radarr API (movie unmonitoring)
├── sonarr_client.py        # Sonarr API (episode unmonitoring)
├── opensubtitles_client.py # OpenSubtitles REST API
├── subtitle_manager.py     # Subtitle scanning & downloading
├── dedup_engine.py         # Core dedup logic
├── app.py                  # Flask web dashboard
├── cli.py                  # CLI interface
└── templates/
    └── index.html          # Dashboard UI
```

## CI/CD

The project uses GitHub Actions to automatically build and publish multi-arch Docker images on every push to `main` and on tagged releases.

Images are published to:
- **GitHub Container Registry:** `ghcr.io/YOUR_GITHUB_USERNAME/plex-dedup`
- **Docker Hub** (optional): `YOUR_DOCKERHUB_USERNAME/plex-dedup`

To create a release, just tag and push:
```bash
git tag v1.0.0
git push origin v1.0.0
```

This generates images tagged as `v1.0.0`, `v1.0`, `v1`, and `latest`.

### Enabling Docker Hub (optional)

If you want to also publish to Docker Hub, add these secrets in your GitHub repo under Settings → Secrets → Actions:
- `DOCKERHUB_USERNAME` — your Docker Hub username
- `DOCKERHUB_TOKEN` — a Docker Hub access token (create at hub.docker.com → Account Settings → Security)

GHCR works out of the box with no extra setup.

## License

MIT
