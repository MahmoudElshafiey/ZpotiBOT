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
| `/ytcheck` | Admin-only: tests anonymous `visionos` and cookie extraction, and reports yt-dlp version/cookies/IPv4/player-client/proxy status. |

## Setup & Deployment

1. **Install dependencies** (Python 3.10+ required, 3.11+ recommended):
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

## YouTube playback errors

The bot resolves audio with `yt-dlp` in a fixed order:

1. **`YTDLP_PLAYER_CLIENT`**, if set — tried first, but never exclusive.
2. **Anonymous `visionos` client** (no cookies). This client is token-free, so it
   usually avoids the bot wall, and it exposes audio that the other clients hide.
3. **Cookie clients** (`web_safari`, then `tv`) — only when a cookies file is configured.
4. **Anonymous `web_safari`, then yt-dlp's default clients** — only when no cookies
   are configured.

`visionos` is tried **before** cookies on purpose: yt-dlp drops that client as soon as
the cookie file contains a signed-in session, and for many videos the authenticated
clients return storyboard images with no audio at all.

### `Requested format is not available`

If logs show:

```
ERROR: [youtube] x8plegt7Kf8: Requested format is not available.
```

YouTube returned formats for the video, but none of them was playable audio — usually
because the client only exposed storyboard images. Fixes, in order:

1. **Update yt-dlp.** `requirements.txt` requires `yt-dlp>=2026.10.21`, the first
   release with the `visionos` client that still supports Python 3.10. Newer releases
   (`2026.10.26+`) need Python 3.11+. Check with `python -m yt_dlp --version` and
   `python --version`, then reinstall:
   ```bash
   pip install -U -r requirements.txt
   ```
2. **Restart the bot and run `/ytcheck`.** It reports the yt-dlp version and tests
   the anonymous `visionos` mode and, when configured, the cookie mode.
3. If the cookie mode is the one returning no audio, that is expected for these
   tracks: the bot already tries anonymous `visionos` first, so leave
   `YTDLP_PLAYER_CLIENT` empty and cookies will only be used when `visionos` is blocked.
4. If a single track still fails in every mode, it has no usable audio source; play
   another search result instead.

`YTDLP_PLAYER_CLIENT` is tried first but does not replace the chain: if the pinned
client fails, the bot still falls back to anonymous `visionos` and then cookies.
Leave it empty unless you are debugging — a stale value such as `tv_samsung` is
exactly what used to cause this error.

## YouTube bot check

If logs show:

```
ERROR: [youtube] ...: Sign in to confirm you're not a bot.
```

YouTube is refusing the host's IP, not the song. Shared/datacenter IPs are commonly
flagged. The bot tries the token-free `visionos` client first, then applies cookies
when a cookies file is configured, and supports two optional overrides.

### 1. Export cookies (when the anonymous client is blocked)

Use a **throwaway Google account**, never your main account.

1. Open a private/incognito window and sign in to <https://www.youtube.com> with the
   throwaway account.
2. Export cookies in **Netscape** format with an extension such as
   *Get cookies.txt LOCALLY* (or `yt-dlp --cookies-from-browser` on a desktop).
3. Upload the file as `cookies.txt` next to `bot.py` on the host, or set
   `YTDLP_COOKIES_FILE` to its path. Alternatively, set `YTDLP_COOKIES_B64` to the
   base64-encoded file contents.
4. Restart the bot and run `/ytcheck`.
5. Close the incognito window when done.

Cookie files are credentials: they are listed in `.gitignore` and must never be
committed. Re-export them if playback breaks again after a few days/weeks.

### 2. Force IPv4

`YTDLP_FORCE_IPV4=true` is the default and equivalent to `yt-dlp -4`. Set it to
`false` only if your host is IPv6-only.

### 3. Optional overrides

- `YTDLP_PLAYER_CLIENT` — comma-separated yt-dlp client override, e.g.
  `tv_samsung`, `visionos`, or `web_embedded`. Clients are scored differently and
  change every few weeks. It is tried first, then the `visionos`-first fallback chain
  described in [YouTube playback errors](#youtube-playback-errors) still runs, so a
  stale value cannot permanently break playback.
- `YTDLP_PROXY` — proxy used only for yt-dlp extraction, e.g.
  `http://user:pass@host:port`. A residential/mobile proxy is the durable fix when
  the host IP itself is flagged and cookies alone stop working.

### Host-side test

Before restarting the bot, you can test the host Shell directly. First confirm the
version — `2026.10.21` or newer is required:

```bash
python --version
python -m yt_dlp --version
```

Then test the same mode the bot uses first (anonymous `visionos`):

```bash
python -m yt_dlp -4 --no-warnings --no-playlist -f bestaudio/best -g \
  --extractor-args "youtube:player_client=visionos" \
  "ytsearch1:bu dünya yıkılsa üzülmem TUANA"
```

If that prints a URL, the bot will play without cookies. If it only reports a bot
check, test the cookie fallback:

```bash
python -m yt_dlp -4 --cookies cookies.txt --no-warnings --no-playlist -f bestaudio/best -g \
  --extractor-args "youtube:player_client=web_safari" \
  "ytsearch1:bu dünya yıkılsa üzülmem TUANA"
```

If the cookie test fails too, the IP is flagged and `YTDLP_PROXY` or a different host
is required.
