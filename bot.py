import asyncio
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import spotipy
from spotipy.exceptions import SpotifyException

from spotify_auth import (
    DEFAULT_CACHE_PATH,
    get_cached_token,
    get_spotify_oauth,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ZpotiBot")

# Keep third-party logs quiet: less console I/O and less disk usage on hosts
# that capture stdout to a log file.
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

load_dotenv()

DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip().strip("'\"")
SPOTIFY_REFRESH_TOKEN = (os.getenv("SPOTIFY_REFRESH_TOKEN") or "").strip().strip("'\"")

# Directory containing bot.py. Used to find cookies.txt regardless of the
# process working directory chosen by the hosting panel.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env_bool(name: str, default: bool) -> bool:
    """Reads a boolean env var, accepting 1/true/yes/on and 0/false/no/off."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "y"}:
        return True
    if raw in {"0", "false", "no", "off", "n"}:
        return False
    logger.warning("Invalid boolean for %s (%r); using default %s.", name, raw, default)
    return default


def _env_number(name: str, default: float, cast=float):
    """Reads a numeric env var, falling back to a default for invalid values."""
    try:
        return cast(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("Invalid value for %s; using default %s.", name, default)
        return default


# Free-tier tuning knobs.
# 10s polling is enough for presence/auto-sync and halves API + CPU churn.
SPOTIFY_POLL_SECONDS = max(3.0, _env_number("SPOTIFY_POLL_SECONDS", 10.0))
# Each voice stream is a separate FFmpeg process (~30-60 MB). Keep this low on 256 MB RAM.
MAX_VOICE_STREAMS = max(1, int(_env_number("MAX_VOICE_STREAMS", 2, int)))
# Resolved YouTube URLs stay valid for hours, so cache them briefly.
STREAM_CACHE_TTL = 3 * 60 * 60
STREAM_CACHE_MAX = 32

# Bounded pool for blocking Spotify/yt-dlp work. The default executor can grow to
# min(32, cpu + 4) threads, which wastes memory through per-thread arenas/stacks.
IO_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zpoti-io")


def _resolve_cookie_file() -> Optional[str]:
    """Finds a Netscape-format cookies file for yt-dlp.

    Priority:
      1. ``YTDLP_COOKIES_FILE`` (absolute, or relative to bot.py)
      2. ``YTDLP_COOKIES_B64`` (base64 contents, written to a runtime file)
      3. ``cookies.txt`` next to bot.py

    Cookies let YouTube see a real signed-in session, which is the reliable way
    around the "Sign in to confirm you're not a bot" wall on datacenter IPs.
    """
    env_path = (os.getenv("YTDLP_COOKIES_FILE") or "").strip().strip("'\"")
    if env_path:
        path = env_path if os.path.isabs(env_path) else os.path.join(BASE_DIR, env_path)
        if os.path.isfile(path):
            return path
        logger.warning("YTDLP_COOKIES_FILE points to a missing file: %s", path)

    encoded = (os.getenv("YTDLP_COOKIES_B64") or "").strip()
    if encoded:
        try:
            import base64

            target = os.path.join(BASE_DIR, ".ytdlp_cookies.txt")
            with open(target, "wb") as fh:
                fh.write(base64.b64decode(encoded))
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return target
        except Exception as exc:
            logger.warning("Could not decode YTDLP_COOKIES_B64: %s", exc)

    auto_path = os.path.join(BASE_DIR, "cookies.txt")
    if os.path.isfile(auto_path):
        return auto_path
    return None


YTDLP_COOKIES_FILE = _resolve_cookie_file()
# yt-dlp -4 equivalent. Datacenter IPv6 ranges are scored more harshly by YouTube.
YTDLP_FORCE_IPV4 = _env_bool("YTDLP_FORCE_IPV4", True)
# Optional comma-separated override, e.g. "tv_samsung" or "visionos,web_embedded".
YTDLP_PLAYER_CLIENT = (os.getenv("YTDLP_PLAYER_CLIENT") or "").strip().strip("'\"")
# Optional proxy for extraction only (does not affect Discord/Spotify traffic).
YTDLP_PROXY = (os.getenv("YTDLP_PROXY") or "").strip().strip("'\"")

if YTDLP_COOKIES_FILE:
    logger.info("Using yt-dlp cookies file: %s", YTDLP_COOKIES_FILE)
else:
    logger.info("No yt-dlp cookies file configured; YouTube requests will be anonymous.")

BOT_WALL_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "confirm you’re not a bot",
)

FORMAT_ERROR_MARKERS = (
    "requested format is not available",
    "no video formats found",
    "no playable audio",
    "only images are available",
)


def is_bot_wall_error(message: str) -> bool:
    """Detects YouTube's datacenter-IP bot wall in a yt-dlp error string."""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in BOT_WALL_MARKERS)


def is_format_error(message: str) -> bool:
    """Detects a missing/unusable audio format in a yt-dlp error string."""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in FORMAT_ERROR_MARKERS)


def describe_ytdlp_failure(message: str) -> str:
    """Turns the raw yt-dlp error into an actionable explanation when possible."""
    if is_bot_wall_error(message):
        return (
            "YouTube is blocking this host with a bot check. The bot already tried the "
            "token-free visionos client. Upload a Netscape-format cookies.txt from a "
            "throwaway Google account (or set YTDLP_COOKIES_FILE / YTDLP_COOKIES_B64) and "
            "restart the bot. If it still fails, set YTDLP_PROXY or try YTDLP_PLAYER_CLIENT. "
            "Run /ytcheck for details."
        )
    if is_format_error(message):
        return (
            "YouTube returned no playable audio format for this video. The bot tried the "
            "token-free visionos client and, when configured, cookie-based clients. Update "
            "yt-dlp to >=2026.10.21, then run /ytcheck. If it still fails, try another search result."
        )
    return message


def _parse_clients(value: str) -> tuple[str, ...]:
    """Parses a comma-separated player-client list."""
    return tuple(client.strip() for client in (value or "").split(",") if client.strip())


def get_ytdlp_version() -> str:
    """Returns the installed yt-dlp version without importing it at startup."""
    try:
        import yt_dlp

        return getattr(yt_dlp.version, "__version__", "unknown")
    except Exception:
        return "not installed"


def extraction_attempts() -> list[tuple[Optional[tuple[str, ...]], bool]]:
    """Returns (player_clients, use_cookies) attempts in priority order.

    The primary mode is anonymous ``visionos`` (after any explicit override):
      * it is token-free, so it avoids the bot wall that affects web/tv clients;
      * it exposes audio that the authenticated clients often hide (some videos
        only return storyboard images through web/web_embedded/tv_samsung);
      * yt-dlp skips ``visionos`` entirely when cookies make the session
        authenticated, so cookies must be a separate fallback attempt.

    An explicit ``YTDLP_PLAYER_CLIENT`` is tried first but is *not* exclusive: a
    stale override (for example ``tv_samsung``) must not be able to disable the
    chain that makes playback work.
    """
    configured = _parse_clients(YTDLP_PLAYER_CLIENT)
    attempts: list[tuple[Optional[tuple[str, ...]], bool]] = []

    if configured:
        # Cookies are skipped for visionos because yt-dlp would discard that
        # client when the cookie file makes the session authenticated.
        attempts.append((configured, bool(YTDLP_COOKIES_FILE) and "visionos" not in configured))

    # Primary mode: anonymous (no cookies), token-free client.
    attempts.append((("visionos",), False))

    # Cookie fallback: clients that support cookies and may bypass the bot wall.
    if YTDLP_COOKIES_FILE:
        attempts.append((("web_safari",), True))
        attempts.append((("tv",), True))
    else:
        # Last-resort anonymous attempts when no cookie file is configured.
        attempts.append((("web_safari",), False))
        attempts.append((None, False))

    seen: set[tuple[Optional[tuple[str, ...]], bool]] = set()
    unique: list[tuple[Optional[tuple[str, ...]], bool]] = []
    for clients, use_cookies in attempts:
        key = (clients, use_cookies)
        if key not in seen:
            seen.add(key)
            unique.append((clients, use_cookies))
    return unique


def build_ydl_opts(
    player_clients: Optional[tuple[str, ...]] = None,
    use_cookies: bool = True,
) -> Dict[str, Any]:
    """Builds yt-dlp options, including client/cookie/IPv4/proxy overrides."""
    opts: Dict[str, Any] = {
        # "all" lets us pick the best audio format ourselves. "bestaudio/best"
        # raises "Requested format is not available" when a client returns only
        # storyboard images.
        "format": "all",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "color": {"stdout": "no_color", "stderr": "no_color"},
        "cachedir": False,
        "default_search": "ytsearch",
        "extract_flat": False,
        # Fail fast: a blocked host should not sit in retries for a minute.
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 20,
    }
    if use_cookies and YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    if YTDLP_FORCE_IPV4:
        opts["source_address"] = "0.0.0.0"
    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(player_clients)}}
    elif YTDLP_PLAYER_CLIENT:
        clients = _parse_clients(YTDLP_PLAYER_CLIENT)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    return opts


def extract_with_ytdlp(
    search_target: str,
    player_clients: Optional[tuple[str, ...]] = None,
    use_cookies: bool = True,
) -> Optional[Dict[str, Any]]:
    """Blocking yt-dlp extraction, run on IO_EXECUTOR."""
    import yt_dlp  # deferred import keeps idle memory lower

    try:
        with yt_dlp.YoutubeDL(
            build_ydl_opts(player_clients=player_clients, use_cookies=use_cookies)
        ) as ydl:
            return ydl.extract_info(search_target, download=False)
    finally:
        trim_process_memory()


def audio_format_candidates(entry: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Returns formats that contain audio and a playable URL."""
    return [
        fmt
        for fmt in (entry.get("formats") or [])
        if fmt.get("url")
        and not fmt.get("has_drm")
        and fmt.get("acodec") not in (None, "none")
    ]


def pick_audio_format(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Picks the best playable audio format from an extracted entry.

    Prefers audio-only progressive HTTPS formats, then any format that has audio.
    """
    formats = entry.get("formats") or []
    if not formats:
        return entry

    candidates = audio_format_candidates(entry)
    if not candidates:
        return None

    audio_only = [fmt for fmt in candidates if fmt.get("vcodec") in (None, "none")]
    pool = audio_only or candidates

    def score(fmt: Dict[str, Any]) -> tuple[int, float, float]:
        protocol = str(fmt.get("protocol") or "").lower()
        progressive = 1 if protocol.startswith("http") else 0
        return (progressive, float(fmt.get("abr") or 0), float(fmt.get("tbr") or 0))

    return max(pool, key=score)


def pick_audio_url(entry: Dict[str, Any]) -> Optional[str]:
    """Picks the best audio URL from an extracted entry."""
    fmt = pick_audio_format(entry)
    return (fmt or {}).get("url")


def get_ffmpeg_binary() -> str:
    """Finds or resolves the working FFmpeg executable path.
    Prioritizes:
    1. Custom FFMPEG_PATH environment variable
    2. System PATH (shutil.which)
    3. imageio-ffmpeg bundled platform executable (guaranteed on Windows & Linux)
    """
    env_path = os.getenv("FFMPEG_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception as exc:
        logger.debug("imageio-ffmpeg not available: %s", exc)

    return "ffmpeg"


FFMPEG_EXECUTABLE = get_ffmpeg_binary()
logger.info("Using FFmpeg binary: %s", FFMPEG_EXECUTABLE)


def get_spotify_client(cache_path: str = DEFAULT_CACHE_PATH) -> Optional[spotipy.Spotify]:
    """Instantiates a Spotify client using SPOTIFY_REFRESH_TOKEN from env or local cached tokens."""
    refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN")
    if refresh_token and refresh_token != "your_spotify_refresh_token_here":
        try:
            sp_oauth = get_spotify_oauth(cache_path=cache_path)
            token_info = sp_oauth.refresh_access_token(refresh_token)
            if token_info and "access_token" in token_info:
                logger.info("Authenticated Spotify using SPOTIFY_REFRESH_TOKEN environment variable.")
                return spotipy.Spotify(auth_manager=sp_oauth)
        except Exception as exc:
            logger.error("Failed to authenticate with SPOTIFY_REFRESH_TOKEN from environment: %s", exc)

    cached_data = get_cached_token(cache_path)
    if not cached_data or "refresh_token" not in cached_data:
        logger.warning(
            "No active Spotify credentials found. Please set SPOTIFY_REFRESH_TOKEN in your .env / host settings, "
            "or run 'python spotify_auth.py' locally to generate .spotify_cache."
        )
        return None

    try:
        sp_oauth = get_spotify_oauth(cache_path=cache_path)
        token_info = sp_oauth.validate_token(cached_data)
        if not token_info and "refresh_token" in cached_data:
            token_info = sp_oauth.refresh_access_token(cached_data["refresh_token"])
            logger.info("Successfully refreshed Spotify access token using cached refresh_token.")
        return spotipy.Spotify(auth_manager=sp_oauth)
    except Exception as exc:
        logger.error("Failed to refresh access token with cached refresh_token: %s", exc)
        return None


def format_duration(milliseconds: int) -> str:
    """Formats milliseconds into mm:ss display string."""
    seconds = max(0, milliseconds // 1000)
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    return f"{minutes:02d}:{remaining_seconds:02d}"


def create_progress_bar(progress_ms: int, duration_ms: int, bar_length: int = 14) -> str:
    """Generates an ASCII/Unicode progress bar."""
    if duration_ms <= 0:
        return f"[{'▬' * bar_length}]"
    ratio = max(0.0, min(1.0, progress_ms / duration_ms))
    indicator_pos = min(bar_length - 1, int(ratio * bar_length))
    bar = ["▬"] * bar_length
    bar[indicator_pos] = "🔘"
    return "".join(bar)


def search_spotify_track(query: str, sp: Optional[spotipy.Spotify]) -> Optional[Dict[str, Any]]:
    """Searches Spotify for a song by title, artist, or Spotify track URL/URI."""
    if not sp:
        return None

    query = query.strip()
    try:
        # If user passed a direct Spotify URL or URI
        if "open.spotify.com/track/" in query:
            track_id = query.split("track/")[1].split("?")[0]
            return sp.track(track_id)
        elif query.startswith("spotify:track:"):
            track_id = query.split("spotify:track:")[1]
            return sp.track(track_id)

        # Otherwise search Spotify by text
        results = sp.search(q=query, limit=1, type="track")
        tracks = results.get("tracks", {}).get("items", [])
        if tracks:
            return tracks[0]
    except Exception as exc:
        logger.warning("Spotify track search failed for '%s': %s", query, exc)

    return None


# Resolved stream URLs are valid for hours; reuse them for repeated tracks.
_stream_cache: Dict[str, Any] = {}
_ydl_lock: Optional[asyncio.Lock] = None
# Last yt-dlp failure, surfaced by /play and /ytcheck.
_last_ytdlp_error: Optional[str] = None
# Failure category for the last failed extraction: "bot_wall", "format", or "other".
_last_ytdlp_failure_kind: Optional[str] = None


def _get_ydl_lock() -> asyncio.Lock:
    """Returns the process-wide extraction lock, creating it on first use."""
    global _ydl_lock
    if _ydl_lock is None:
        _ydl_lock = asyncio.Lock()
    return _ydl_lock


def trim_process_memory() -> None:
    """Returns freed glibc arenas to the OS after heavy yt-dlp allocations (Linux only)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _cached_stream(cache_key: str) -> Optional[Dict[str, Any]]:
    entry = _stream_cache.get(cache_key)
    if not entry:
        return None
    timestamp, info = entry
    if time.monotonic() - timestamp > STREAM_CACHE_TTL:
        _stream_cache.pop(cache_key, None)
        return None
    return info


def _store_stream(cache_key: str, info: Dict[str, Any]) -> None:
    now = time.monotonic()
    for cached_key, (timestamp, _) in list(_stream_cache.items()):
        if now - timestamp > STREAM_CACHE_TTL:
            _stream_cache.pop(cached_key, None)
    if len(_stream_cache) >= STREAM_CACHE_MAX:
        oldest_key = min(_stream_cache, key=lambda key: _stream_cache[key][0])
        _stream_cache.pop(oldest_key, None)
    _stream_cache[cache_key] = (now, info)


async def fetch_youtube_stream(track: dict) -> Optional[Dict[str, Any]]:
    """Resolves a YouTube audio URL for a Spotify track.

    Extraction is serialized, cached for a few hours, and runs on a small bounded
    thread pool. yt-dlp is imported lazily so an idle bot does not pay its memory cost.
    """
    global _last_ytdlp_error, _last_ytdlp_failure_kind

    track_name = track.get("name", "Unknown Track")
    artists_list = [artist.get("name") for artist in track.get("artists", []) if artist.get("name")]
    artists_str = ", ".join(artists_list) or "Unknown Artist"
    search_query = f"{track_name} {artists_str}".strip()
    cache_key = str(track.get("id") or track.get("uri") or f"{track_name}|{artists_str}")

    cached = _cached_stream(cache_key)
    if cached:
        logger.info("Reusing cached YouTube stream for '%s' by '%s'.", track_name, artists_str)
        return cached

    # Only one yt-dlp extraction at a time: avoids doubling peak RAM when /play
    # and the auto-sync poller resolve tracks concurrently.
    async with _get_ydl_lock():
        cached = _cached_stream(cache_key)
        if cached:
            return cached

        logger.info("Extracting YouTube audio stream for Spotify track: '%s' by '%s'...", track_name, artists_str)
        search_target = f"ytsearch1:{search_query} audio"

        loop = asyncio.get_running_loop()
        attempts = extraction_attempts()
        failure_kind: Optional[str] = None
        failure_message: Optional[str] = None

        for index, (clients, use_cookies) in enumerate(attempts, start=1):
            label = "clients={}, cookies={}".format(
                ",".join(clients) if clients else "default", "yes" if use_cookies else "no"
            )
            try:
                info = await loop.run_in_executor(
                    IO_EXECUTOR, extract_with_ytdlp, search_target, clients, use_cookies
                )
            except Exception as exc:
                message = str(exc)
                failure_kind = (
                    "bot_wall" if is_bot_wall_error(message)
                    else "format" if is_format_error(message)
                    else "other"
                )
                failure_message = message
                logger.warning(
                    "yt-dlp attempt %d/%d failed for '%s' (%s): %s",
                    index, len(attempts), search_query, label, message,
                )
                continue

            entry = info["entries"][0] if info and info.get("entries") else None
            if not entry:
                failure_kind = "format"
                failure_message = "yt-dlp returned no search results."
                logger.warning(
                    "yt-dlp attempt %d/%d returned no results for '%s' (%s).",
                    index, len(attempts), search_query, label,
                )
                continue

            audio_url = pick_audio_url(entry)
            if not audio_url:
                failure_kind = "format"
                failure_message = "yt-dlp returned no playable audio format."
                logger.warning(
                    "yt-dlp attempt %d/%d found %d format(s) but no audio for '%s' (%s).",
                    index, len(attempts), len(entry.get("formats") or []), search_query, label,
                )
                continue

            logger.info(
                "yt-dlp attempt %d/%d resolved audio for '%s' (%s).",
                index, len(attempts), search_query, label,
            )
            _last_ytdlp_error = None
            _last_ytdlp_failure_kind = None

            result = {
                "audio_url": audio_url,
                "title": entry.get("title", track_name),
                "webpage_url": entry.get("webpage_url", ""),
                "thumbnail": entry.get("thumbnail"),
                "track_name": track_name,
                "artists_str": artists_str,
            }
            _store_stream(cache_key, result)
            return result

        _last_ytdlp_error = failure_message or "yt-dlp returned no playable audio format."
        _last_ytdlp_failure_kind = failure_kind or "other"
        logger.error(
            "yt-dlp extraction failed for '%s' after %d attempt(s): %s",
            search_query, len(attempts), _last_ytdlp_error,
        )
        return None


async def stream_to_voice_client(voice_client: discord.VoiceClient, stream_info: Dict[str, Any]) -> bool:
    """Streams audio to an active discord.VoiceClient using resolved FFmpeg."""
    ffmpeg_exe = get_ffmpeg_binary()
    # -threads 1 caps decoder threads/buffers; -loglevel error silences FFmpeg and
    # overrides discord.py's default warning level.
    ffmpeg_options = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn -threads 1 -loglevel error",
    }
    audio_source: Optional[discord.FFmpegPCMAudio] = None
    try:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        def after_playback(error):
            if error:
                logger.error("Error during voice playback: %s", error)

        audio_source = discord.FFmpegPCMAudio(
            stream_info["audio_url"],
            executable=ffmpeg_exe,
            **ffmpeg_options,
        )
        voice_client.play(audio_source, after=after_playback)
        return True
    except Exception as exc:
        if audio_source is not None:
            audio_source.cleanup()
        logger.error("Failed to stream to voice client with FFmpeg (%s): %s", ffmpeg_exe, exc)
        return False


async def play_audio(interaction: discord.Interaction, track: dict) -> bool:
    """Connects to the user's voice channel and streams the Spotify track's audio."""
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("⚠️ Voice streaming commands must be executed inside a server.")
        return False

    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        await interaction.followup.send("⚠️ You must be in a voice channel so the bot can stream audio to you.")
        return False

    voice_channel = user.voice.channel
    track_name = track.get("name", "Unknown Track")
    artists_list = [artist.get("name") for artist in track.get("artists", []) if artist.get("name")]
    artists_str = ", ".join(artists_list) or "Unknown Artist"
    album_name = track.get("album", {}).get("name", "Unknown Album")
    duration_ms = track.get("duration_ms", 0)
    spotify_url = track.get("external_urls", {}).get("spotify", "")
    images = track.get("album", {}).get("images", [])
    album_art = images[0]["url"] if images else None

    # Connect to or move into user's voice channel
    voice_client: Optional[discord.VoiceClient] = guild.voice_client
    try:
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)
    except discord.Forbidden:
        await interaction.followup.send(f"⚠️ Bot lacks permission to connect to `{voice_channel.name}`.")
        return False
    except Exception as exc:
        logger.error("Failed to connect to voice channel %s: %s", voice_channel.name, exc)
        await interaction.followup.send(f"⚠️ Could not connect to voice channel: {exc}")
        return False

    # Stop the old FFmpeg process before resolving the next track. This avoids
    # holding an old decoder + a new yt-dlp extraction in memory at the same time.
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    # Extract stream URL
    stream_info = await fetch_youtube_stream(track)
    if not stream_info:
        if _last_ytdlp_failure_kind == "bot_wall":
            await interaction.followup.send(
                f"🚫 YouTube blocked this host with a bot check while resolving **{track_name}**.\n"
                "Upload a Netscape-format `cookies.txt` from a throwaway Google account "
                "(or set `YTDLP_COOKIES_FILE`), then restart the bot and run `/ytcheck`.\n"
                "See the README's *YouTube bot check* section."
            )
        elif _last_ytdlp_failure_kind == "format":
            await interaction.followup.send(
                f"🎧 YouTube returned no playable audio format for **{track_name}**.\n"
                "The bot tried the token-free `visionos` client first and, when configured, "
                "cookie-based clients.\n"
                "Fix: update `yt-dlp` to `>=2026.10.21`, then restart and run `/ytcheck`. "
                "If it still fails, try a different search result."
            )
        else:
            detail = describe_ytdlp_failure(_last_ytdlp_error or "").strip()
            await interaction.followup.send(
                f"🔍 Could not find an audio stream matching Spotify track **{track_name}**."
                + (f"\n> {detail[:300]}" if detail else "")
            )
        return False

    # Stream to voice
    success = await stream_to_voice_client(voice_client, stream_info)
    if not success:
        await interaction.followup.send(f"⚠️ Failed to initiate audio streaming for **{track_name}**.")
        return False

    # Reset any alone timer if present
    bot.voice_alone_since.pop(guild.id, None)

    embed = discord.Embed(
        title="🎶 Now Playing in Voice",
        description=f"**[{track_name}]({spotify_url or stream_info['webpage_url']})**\nby **{artists_str}**\n\n*Album:* {album_name}\n*Duration:* `{format_duration(duration_ms)}`",
        color=discord.Color.green(),
    )
    embed.add_field(name="Voice Channel", value=f"`{voice_channel.name}`", inline=True)
    if album_art:
        embed.set_thumbnail(url=album_art)
    elif stream_info.get("thumbnail"):
        embed.set_thumbnail(url=stream_info["thumbnail"])

    embed.set_footer(text="Spotify Track Search • Auto-disconnects after 10m alone")

    await interaction.followup.send(embed=embed)
    return True


class SpotifyDiscordBot(commands.Bot):
    """Discord Bot class managing application slash commands, Spotify client,
    periodic Spotify status sync, and 10-minute alone voice auto-disconnect.

    Only the guilds and voice-states intents are requested, and the message cache
    is disabled: this bot has no message/prefix commands, so caching messages only
    wastes RAM on small hosting plans.
    """

    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
        )

        self.sp: Optional[spotipy.Spotify] = None
        self.last_track_id: Optional[str] = None
        self.last_is_playing: bool = False
        self.auto_sync_voice: bool = True  # Auto stream new tracks to connected voice channels
        self.voice_alone_since: Dict[int, float] = {}  # guild_id -> timestamp when left alone

    def ensure_spotify(self) -> Optional[spotipy.Spotify]:
        """Ensures the Spotify client is initialized, refreshing from cache if necessary."""
        if self.sp is None:
            self.sp = get_spotify_client()
        return self.sp

    async def setup_hook(self):
        # Initialize Spotify client using cached credentials
        self.sp = get_spotify_client()
        if self.sp:
            logger.info("Spotify client initialized successfully with cached credentials.")
        else:
            logger.warning("Bot started without an active Spotify session.")

        # Sync slash commands with Discord
        logger.info("Syncing slash commands...")
        synced = await self.tree.sync()
        logger.info("Synced %d application (slash) command(s).", len(synced))

        # Start background polling tasks
        self.spotify_poller.start()
        self.inactivity_poller.start()

    async def close(self):
        # Gracefully cancel polling tasks on shutdown
        if self.spotify_poller.is_running():
            self.spotify_poller.cancel()
        if self.inactivity_poller.is_running():
            self.inactivity_poller.cancel()
        IO_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        await super().close()

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        print(f"Bot is ready! Logged in as {self.user} (ID: {self.user.id})")

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Monitors voice channels and starts/stops the 10-minute alone countdown."""
        now = asyncio.get_event_loop().time()
        for vc in list(self.voice_clients):
            if not vc.is_connected() or not vc.channel:
                continue
            non_bot_members = [m for m in vc.channel.members if not m.bot]
            if len(non_bot_members) == 0:
                if vc.guild.id not in self.voice_alone_since:
                    self.voice_alone_since[vc.guild.id] = now
                    logger.info("Bot left alone in voice channel '%s' (%s). 10-minute countdown started.", vc.channel.name, vc.guild.name)
            else:
                if vc.guild.id in self.voice_alone_since:
                    self.voice_alone_since.pop(vc.guild.id, None)
                    logger.info("Member joined '%s'. Inactivity countdown cancelled.", vc.channel.name)

    @tasks.loop(seconds=10.0)
    async def inactivity_poller(self):
        """Checks every 10 seconds if the bot has been left alone in a voice channel for > 10 minutes."""
        now = asyncio.get_event_loop().time()
        for vc in list(self.voice_clients):
            if not vc.is_connected() or not vc.channel:
                continue
            non_bot_members = [m for m in vc.channel.members if not m.bot]
            if len(non_bot_members) == 0:
                if vc.guild.id not in self.voice_alone_since:
                    self.voice_alone_since[vc.guild.id] = now
                elif now - self.voice_alone_since[vc.guild.id] >= 600:  # 10 minutes (600 seconds)
                    logger.info("Disconnecting from voice channel '%s' after 10 minutes of inactivity.", vc.channel.name)
                    try:
                        if vc.is_playing() or vc.is_paused():
                            vc.stop()
                        await vc.disconnect()
                    except Exception as exc:
                        logger.error("Error disconnecting inactive voice client: %s", exc)
                    self.voice_alone_since.pop(vc.guild.id, None)
            else:
                self.voice_alone_since.pop(vc.guild.id, None)

    @inactivity_poller.before_loop
    async def before_inactivity_poller(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=SPOTIFY_POLL_SECONDS)
    async def spotify_poller(self):
        """Polls Spotify's /me/player/currently-playing endpoint periodically.
        Reflects changes in the bot's presence and synchronizes voice streaming if active.
        """
        sp = self.ensure_spotify()
        if not sp:
            return

        loop = asyncio.get_running_loop()
        try:
            playback = await loop.run_in_executor(IO_EXECUTOR, sp.currently_playing)
        except SpotifyException as exc:
            logger.warning("Spotify polling API error: %s", exc)
            return
        except Exception as exc:
            logger.debug("Transient error polling Spotify playback: %s", exc)
            return

        if not playback or not playback.get("item"):
            if self.last_is_playing:
                self.last_is_playing = False
                self.last_track_id = None
                await self.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name="Spotify (Paused / Idle)",
                    )
                )
            return

        item = playback.get("item", {})
        track_id = item.get("id") or item.get("uri")
        is_playing = playback.get("is_playing", False)
        track_name = item.get("name", "Unknown Track")
        artists = ", ".join(artist["name"] for artist in item.get("artists", [])) or "Unknown Artist"

        if track_id != self.last_track_id:
            logger.info("Track change detected on Spotify: '%s' by '%s' [ID: %s]", track_name, artists, track_id)
            self.last_track_id = track_id
            self.last_is_playing = is_playing

            # Update Discord bot presence
            presence_text = f"{track_name} • {artists}"[:128]
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening if is_playing else discord.ActivityType.watching,
                    name=presence_text,
                )
            )

            # Auto-sync to connected voice channels if enabled. Cap the number of
            # simultaneous FFmpeg streams so a multi-guild bot cannot exhaust 256 MB.
            if self.auto_sync_voice and is_playing and self.voice_clients:
                connected = [vc for vc in self.voice_clients if vc.is_connected()]
                if len(connected) > MAX_VOICE_STREAMS:
                    logger.warning(
                        "Auto-sync limited to %d of %d connected voice channel(s) (MAX_VOICE_STREAMS).",
                        MAX_VOICE_STREAMS,
                        len(connected),
                    )
                targets = connected[:MAX_VOICE_STREAMS]
                if targets:
                    logger.info("Auto-syncing new Spotify track to %d voice channel(s)...", len(targets))
                    # Free the old FFmpeg processes before yt-dlp allocates for the new track.
                    for vc in targets:
                        if vc.is_playing() or vc.is_paused():
                            vc.stop()
                    stream_info = await fetch_youtube_stream(item)
                    if stream_info:
                        for vc in targets:
                            await stream_to_voice_client(vc, stream_info)

        elif is_playing != self.last_is_playing:
            self.last_is_playing = is_playing
            state_label = "▶️ " if is_playing else "⏸️ "
            presence_text = f"{state_label}{track_name} • {artists}"[:128]
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name=presence_text,
                )
            )

    @spotify_poller.before_loop
    async def before_spotify_poller(self):
        await self.wait_until_ready()


bot = SpotifyDiscordBot()


@bot.tree.command(name="status", description="Check if the bot is alive, Spotify status, and voice timers")
async def status(interaction: discord.Interaction):
    """Slash command that displays bot status, Spotify connection, and voice inactivity state."""
    spotify_status = "Connected 🟢" if bot.ensure_spotify() is not None else "Not authenticated 🟡"
    latency_ms = round(bot.latency * 1000, 1)
    sync_status = "Enabled 🟢" if bot.auto_sync_voice else "Disabled 🔴"

    guild = interaction.guild
    voice_info = "Not connected"
    if guild and guild.voice_client:
        vc: discord.VoiceClient = guild.voice_client
        non_bot_count = len([m for m in vc.channel.members if not m.bot])
        alone_state = ""
        if guild.id in bot.voice_alone_since:
            elapsed = int(asyncio.get_event_loop().time() - bot.voice_alone_since[guild.id])
            remaining = max(0, 600 - elapsed)
            alone_state = f" (Alone for {elapsed}s, disconnect in {remaining}s)"
        voice_info = f"Connected to `{vc.channel.name}` with {non_bot_count} listener(s){alone_state}"

    message = (
        f"🤖 **Bot is alive!**\n"
        f"• **Latency:** {latency_ms}ms\n"
        f"• **Spotify:** {spotify_status}\n"
        f"• **Voice Channel:** {voice_info}\n"
        f"• **Voice Auto-Sync:** {sync_status}\n"
        f"• **Active Spotify Track:** {bot.last_track_id or 'None / Idle'}"
    )
    await interaction.response.send_message(message)


@bot.tree.command(name="ytcheck", description="Test YouTube audio extraction (visionos + cookies) and show yt-dlp settings (admin only)")
@app_commands.default_permissions(administrator=True)
async def ytcheck(interaction: discord.Interaction):
    """Tests the extraction chain (anonymous visionos, then cookies) for admins."""
    permissions = getattr(interaction, "permissions", None)
    if interaction.guild is None or not getattr(permissions, "administrator", False):
        await interaction.response.send_message(
            "⚠️ Only server administrators can run this check.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    config = (
        f"• **yt-dlp:** `{get_ytdlp_version()}`\n"
        f"• **Cookies:** {'yes (`' + os.path.basename(YTDLP_COOKIES_FILE) + '`)' if YTDLP_COOKIES_FILE else 'no'}\n"
        f"• **Force IPv4:** {'yes' if YTDLP_FORCE_IPV4 else 'no'}\n"
        f"• **Player client:** {YTDLP_PLAYER_CLIENT or 'auto (visionos first, then cookie clients)'}\n"
        f"• **Proxy:** {'configured' if YTDLP_PROXY else 'none'}"
    )

    loop = asyncio.get_running_loop()
    test_target = "ytsearch1:Never Gonna Give You Up Rick Astley audio"
    # Test at most two modes: the primary (anonymous visionos) and the cookie
    # fallback. Running every attempt would make this command very slow on a
    # host that YouTube blocks.
    probes = extraction_attempts()[:2]
    lines: list[str] = []
    any_audio = False
    saw_bot_wall = False
    saw_no_audio = False

    for clients, use_cookies in probes:
        label = ", ".join(clients) if clients else "yt-dlp default"
        mode = "cookies" if use_cookies else "anonymous"
        started = time.perf_counter()
        try:
            async with _get_ydl_lock():
                info = await loop.run_in_executor(
                    IO_EXECUTOR, extract_with_ytdlp, test_target, clients, use_cookies
                )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            message = str(exc)
            logger.error("ytcheck extraction failed (%s, %s): %s", label, mode, message)
            if is_bot_wall_error(message):
                saw_bot_wall = True
                reason = "bot check"
            elif is_format_error(message):
                saw_no_audio = True
                reason = "no audio format"
            else:
                reason = "error"
            lines.append(
                f"❌ **{label}** ({mode}) — `{elapsed:.1f}s`\n"
                f"> {reason}: {message[:200]}"
            )
            continue

        elapsed = time.perf_counter() - started
        entry = info["entries"][0] if info and info.get("entries") else None
        formats = (entry or {}).get("formats") or []
        chosen = pick_audio_format(entry) if entry else None
        if chosen and chosen.get("url"):
            any_audio = True
            abr = chosen.get("abr") or chosen.get("tbr") or 0
            lines.append(
                f"✅ **{label}** ({mode}) — `{elapsed:.1f}s`\n"
                f"> {(entry.get('title') or 'unknown')[:80]} — format `{chosen.get('format_id')}` "
                f"{chosen.get('ext') or ''} ~{float(abr):.0f}k, {len(formats)} format(s)"
            )
        else:
            saw_no_audio = True
            lines.append(
                f"⚠️ **{label}** ({mode}) — `{elapsed:.1f}s`\n"
                f"> {len(formats)} format(s) but no playable audio"
            )

    if any_audio:
        verdict = "✅ **Playback should work.**"
    elif saw_bot_wall:
        verdict = (
            "❌ **YouTube is blocking this host with a bot check.** Add a throwaway-account "
            "`cookies.txt` (or `YTDLP_PROXY`); see the README's *YouTube bot check* section."
        )
    elif saw_no_audio:
        verdict = (
            "❌ **YouTube returned no playable audio.** Update `yt-dlp` to `>=2026.10.21`, "
            "restart the bot, and run `/ytcheck` again."
        )
    else:
        verdict = "❌ **Extraction failed.** Check the logs above and the bot's network access to YouTube."

    await interaction.followup.send(
        f"{verdict}\n\n"
        f"**Extraction tests**\n" + "\n".join(lines) + "\n\n"
        f"**Current settings**\n{config}",
        ephemeral=True,
    )


@bot.tree.command(name="play", description="Search Spotify for a song and play it in your voice channel")
@app_commands.describe(query="Song title, artist, or Spotify track URL/URI (resumes or plays your Spotify track if omitted)")
async def play(interaction: discord.Interaction, query: Optional[str] = None):
    """Searches Spotify for the track, connects to voice, and plays the audio."""
    await interaction.response.defer()

    # Verify voice connection
    user = interaction.user
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        await interaction.followup.send("⚠️ You must be connected to a voice channel to play music!")
        return

    sp = bot.ensure_spotify()
    track_obj: Optional[dict] = None

    if query:
        query = query.strip()
        # Search Spotify directly for the track
        track_obj = search_spotify_track(query, sp)
        if not track_obj:
            # Fallback pseudo-track if Spotify search returned nothing
            track_obj = {"name": query, "artists": []}
    else:
        # If no query provided:
        guild = interaction.guild
        # 1. If voice playback was paused, resume it
        if guild and guild.voice_client and guild.voice_client.is_paused():
            guild.voice_client.resume()
            await interaction.followup.send("▶️ Resumed voice channel playback.")
            return

        # 2. Check if user is currently playing something on their Spotify app
        if sp:
            try:
                current = sp.current_playback()
                if current and current.get("item"):
                    track_obj = current["item"]
            except Exception as exc:
                logger.warning("Error getting current Spotify playback: %s", exc)

        if not track_obj:
            await interaction.followup.send(
                "⚠️ Nothing is currently playing on Spotify. Please provide a song name or Spotify link to play!"
            )
            return

    # Stream the Spotify track into the user's voice channel
    await play_audio(interaction, track_obj)


@bot.tree.command(name="stream", description="Search Spotify for a song and stream it into voice (alias of /play)")
@app_commands.describe(query="Song title, artist, or Spotify track URL/URI (optional)")
async def stream(interaction: discord.Interaction, query: Optional[str] = None):
    """Alias for /play."""
    await play(interaction, query)


@bot.tree.command(name="pause", description="Pause current music playback")
async def pause(interaction: discord.Interaction):
    """Pauses voice channel audio playback."""
    await interaction.response.defer()
    guild = interaction.guild
    paused_voice = False

    if guild and guild.voice_client and guild.voice_client.is_playing():
        guild.voice_client.pause()
        paused_voice = True

    sp = bot.ensure_spotify()
    if sp:
        try:
            sp.pause_playback()
        except Exception:
            pass

    if paused_voice:
        await interaction.followup.send("⏸️ Paused voice channel playback.")
    else:
        await interaction.followup.send("⏸️ Playback paused.")


@bot.tree.command(name="resume", description="Resume paused music playback")
async def resume(interaction: discord.Interaction):
    """Resumes paused voice channel audio playback."""
    await interaction.response.defer()
    guild = interaction.guild

    if guild and guild.voice_client and guild.voice_client.is_paused():
        guild.voice_client.resume()
        await interaction.followup.send("▶️ Resumed voice channel playback.")
        return

    sp = bot.ensure_spotify()
    if sp:
        try:
            sp.start_playback()
            await interaction.followup.send("▶️ Resumed Spotify playback.")
            return
        except Exception:
            pass

    await interaction.followup.send("⚠️ Nothing is currently paused.")


@bot.tree.command(name="stop", description="Stop playback and disconnect the bot from voice")
async def stop(interaction: discord.Interaction):
    """Stops audio streaming and disconnects the bot from the voice channel."""
    guild = interaction.guild
    if not guild or not guild.voice_client:
        await interaction.response.send_message("🔇 The bot is not currently connected to a voice channel.")
        return

    voice_client: discord.VoiceClient = guild.voice_client
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
    await voice_client.disconnect()
    bot.voice_alone_since.pop(guild.id, None)
    await interaction.response.send_message("⏹️ Disconnected from voice channel and stopped playback.")


@bot.tree.command(name="nowplaying", description="Show the currently playing track and progress on Spotify")
async def nowplaying(interaction: discord.Interaction):
    """Fetches the currently playing track and displays track name, artist, and playback progress."""
    await interaction.response.defer()
    sp = bot.ensure_spotify()
    if not sp:
        await interaction.followup.send("⚠️ Spotify client is not authenticated. Please run `python spotify_auth.py` first.")
        return

    try:
        current = sp.current_playback()
        if not current or not current.get("item"):
            await interaction.followup.send("🔇 Nothing is currently playing on Spotify.")
            return

        item = current["item"]
        track_name = item.get("name", "Unknown Title")
        artists = ", ".join(artist["name"] for artist in item.get("artists", [])) or "Unknown Artist"
        album = item.get("album", {}).get("name", "Unknown Album")
        progress_ms = current.get("progress_ms", 0)
        duration_ms = item.get("duration_ms", 0)
        is_playing = current.get("is_playing", False)
        track_url = item.get("external_urls", {}).get("spotify", "")

        progress_bar = create_progress_bar(progress_ms, duration_ms)
        time_display = f"{format_duration(progress_ms)} / {format_duration(duration_ms)}"
        status_label = "Playing" if is_playing else "Paused"
        status_icon = "▶️" if is_playing else "⏸️"

        embed = discord.Embed(
            title=f"{status_icon} Currently {status_label}",
            color=discord.Color.green() if is_playing else discord.Color.dark_gray(),
        )

        if track_url:
            embed.description = f"**[{track_name}]({track_url})**\nby **{artists}**\n*Album:* {album}"
        else:
            embed.description = f"**{track_name}**\nby **{artists}**\n*Album:* {album}"

        embed.add_field(
            name="Progress",
            value=f"`{progress_bar}`\n`{time_display}`",
            inline=False,
        )

        device = current.get("device", {})
        device_name = device.get("name")
        device_type = device.get("type", "Device")
        if device_name:
            embed.set_footer(text=f"Playing on {device_name} ({device_type})")

        images = item.get("album", {}).get("images", [])
        if images:
            embed.set_thumbnail(url=images[0]["url"])

        await interaction.followup.send(embed=embed)

    except SpotifyException as exc:
        await interaction.followup.send(handle_spotify_error(exc))
    except Exception as exc:
        logger.exception("Unexpected error in /nowplaying: %s", exc)
        await interaction.followup.send(f"⚠️ Error fetching current playback: {exc}")


@bot.tree.command(name="autosync", description="Toggle whether the bot automatically streams new tracks to voice")
@app_commands.describe(enabled="Enable or disable automatic voice synchronization")
async def autosync(interaction: discord.Interaction, enabled: bool):
    """Toggles automatic voice channel track synchronization when Spotify track changes."""
    bot.auto_sync_voice = enabled
    state_str = "enabled 🟢" if enabled else "disabled 🔴"
    await interaction.response.send_message(
        f"🔄 Automatic voice synchronization has been **{state_str}**.\n"
        "When active and in a voice channel, new tracks played on Spotify will auto-stream into voice."
    )


if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        raise ValueError(
            "Please provide a valid DISCORD_TOKEN in your .env file before running bot.py."
        )

    bot.run(DISCORD_TOKEN)
