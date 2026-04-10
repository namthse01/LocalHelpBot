import discord
import requests
import json
import sys
import os

# Add parent directory to sys.path to allow importing from config.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DISCORD_TOKEN, DISCORD_SERVER_ID, PROXY_PORT, ALLOWED_CHANNELS, ALLOWED_CHANNELS

# Setup Discord Intents
# Note: Message Content Intent must be enabled in the Discord Developer Portal
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

PROXY_URL = f"http://localhost:{PROXY_PORT}/api/chat"

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print(f'Monitoring Server ID: {DISCORD_SERVER_ID}')
    print('Discord Gateway is active and connected to LocalHelpBot!')

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Only respond in the specified server and allowed channels
    if message.guild and message.guild.id != DISCORD_SERVER_ID:
        return

    if message.channel.id not in ALLOWED_CHANNELS:
        return

    # Handle the request
    async with message.channel.typing():
        try:
            # We use the 'auto-agent' so the bot automatically switches specialists
            payload = {
                "model": "auto-agent",
                "messages": [
                    {"role": "user", "content": message.content}
                ],
                "stream": False
            }

            # Call the local RAG proxy
            response = requests.post(PROXY_URL, json=payload, timeout=300)

            if response.status_code == 200:
                data = response.json()
                # Extract content from Ollama/Proxy response format
                reply_text = data.get("message", {}).get("content", "I'm sorry, I couldn't generate a response.")

                # Discord has a 2000 character limit per message
                if len(reply_text) > 2000:
                    for i in range(0, len(reply_text), 2000):
                        await message.reply(reply_text[i:i+2000])
                else:
                    await message.reply(reply_text)
            else:
                await message.reply(f"❌ Proxy Error ({response.status_code}): Unable to get a response from the local bot.")

        except requests.exceptions.ConnectionError:
            await message.reply("⚠️ LocalHelpBot Proxy is not running. Please start the proxy first!")
        except Exception as e:
            await message.reply(f"⚠️ An unexpected error occurred: {str(e)}")

if __name__ == "__main__":
    print("Starting Discord Gateway...")
    try:
        client.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("Error: Invalid Discord Token provided in config.py")
    except Exception as e:
        print(f"Critical Error: {e}")
