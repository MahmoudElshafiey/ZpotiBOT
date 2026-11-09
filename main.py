import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import spotipy
from spotify_auth import get_authenticated_spotify

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Configure Discord bot intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def get_spotify_client() -> spotipy.Spotify:
    """Initializes and returns an authenticated Spotify client via token cache."""
    return get_authenticated_spotify()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    print("Bot is ready to control Spotify playback!")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Simple healthcheck command."""
    await ctx.send("Pong!")


if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        raise ValueError("Please provide a valid DISCORD_TOKEN in the .env file.")
    bot.run(DISCORD_TOKEN)
