import asyncio
import logging
import os
import shutil
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


async def fetch_youtube_stream(track: dict) -> Optional[Dict[str, Any]]:
    """Searches YouTube using yt-dlp using Spotify track metadata to extract direct audio stream URL."""
    track_name = track.get("name", "Unknown Track")
    artists_list = [artist.get("name") for artist in track.get("artists", []) if artist.get("name")]
    artists_str = ", ".join(artists_list) or "Unknown Artist"
    search_query = f"{track_name} {artists_str}".strip()

    logger.info("Extracting YouTube audio stream for Spotify track: '%s' by '%s'...", track_name, artists_str)

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
    """Streams audio to an active discord.VoiceClient using resolved FFmpeg."""
    ffmpeg_exe = get_ffmpeg_binary()
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
            executable=ffmpeg_exe,
            **ffmpeg_options,
        )
        voice_client.play(audio_source, after=after_playback)
        return True
    except Exception as exc:
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

    # Extract stream URL
    stream_info = await fetch_youtube_stream(track)
    if not stream_info:
        await interaction.followup.send(f"🔍 Could not find an audio stream matching Spotify track **{track_name}**.")
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
    5-second Spotify background status sync, and 10-minute alone voice auto-disconnect.
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
            playback = await loop.run_in_executor(None, sp.currently_playing)
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

            # Auto-sync to connected voice channels if enabled
            if self.auto_sync_voice and is_playing and self.voice_clients:
                logger.info("Auto-syncing new Spotify track to %d voice channel(s)...", len(self.voice_clients))
                stream_info = await fetch_youtube_stream(item)
                if stream_info:
                    for vc in self.voice_clients:
                        if vc.is_connected():
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
