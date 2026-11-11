"""Compatibility entry point.

The real bot lives in bot.py. This shim keeps ``python main.py`` working for
panels or scripts that are configured to use main.py as the startup file.
"""

from bot import DISCORD_TOKEN, bot

if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        raise ValueError("Please provide a valid DISCORD_TOKEN in the .env file.")
    bot.run(DISCORD_TOKEN)
