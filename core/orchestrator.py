import json
import logging
from typing import Dict, List, Any, Optional, Callable
from config import AGENT_PROFILES, OLLAMA_BASE, CHAT_MODEL
import urllib.request

logger = logging.getLogger(__name__)

class AgentOrchestrator:
    def __init__(self, tools_registry: Dict[str, Callable]):
        self.tools_registry = tools_registry
        self.active_sessions = {}

    def _ollama_chat(self, model: str, messages: List[Dict[str, str]]) -> str:
        """Internal helper to call Ollama API."""
        body = json.dumps({
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 2048},
        }).encode()

        req = urllib.request.Request(
            OLLAMA_BASE + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]

    def run_specialist(self, agent_name: str, prompt: str, conversation: Optional[List[Dict[str, str]]] = None) -> str:
        """
        Runs a specific specialist agent loop.
        """
        profile = AGENT_PROFILES.get(agent_name)
        if not profile:
            return f"ERROR: Specialist '{agent_name}' not found."

        # Initialize conversation with system prompt
        messages = [{"role": "system", "content": profile["system_prompt"]}]
        if conversation:
            messages.extend(conversation)
        messages.append({"role": "user", "content": prompt})

        # Filter tools for this specialist
        specialist_tools = {}
        for tool_name in profile.get("tools", []):
            if tool_name == "delegate":
                specialist_tools["delegate"] = self.delegate_tool
            elif tool_name in self.tools_registry:
                specialist_tools[tool_name] = self.tools_registry[tool_name]

        # Import run_agent from core.agent to avoid circular import if necessary,
        # but we'll assume it's available.
        from core.agent import run_agent

        return run_agent(messages, specialist_tools)

    def delegate_tool(self, args: Dict[str, Any]) -> str:
        """
        Tool used by agents to delegate tasks to other specialists.
        Expected args: {"agent": "researcher", "prompt": "..."}
        """
        target_agent = args.get("agent")
        prompt = args.get("prompt")

        if not target_agent or not prompt:
            return "ERROR: Missing 'agent' or 'prompt' arguments for delegation."

        logger.info(f"Delegating task to {target_agent}: {prompt[:100]}...")
        return self.run_specialist(target_agent, prompt)

    def handle_request(self, user_prompt: str, conversation: List[Dict[str, str]] = None) -> str:
        """
        Entry point for the orchestrator. Starts with the 'main' agent.
        """
        return self.run_specialist("main", user_prompt, conversation)
