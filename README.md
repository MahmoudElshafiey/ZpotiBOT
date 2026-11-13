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
2. **Anonymous `visionos` client** (no cookies). This is the only client yt-dlp
   still gets direct audio URLs from for many tracks, including most music uploads.
3. **Cookie clients** (`web_safari`, then `tv`) — only when cookies are enabled.
4. **Anonymous `web_safari`, then yt-dlp's default clients** — when cookies are off.

Cookies follow `YTDLP_USE_COOKIES` (default `auto`): **enabled without a proxy,
disabled when `YTDLP_PROXY` is set**. This matters because a signed-in session makes
yt-dlp drop `visionos`, and the remaining clients are served by YouTube as
SABR-only streams that contain images but no audio (yt-dlp issue #12482).

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

YouTube is refusing the host's IP, not the song. Shared/datacenter IPs (Hetzner,
OVH, Contabo, most free tiers) are commonly flagged. **Cookies rarely fix this**, for
the reason above: signing in disables `visionos`, and the remaining clients only
return SABR/image streams. The durable fix is a proxy with a clean IP.

### 1. Proxy (the reliable fix)

Set `YTDLP_PROXY` and, if the proxy is not local, `FFMPEG_PROXY` too. Audio URLs are
bound to the IP that extracted them, so FFmpeg must use the same proxy that yt-dlp
used or YouTube answers `403`.

```env
YTDLP_PROXY=http://127.0.0.1:25344
FFMPEG_PROXY=http://127.0.0.1:25344
```

`FFMPEG_PROXY` defaults to `YTDLP_PROXY`, so a single variable is enough.

Requirements and limits:

- The proxy must expose an **HTTP** listener. FFmpeg cannot use SOCKS.
- FFmpeg **does not send proxy credentials**. Either run the proxy locally without
  auth (recommended), or put an auth-forwarding listener in front of it. `/ytcheck`
  flags this case as `credentials not supported`.
- Datacenter proxies are often blocked too; residential/mobile exits work best.
- Bandwidth: roughly 58 MB per hour at 130 kbps. Budget a few GB per month.

#### Free option: Cloudflare WARP in userspace

No root required, but it consumes ~25–40 MB RAM on a 256 MB host.

```bash
mkdir -p ~/warp && cd ~/warp
# 1. account config (Linux amd64 build; check the releases page for your arch)
curl -L -o wgcf https://github.com/ViRb3/wgcf/releases/latest/download/wgcf_2.2.28_linux_amd64
chmod +x wgcf && ./wgcf register --accept-tos && ./wgcf generate
# 2. userspace WireGuard exposing a local HTTP proxy
curl -L -o wp.tar.gz https://github.com/pufferffish/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz
tar xzf wp.tar.gz && chmod +x wireproxy
```

`wireproxy.conf` (copy `PrivateKey` and `Address` from `wgcf-profile.conf`):

```ini
[Interface]
PrivateKey = <YOUR_PRIVATE_KEY>
Address = 172.16.0.2/32
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
AllowedIPs = 0.0.0.0/0
Endpoint = engage.cloudflareclient.com:2408

[http]
BindAddress = 127.0.0.1:25344
```

Start it before the bot (or from a `start.sh` that launches `wireproxy`, waits for
the port, then `exec`s the bot), then verify:

```bash
curl -s --proxy http://127.0.0.1:25344 https://ipinfo.io/org
python -m yt_dlp --proxy http://127.0.0.1:25344 --no-warnings \
  --extractor-args "youtube:player_client=visionos" \
  -f bestaudio/best -g "https://www.youtube.com/watch?v=87ilJONnFiM"
```

A printed URL means it works. If it still reports a bot check, WARP's ranges are also
flagged — use a different proxy.

### 2. Cookies (only as a last resort)

Cookies are still supported, but with SABR in play they can make playback worse.
Check what the file actually contains before relying on it:

```bash
grep -o -E 'LOGIN_INFO|SAPISID|__Secure-1PAPISID|__Secure-3PAPISID' cookies.txt | sort -u
```

You need `LOGIN_INFO` plus one `SAPISID`-family name, exported from a **logged-in,
non-incognito** browser session using the **throwaway** account. `/ytcheck` reports
the parsed state (`signed in`, `logged out`, `expired`).

`YTDLP_USE_COOKIES` controls the policy:

| Value | Behaviour |
| :--- | :--- |
| `auto` (default) | Cookies used only when `YTDLP_PROXY` is **not** set |
| `always` | Cookies always used (accepts SABR-only results) |
| `never` | Cookies ignored even if present |

### 3. Force IPv4

`YTDLP_FORCE_IPV4=true` is the default and equivalent to `yt-dlp -4`. Set it to
`false` only if your host is IPv6-only.

### 4. Optional overrides

- `YTDLP_PLAYER_CLIENT` — comma-separated yt-dlp client override, e.g.
  `tv_samsung`, `visionos`, or `web_embedded`. Clients are scored differently and
  change every few weeks. It is tried first, then the `visionos`-first fallback chain
  described in [YouTube playback errors](#youtube-playback-errors) still runs, so a
  stale value cannot permanently break playback.
- `YTDLP_PROXY` / `FFMPEG_PROXY` — see above. `FFMPEG_PROXY` defaults to
  `YTDLP_PROXY`.
- `YTDLP_USE_COOKIES` — `auto` / `always` / `never`.

### Host-side test

Before restarting the bot, confirm the version — `2026.10.21` or newer is required:

```bash
python --version
python -m yt_dlp --version
```

Then test the mode the bot uses first (anonymous `visionos`), optionally through the
proxy:

```bash
python -m yt_dlp -4 --no-warnings --no-playlist -f bestaudio/best -g \
  --extractor-args "youtube:player_client=visionos" \
  "ytsearch1:bu dünya yıkılsa üzülmem TUANA"
```

If that prints a URL, the bot will play. If it only reports a bot check, the host IP
is flagged: add a proxy (above) rather than cookies. Cookies can still be tested:

```bash
python -m yt_dlp -4 --cookies cookies.txt --no-warnings --no-playlist -f bestaudio/best -g \
  --extractor-args "youtube:player_client=web_safari" \
  "ytsearch1:bu dünya yıkılsa üzülmem TUANA"
```

If this prints a URL but `/ytcheck` shows `format(s) but no playable audio`, the
client was served SABR-only content — your cookies "work" but cannot produce audio.
