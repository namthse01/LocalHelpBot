import discord
import requests
import json
import sys
import os

# Add parent directory to sys.path to allow importing from config.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DISCORD_TOKEN, DISCORD_SETTINGS, PROXY_PORT
import logging
from core.scheduler import TaskScheduler
from core.orchestrator import AgentOrchestrator
from core.tools import FILE_TOOLS, WEB_TOOLS

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Setup Discord Intents
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

PROXY_URL = f"http://localhost:{PROXY_PORT}/api/chat"

# Initialize Orchestrator for the scheduler to use (if it doesn't just use the proxy)
# Note: Scheduler in our implementation can use the proxy API, but let's give it the orchestrator
# to avoid unnecessary HTTP overhead for internal tasks if we want,
# but using the proxy is more consistent.
all_tools = {**FILE_TOOLS, **WEB_TOOLS}
orchestrator = AgentOrchestrator(all_tools)
scheduler = TaskScheduler(orchestrator, client)

@client.event
async def on_ready():
    logger.info(f'Logged in as {client.user} (ID: {client.user.id})')
    logger.info(f'Monitoring Guilds: {list(DISCORD_SETTINGS["guilds"].keys())}')
    logger.info('Discord Gateway is active and connected to LocalHelpBot!')

    # Start the automation scheduler
    scheduler.start()

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if the message is in a managed guild
    if message.guild:
        guild_id = message.guild.id
        if guild_id not in DISCORD_SETTINGS["guilds"]:
            return

        guild_config = DISCORD_SETTINGS["guilds"][guild_id]
        if not DISCORD_SETTINGS["allow_all_channels"]:
            if message.channel.id not in guild_config.get("allowed_channels", []):
                return
    else:
        # Handle DMs if desired, currently we ignore them if not specified
        return

    # Handle the request
    async with message.channel.typing():
        try:
            payload = {
                "model": "auto-agent",
                "messages": [
                    {"role": "user", "content": message.content}
                ],
                "stream": False
            }

            response = requests.post(PROXY_URL, json=payload, timeout=300)

            if response.status_code == 200:
                data = response.json()
                reply_text = data.get("message", {}).get("content", "I'm sorry, I couldn't generate a response.")

                if len(reply_text) > 2000:
                    for i in range(0, len(reply_text), 2000):
                        await message.reply(reply_text[i:i+2000])
                else:
                    await message.reply(reply_text)
            else:
                logger.error(f"Proxy Error ({response.status_code}): {response.text}")
                await message.reply(f"❌ Proxy Error ({response.status_code}): Unable to get a response from the local bot.")

        except requests.exceptions.ConnectionError:
            logger.error("Connection error to LocalHelpBot Proxy.")
            await message.reply("⚠️ LocalHelpBot Proxy is not running. Please start the proxy first!")
        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")
            await message.reply(f"⚠️ An unexpected error occurred: {str(e)}")

if __name__ == "__main__":
    print("Starting Discord Gateway...")
    try:
        client.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Invalid Discord Token provided in config.py")
    except Exception as e:
        print(f"Critical Error: {e}")
