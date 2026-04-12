import threading
import time
import logging
from datetime import datetime
from config import AUTOMATION_TASKS, DISCORD_SETTINGS
from core.orchestrator import AgentOrchestrator
import requests

logger = logging.getLogger(__name__)

class TaskScheduler:
    def __init__(self, orchestrator: AgentOrchestrator, discord_client):
        self.orchestrator = orchestrator
        self.discord_client = discord_client
        self.running = False
        self._thread = None

    def start(self):
        """Start the scheduling loop in a background thread."""
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Automation Scheduler started.")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join()

    def _run_loop(self):
        """The main loop that checks for scheduled tasks every minute."""
        while self.running:
            now = datetime.now().strftime("%H:%M")
            for task in AUTOMATION_TASKS:
                if task["schedule"] == now:
                    logger.info(f"Triggering scheduled task: {task['id']}")
                    self._execute_task(task)

            # Sleep for 60 seconds to avoid double-triggering in the same minute
            time.sleep(60)

    def _execute_task(self, task):
        """Executes an automation task and sends the result to Discord."""
        try:
            # Run the task via the orchestrator
            result = self.orchestrator.handle_request(task["prompt"])

            # Send to the specified Discord recipient
            recipient_id = task.get("recipient")
            if recipient_id:
                self._send_to_discord(recipient_id, f"🤖 **Automated Task: {task['id']}**\n\n{result}")
            else:
                logger.warning(f"No recipient specified for task {task['id']}. Result: {result}")

        except Exception as e:
            logger.exception(f"Error executing scheduled task {task['id']}: {e}")

    def _send_to_discord(self, channel_id: int, message: str):
        """Sends a message to a Discord channel using the discord_client."""
        # Since discord_client is an async client, we need to run this in the event loop
        import asyncio

        async def send():
            try:
                channel = self.discord_client.get_channel(channel_id)
                if channel:
                    await channel.send(message)
                else:
                    logger.error(f"Could not find Discord channel {channel_id}")
            except Exception as e:
                logger.error(f"Failed to send discord message: {e}")

        # Schedule the async function to run in the discord client's event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(send())
            else:
                loop.run_until_complete(send())
        except Exception as e:
            logger.error(f"Async loop error: {e}")
