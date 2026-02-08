# 🖥 QNAP Container Station 3 — Setup Guide

This walks you through setting up Plex Dedup entirely through the Container Station web interface. No SSH or command line needed.

---

## Before You Start — Gather Your Info

You'll need these values. Open each app's web UI and grab them:

| What | Where to find it |
|------|-----------------|
| **QNAP IP** | Control Panel → Network → look for your LAN IP (e.g., `192.168.1.100`) |
| **Plex Token** | See "Finding Your Plex Token" below |
| **Radarr API Key** | Radarr → Settings → General → API Key |
| **Sonarr API Key** | Sonarr → Settings → General → API Key |
| **Plex Movie Library name** | Open Plex → Libraries sidebar → note the exact name (e.g., "Movies") |
| **Plex TV Library name** | Same as above (e.g., "TV Shows") |
| **Media file path** | Plex → pick any movie → "Get Info" → note the file path |
| **OpenSubtitles API key** | opensubtitles.com → Consumers page (optional, for subtitles) |

### Finding Your Plex Token

1. Open Plex in your browser and sign in
2. Navigate to any movie or show
3. Click the **⋯** menu → **Get Info** → **View XML**
4. A new tab opens — look at the URL bar
5. At the end you'll see `?X-Plex-Token=XXXXXXXXXXXX`
6. Copy everything after the `=` — that's your token

---

## Step-by-Step Setup

### Step 1 — Open Container Station

Open your QNAP web interface and launch **Container Station** from the app drawer.

---

### Step 2 — Go to Applications

In the left sidebar, click **Applications**.

---

### Step 3 — Create New Application

Click the **+ Create** button in the top-right corner.

---

### Step 4 — Name Your Application

Enter the application name: **plex-dedup**

---

### Step 5 — Paste the YAML

1. Open the file `container-station-paste.yml` from this project
2. **Select all** the text and **copy** it
3. In Container Station, paste it into the **YAML editor**

---

### Step 6 — Edit Your Values

In the YAML editor, find and replace these placeholder values:

#### Required changes:

| Find this | Replace with |
|-----------|-------------|
| `YOUR_GITHUB_USERNAME` | Your actual GitHub username (in the `image:` line) |
| `YOUR_QNAP_IP` | Your QNAP's local IP, e.g., `192.168.1.100` (appears 3 times) |
| `PASTE_YOUR_PLEX_TOKEN_HERE` | Your Plex token |
| `PASTE_YOUR_RADARR_API_KEY_HERE` | Your Radarr API key |
| `PASTE_YOUR_SONARR_API_KEY_HERE` | Your Sonarr API key |
| `PASTE_YOUR_OPENSUBTITLES_API_KEY_HERE` | Your OpenSubtitles API key (or leave as-is to skip) |
| `your-opensubtitles-username` | Your OpenSubtitles username |
| `your-opensubtitles-password` | Your OpenSubtitles password |

#### Check these too:

| Setting | Default | Change if... |
|---------|---------|-------------|
| `PLEX_MOVIE_LIBRARY` | `Movies` | Your Plex movie library has a different name |
| `PLEX_TV_LIBRARY` | `TV Shows` | Your Plex TV library has a different name |
| Volume mount `/share/Multimedia` | `/share/Multimedia` | Your media is stored elsewhere (see step 7) |

---

### Step 7 — Fix the Volume Mount

This is the most important part. The container needs to see your media files at the **same path** that Plex uses.

1. In Plex, go to any movie → click **⋯** → **Get Info**
2. Look at the file path, e.g.: `/share/Multimedia/Movies/The Matrix (1999)/The.Matrix.1999.mkv`
3. The root folder is `/share/Multimedia`
4. In the YAML, make sure this line matches:

```yaml
- /share/Multimedia:/share/Multimedia
```

If your files are at `/share/Media/Movies/...` instead, change it to:
```yaml
- /share/Media:/share/Media
```

**Both sides of the `:` must be identical** — this ensures the container sees the same paths as Plex.

---

### Step 8 — Validate and Create

1. Click **Validate YAML** to check for errors
2. If it says valid, click **Create**
3. Wait for the image to download and the container to start (30-60 seconds)

---

### Step 9 — Open the Dashboard

Open your browser and go to:

```
http://YOUR_QNAP_IP:8585
```

You should see the Plex Dedup dashboard with connection status indicators for Plex, Radarr, Sonarr, and OpenSubtitles.

---

### Step 10 — First Run

1. Check that all four status dots are **green** (top right)
2. Leave **Dry Run** enabled (checked) — this is safe mode
3. Click **🔍 Scan for Duplicates**
4. Review the results — expand each item to see what gets kept vs. removed
5. Once you're happy with the plan, uncheck **Dry Run** and execute

---

## Managing the Container

### Viewing Logs

1. Go to Container Station → **Containers**
2. Click on **plex-dedup**
3. Click the **Logs** tab

### Stopping / Starting

1. Go to Container Station → **Applications**
2. Find **plex-dedup**
3. Use the **⏹ Stop** / **▶ Start** buttons

### Updating to a New Version

1. Go to Container Station → **Applications**
2. Click on **plex-dedup**
3. Click **Recreate** (this pulls the latest image and restarts)

Or manually:
1. Go to **Images** → find `ghcr.io/.../plex-dedup`
2. Click **Pull** to get the latest version
3. Go to **Applications** → **plex-dedup** → **Recreate**

---

## Troubleshooting

### All status dots are red
- Make sure Plex, Radarr, and Sonarr are running
- Check that `YOUR_QNAP_IP` is correct in the environment variables
- Try using `172.17.0.1` instead (Docker's host gateway)

### Plex is green but Radarr/Sonarr are red
- Verify the ports are correct (Radarr default: 7878, Sonarr: 8989)
- Check the API keys are copied correctly (no extra spaces)

### "Library not found" error
- Library names are case-sensitive
- Check the exact name in Plex (sidebar) and match it in the config

### Files not being deleted
- The volume mount path must exactly match what Plex reports
- The container needs write access to the media folder

### Container won't start
- Click on the container → **Logs** tab → check for error messages
- Most common: invalid YAML (wrong indentation) or image not found

### Port 8585 already in use
- Change the left side of the port mapping: `"9090:8585"` → then access via port 9090
