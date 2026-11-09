import asyncio
import logging
import os
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import spotipy
from spotipy.exceptions import SpotifyException
import yt_dlp

from spotify_auth import (
    DEFAULT_CACHE_PATH,
    get_cached_token,
    get_spotify_oauth,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ZpotiBot")

load_dotenv()

DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip().strip("'\"")
SPOTIFY_REFRESH_TOKEN = (os.getenv("SPOTIFY_REFRESH_TOKEN") or "").strip().strip("'\"")

# Initialize static-ffmpeg for headless container hosting (e.g., Bot-Hosting.net)
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    logger.info("static-ffmpeg initialized successfully.")
except Exception as exc:
    logger.debug("static-ffmpeg not loaded or add_paths deferred: %s", exc)

FFMPEG_EXECUTABLE = os.getenv("FFMPEG_PATH") or "ffmpeg"


def get_spotify_client(cache_path: str = DEFAULT_CACHE_PATH) -> Optional[spotipy.Spotify]:
    """Instantiates a Spotify client using SPOTIFY_REFRESH_TOKEN from env or local cached tokens."""
    # Priority 1: Direct SPOTIFY_REFRESH_TOKEN environment variable (Ideal for headless/Bot-Hosting.net)
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

    # Priority 2: Local token cache file (.spotify_cache)
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


def handle_spotify_error(exc: SpotifyException) -> str:
    """Translates Spotify API exceptions into user-friendly error messages."""
    error_msg = str(exc)
    if exc.http_status == 404 or "NO_ACTIVE_DEVICE" in error_msg:
        return "⚠️ **No active Spotify device found.** Please open Spotify on your device and start playback first."
    if exc.http_status == 403:
        if "PREMIUM_REQUIRED" in error_msg:
            return "⚠️ **Spotify Premium required.** Controlling playback requires an active Spotify Premium subscription."
        return f"⚠️ **Playback restriction:** {exc.msg or error_msg}"
    return f"⚠️ **Spotify API error ({exc.http_status}):** {exc.msg or error_msg}"


async def fetch_youtube_stream(track: dict) -> Optional[Dict[str, Any]]:
    """Searches YouTube using yt-dlp for a Spotify track and extracts audio streaming info."""
    track_name = track.get("name", "Unknown Track")
    artists_list = [artist.get("name") for artist in track.get("artists", []) if artist.get("name")]
    artists_str = ", ".join(artists_list) or "Unknown Artist"
    search_query = f"{track_name} {artists_str}".strip()

    logger.info("Searching YouTube for '%s'...", search_query)

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "extract_flat": False,
    }

    loop = asyncio.get_running_loop()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_target = f"ytsearch1:{search_query} audio"
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(search_target, download=False)
            )
            if info and "entries" in info and info["entries"]:
                entry = info["entries"][0]
                audio_url = entry.get("url")
                if audio_url:
                    return {
                        "audio_url": audio_url,
                        "title": entry.get("title", track_name),
                        "webpage_url": entry.get("webpage_url", ""),
                        "thumbnail": entry.get("thumbnail"),
                        "track_name": track_name,
                        "artists_str": artists_str,
                    }
    except Exception as exc:
        logger.error("yt-dlp extraction failed for '%s': %s", search_query, exc)

    return None


async def stream_to_voice_client(voice_client: discord.VoiceClient, stream_info: Dict[str, Any]) -> bool:
    """Streams audio to an active discord.VoiceClient using FFmpeg."""
    ffmpeg_options = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn",
    }
    try:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        def after_playback(error):
            if error:
                logger.error("Error during voice playback: %s", error)

        audio_source = discord.FFmpegPCMAudio(
            stream_info["audio_url"],
            executable=FFMPEG_EXECUTABLE,
            **ffmpeg_options,
        )
        voice_client.play(audio_source, after=after_playback)
        return True
    except Exception as exc:
        logger.error("Failed to stream to voice client: %s", exc)
        return False


async def play_audio(interaction: discord.Interaction, track: dict) -> bool:
    """Given a Spotify track object, searches YouTube using yt-dlp and streams the audio
    to the Discord voice channel the user is currently in.
    
    Handles errors gracefully (e.g. user not in voice channel, search failure, or stream issues).
    """
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

    # Extract YouTube stream information
    stream_info = await fetch_youtube_stream(track)
    if not stream_info:
        await interaction.followup.send(f"🔍 No audio stream found on YouTube for **{track_name}**.")
        return False

    # Stream to voice
    success = await stream_to_voice_client(voice_client, stream_info)
    if not success:
        await interaction.followup.send(f"⚠️ Failed to initiate audio streaming for **{track_name}**.")
        return False

    embed = discord.Embed(
        title="🔊 Streaming Audio to Voice Channel",
        description=f"**[{track_name}]({stream_info['webpage_url']})**\nby **{stream_info['artists_str']}**",
        color=discord.Color.brand_green(),
    )
    embed.add_field(name="Voice Channel", value=f"`{voice_channel.name}`", inline=True)
    embed.add_field(name="YouTube Match", value=f"[{stream_info['title']}]({stream_info['webpage_url']})", inline=True)
    if stream_info.get("thumbnail"):
        embed.set_thumbnail(url=stream_info["thumbnail"])
    embed.set_footer(text="Powered by yt-dlp & Discord Voice")

    await interaction.followup.send(embed=embed)
    return True


class SpotifyDiscordBot(commands.Bot):
    """Discord Bot class managing application slash commands, Spotify client,
    and a 5-second background polling task for /me/player/currently-playing.
    """

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

        self.sp: Optional[spotipy.Spotify] = None
        self.last_track_id: Optional[str] = None
        self.last_is_playing: bool = False
        self.auto_sync_voice: bool = True  # Auto stream new tracks to connected voice channels

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

        # Start background polling task
        self.spotify_poller.start()

    async def close(self):
        # Gracefully cancel polling task on shutdown
        if self.spotify_poller.is_running():
            self.spotify_poller.cancel()
        await super().close()

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        print(f"Bot is ready! Logged in as {self.user} (ID: {self.user.id})")

    @tasks.loop(seconds=5.0)
    async def spotify_poller(self):
        """Polls Spotify's /me/player/currently-playing endpoint every 5 seconds.
        Reflects changes in the bot's presence and synchronizes voice streaming if active.
        """
        sp = self.ensure_spotify()
        if not sp:
            return

        loop = asyncio.get_running_loop()
        try:
            # Poll /me/player/currently-playing asynchronously in executor
            playback = await loop.run_in_executor(None, sp.currently_playing)
        except SpotifyException as exc:
            logger.warning("Spotify polling API error: %s", exc)
            return
        except Exception as exc:
            logger.debug("Transient error polling Spotify playback: %s", exc)
            return

        # Case 1: Nothing currently playing or player is inactive
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

        # Case 2: Track has changed
        if track_id != self.last_track_id:
            logger.info("Track change detected on Spotify: '%s' by '%s' [ID: %s]", track_name, artists, track_id)
            self.last_track_id = track_id
            self.last_is_playing = is_playing

            # 1. Update Discord bot presence
            presence_text = f"{track_name} • {artists}"[:128]
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening if is_playing else discord.ActivityType.watching,
                    name=presence_text,
                )
            )

            # 2. If voice clients are connected and auto-sync is enabled, stream the new track
            if self.auto_sync_voice and is_playing and self.voice_clients:
                logger.info("Auto-syncing new Spotify track to %d voice channel(s)...", len(self.voice_clients))
                stream_info = await fetch_youtube_stream(item)
                if stream_info:
                    for vc in self.voice_clients:
                        if vc.is_connected():
                            await stream_to_voice_client(vc, stream_info)

        # Case 3: Same track, but play/pause state toggled
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
        """Wait until the bot is completely logged in before starting the polling loop."""
        await self.wait_until_ready()


bot = SpotifyDiscordBot()


@bot.tree.command(name="status", description="Check if the bot is alive and operational")
async def status(interaction: discord.Interaction):
    """Basic slash command that prints and replies that the bot is alive."""
    print(f"[/status] Executed by {interaction.user} in #{interaction.channel}")

    spotify_status = "Connected 🟢" if bot.ensure_spotify() is not None else "Not authenticated (run spotify_auth.py) 🟡"
    latency_ms = round(bot.latency * 1000, 1)
    sync_status = "Enabled 🟢" if bot.auto_sync_voice else "Disabled 🔴"

    message = (
        f"🤖 **Bot is alive!**\n"
        f"• **Latency:** {latency_ms}ms\n"
        f"• **Spotify:** {spotify_status}\n"
        f"• **Voice Auto-Sync:** {sync_status}\n"
        f"• **Active Track:** {bot.last_track_id or 'None / Idle'}"
    )
    await interaction.response.send_message(message)


@bot.tree.command(name="play", description="Resume Spotify playback or search and play a track")
@app_commands.describe(query="Track name, artist, or Spotify URI/URL (optional; resumes playback if omitted)")
async def play(interaction: discord.Interaction, query: Optional[str] = None):
    """Resumes playback or searches and starts playing a track on Spotify."""
    await interaction.response.defer()
    sp = bot.ensure_spotify()
    if not sp:
        await interaction.followup.send("⚠️ Spotify client is not authenticated. Please run `python spotify_auth.py` first.")
        return

    try:
        if query:
            query = query.strip()
            # If a Spotify URI or URL was passed
            if query.startswith("spotify:track:") or "open.spotify.com/track/" in query:
                sp.start_playback(uris=[query])
                await interaction.followup.send(f"▶️ Playing specified track: <{query}>")
            else:
                # Search for track by query string
                search_results = sp.search(q=query, limit=1, type="track")
                tracks = search_results.get("tracks", {}).get("items", [])
                if not tracks:
                    await interaction.followup.send(f"🔍 No Spotify tracks found for: `{query}`")
                    return

                track = tracks[0]
                track_uri = track.get("uri")
                track_name = track.get("name")
                artist_name = track.get("artists", [{}])[0].get("name", "Unknown Artist")

                sp.start_playback(uris=[track_uri])
                await interaction.followup.send(f"▶️ Playing **{track_name}** by **{artist_name}**")
        else:
            # Resume existing playback
            sp.start_playback()
            await interaction.followup.send("▶️ Resumed Spotify playback.")

    except SpotifyException as exc:
        await interaction.followup.send(handle_spotify_error(exc))
    except Exception as exc:
        logger.exception("Unexpected error in /play: %s", exc)
        await interaction.followup.send(f"⚠️ Error executing play command: {exc}")


@bot.tree.command(name="pause", description="Pause current Spotify playback")
async def pause(interaction: discord.Interaction):
    """Pauses current Spotify playback."""
    await interaction.response.defer()
    sp = bot.ensure_spotify()
    if not sp:
        await interaction.followup.send("⚠️ Spotify client is not authenticated. Please run `python spotify_auth.py` first.")
        return

    try:
        sp.pause_playback()
        await interaction.followup.send("⏸️ Paused Spotify playback.")
    except SpotifyException as exc:
        await interaction.followup.send(handle_spotify_error(exc))
    except Exception as exc:
        logger.exception("Unexpected error in /pause: %s", exc)
        await interaction.followup.send(f"⚠️ Error executing pause command: {exc}")


@bot.tree.command(name="next", description="Skip to the next Spotify track")
async def next_track(interaction: discord.Interaction):
    """Skips to the next track on Spotify."""
    await interaction.response.defer()
    sp = bot.ensure_spotify()
    if not sp:
        await interaction.followup.send("⚠️ Spotify client is not authenticated. Please run `python spotify_auth.py` first.")
        return

    try:
        sp.next_track()
        await interaction.followup.send("⏭️ Skipped to next track.")
    except SpotifyException as exc:
        await interaction.followup.send(handle_spotify_error(exc))
    except Exception as exc:
        logger.exception("Unexpected error in /next: %s", exc)
        await interaction.followup.send(f"⚠️ Error executing next command: {exc}")


@bot.tree.command(name="previous", description="Skip to the previous Spotify track")
async def previous_track(interaction: discord.Interaction):
    """Skips to the previous track on Spotify."""
    await interaction.response.defer()
    sp = bot.ensure_spotify()
    if not sp:
        await interaction.followup.send("⚠️ Spotify client is not authenticated. Please run `python spotify_auth.py` first.")
        return

    try:
        sp.previous_track()
        await interaction.followup.send("⏮️ Skipped to previous track.")
    except SpotifyException as exc:
        await interaction.followup.send(handle_spotify_error(exc))
    except Exception as exc:
        logger.exception("Unexpected error in /previous: %s", exc)
        await interaction.followup.send(f"⚠️ Error executing previous command: {exc}")


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

        # Progress bar calculation
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


@bot.tree.command(name="stream", description="Stream a Spotify track into your Discord voice channel via YouTube")
@app_commands.describe(query="Track title or search query (optional; streams Spotify's now-playing track if omitted)")
async def stream(interaction: discord.Interaction, query: Optional[str] = None):
    """Slash command to find and stream a Spotify track to the user's voice channel using play_audio."""
    await interaction.response.defer()
    sp = bot.ensure_spotify()

    track_obj: Optional[dict] = None

    if query:
        query = query.strip()
        if sp:
            try:
                results = sp.search(q=query, limit=1, type="track")
                items = results.get("tracks", {}).get("items", [])
                if items:
                    track_obj = items[0]
            except Exception as exc:
                logger.warning("Spotify track search failed: %s; falling back to direct query.", exc)

        if not track_obj:
            # Fallback to pseudo track object with query as name
            track_obj = {"name": query, "artists": []}
    else:
        # If no query is provided, attempt to use the track currently playing on Spotify
        if not sp:
            await interaction.followup.send(
                "⚠️ Spotify is not connected. Provide a track query or run `python spotify_auth.py`."
            )
            return

        try:
            current = sp.current_playback()
            if current and current.get("item"):
                track_obj = current["item"]
            else:
                await interaction.followup.send("🔇 No track is currently playing on Spotify. Please provide a track query.")
                return
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not fetch current Spotify track: {exc}")
            return

    await play_audio(interaction, track_obj)


@bot.tree.command(name="autosync", description="Toggle whether the bot automatically streams new tracks to voice")
@app_commands.describe(enabled="Enable or disable automatic voice synchronization")
async def autosync(interaction: discord.Interaction, enabled: bool):
    """Toggles automatic voice channel track synchronization when Spotify track changes."""
    bot.auto_sync_voice = enabled
    state_str = "enabled 🟢" if enabled else "disabled 🔴"
    await interaction.response.send_message(
        f"🔄 Automatic voice synchronization has been **{state_str}**.\n"
        "When active and in a voice channel, new tracks played on Spotify will auto-stream from YouTube."
    )


@bot.tree.command(name="stop", description="Stop streaming audio and disconnect from voice")
async def stop(interaction: discord.Interaction):
    """Stops audio streaming and disconnects the bot from voice."""
    guild = interaction.guild
    if not guild or not guild.voice_client:
        await interaction.response.send_message("🔇 The bot is not currently connected to a voice channel.")
        return

    voice_client: discord.VoiceClient = guild.voice_client
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
    await voice_client.disconnect()
    await interaction.response.send_message("⏹️ Disconnected from voice channel and stopped playback.")


if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        raise ValueError(
            "Please provide a valid DISCORD_TOKEN in your .env file before running bot.py."
        )

    bot.run(DISCORD_TOKEN)
