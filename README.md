# ZpotiBOT

A Discord bot for Spotify music search, voice playback, and playback status synchronization.

## Features

- **Spotify-First Search**: Type `/play <song or artist>` or paste a Spotify track link — the bot queries Spotify Web API directly for metadata, cover art, and duration.
- **Voice Playback**: Joins your voice channel and streams high-quality audio using `imageio-ffmpeg` and `yt-dlp`.
- **10-Minute Auto-Disconnect**: Automatically leaves the voice channel if left alone with no listeners for more than 10 minutes to save server resources.
- **Spotify Auto-Sync**: A background poller (every 10 seconds by default) detects when you change songs on Spotify and can automatically transition the voice channel stream.
- **Rich Presence**: The bot's Discord activity dynamically reflects what you are listening to on Spotify.

## Slash Commands

| Command | Description |
| :--- | :--- |
| `/play [query]` | Searches Spotify for a song or Spotify URL and streams it into your voice channel (resumes or plays your Spotify track if omitted). |
| `/stream [query]` | Alias of `/play`. |
| `/pause` | Pauses current music playback. |
| `/resume` | Resumes paused music playback. |
| `/stop` | Stops audio playback and disconnects from voice. |
| `/nowplaying` | Displays an embed with current Spotify track, artist, album, and progress bar. |
| `/autosync <enabled>` | Toggles whether changing tracks on your Spotify app auto-streams into voice. |
| `/status` | Checks bot health, latency, Spotify connectivity, and voice inactivity state. |

## Setup & Deployment

1. **Install dependencies:**
   ```powershell
   pip install -r requirements.txt
   ```

2. **Configure environment variables in `.env`:**
   ```env
   DISCORD_TOKEN=your_discord_bot_token
   SPOTIFY_CLIENT_ID=your_spotify_client_id
   SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
   SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
   SPOTIFY_REFRESH_TOKEN=your_spotify_refresh_token
   ```

3. **Run the bot:**
   ```powershell
   python bot.py
   ```

## Free-Tier / Low-Memory Hosting

This bot is tuned to fit small plans such as **256 MB RAM / 512 MB disk**.

- `requirements.txt` pins `imageio-ffmpeg<0.8`. Version 0.8.x ships a ~165 MB FFmpeg
  binary; 0.7.x ships ~76 MB, saving ~89 MB of disk. If your host already provides
  FFmpeg (`which ffmpeg`), set `FFMPEG_PATH` and remove `imageio-ffmpeg` entirely.
- Set these **panel environment variables** (not `.env`, because pip runs first):
  ```env
  PIP_NO_CACHE_DIR=1
  PYTHONDONTWRITEBYTECODE=1
  PYTHONUNBUFFERED=1
  MALLOC_ARENA_MAX=2
  ```
- Delete `~/.cache/pip`, `~/.local`, `__pycache__`, `.venv`, and leftover archives
  from the deployment. On hosts that run `pip install` at boot, the pip cache can
  otherwise duplicate every downloaded wheel.
- The bot only requests the `guilds` and `voice_states` intents and disables the
  message cache (`max_messages=None`). Do not re-enable message intents unless you
  add message/prefix commands — they cost significant RAM.
- `yt-dlp` is imported lazily and only one extraction runs at a time. Resolved
  stream URLs are cached for a few hours, so repeating a track does not re-extract.
- Optional tuning:
  - `SPOTIFY_POLL_SECONDS` (default `10`) — lower is more responsive, higher saves CPU.
  - `MAX_VOICE_STREAMS` (default `2`) — cap on simultaneous voice channels, since each
    stream is a separate FFmpeg process.
