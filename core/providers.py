import json
import urllib.request
import urllib.error
import logging
from typing import List, Dict, Any, Optional
from config import MODEL_PROVIDERS, OLLAMA_BASE

logger = logging.getLogger(__name__)

class ModelResponse:
    def __init__(self, content: str, provider: str, model: str, tokens_used: int = 0):
        self.content = content
        self.provider = provider
        self.model = model
        self.tokens_used = tokens_used

class BaseProvider:
    def chat(self, messages: List[Dict[str, str]], options: Optional[Dict] = None) -> ModelResponse:
        raise NotImplementedError()

class LocalProvider(BaseProvider):
    def __init__(self, model: str):
        self.model = model

    def chat(self, messages: List[Dict[str, str]], options: Optional[Dict] = None) -> ModelResponse:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options or {"temperature": 0.2, "num_predict": 2048},
        }).encode()

        req = urllib.request.Request(
            OLLAMA_BASE + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())

        return ModelResponse(
            content=data["message"]["content"],
            provider="ollama",
            model=self.model
        )

class APIProvider(BaseProvider):
    def __init__(self, provider_type: str, api_key: str, model: str):
        self.provider_type = provider_type
        self.api_key = api_key
        self.model = model

    def chat(self, messages: List[Dict[str, str]], options: Optional[Dict] = None) -> ModelResponse:
        if self.provider_type == "anthropic":
            return self._call_anthropic(messages)
        elif self.provider_type == "openai":
            return self._call_openai(messages)
        else:
            raise ValueError(f"Unsupported API provider: {self.provider_type}")

    def _call_anthropic(self, messages: List[Dict[str, str]]) -> ModelResponse:
        # Minimal Anthropic API implementation
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        # Convert messages to Anthropic format (system prompt is separate)
        system_prompt = ""
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            else:
                user_messages.append(m)

        body = json.dumps({
            "model": self.model,
            "system": system_prompt,
            "messages": user_messages,
            "max_tokens": 2048
        }).encode()

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())

        content = data["content"][0]["text"]
        return ModelResponse(content=content, provider="anthropic", model=self.model)

    def _call_openai(self, messages: List[Dict[str, str]]) -> ModelResponse:
        # Minimal OpenAI API implementation
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.2
        }).encode()

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())

        content = data["choices"][0]["message"]["content"]
        return ModelResponse(content=content, provider="openai", model=self.model)

class SmartProvider:
    def __init__(self):
        primary_cfg = MODEL_PROVIDERS["primary"]
        self.primary = self._create_provider(primary_cfg)

        fallback_cfg = MODEL_PROVIDERS["fallback"]
        self.fallback = self._create_provider(fallback_cfg)

    def _create_provider(self, cfg: Dict) -> BaseProvider:
        if cfg["type"] == "local":
            return LocalProvider(cfg["model"])
        elif cfg["type"] == "api":
            return APIProvider(cfg["provider"], cfg["api_key"], cfg["model"])
        else:
            raise ValueError(f"Unknown provider type: {cfg['type']}")

    def chat(self, messages: List[Dict[str, str]], options: Optional[Dict] = None) -> ModelResponse:
        try:
            # Try primary provider
            logger.info(f"Attempting chat with primary provider ({self.primary.__class__.__name__})")
            return self.primary.chat(messages, options)
        except Exception as e:
            # Check if it's a token/quota error or connection error
            error_msg = str(e).lower()
            if "rate_limit" in error_msg or "quota" in error_msg or "429" in error_msg or "401" in error_msg:
                logger.warning(f"Primary provider failed (Token/Quota/Auth): {e}. Falling back to local model...")
            else:
                logger.error(f"Primary provider unexpected error: {e}. Falling back to local model...")

            # Fallback to local
            return self.fallback.chat(messages, options)

# Global instance for the system
smart_provider = SmartProvider()
