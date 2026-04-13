import json
import time
import urllib.request
import urllib.error
import logging
from typing import List, Dict, Any, Optional
from config import MODEL_PROVIDERS, OLLAMA_BASE

logger = logging.getLogger(__name__)

class ModelResponse:
    def __init__(self, content: str, provider: str, model: str, tokens_used: int = 0,
                 latency_ms: int = 0, prompt_tokens: int = 0, completion_tokens: int = 0,
                 tok_per_sec: float = 0.0):
        self.content = content
        self.provider = provider
        self.model = model
        self.tokens_used = tokens_used
        self.latency_ms = latency_ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.tok_per_sec = tok_per_sec

    def timing_str(self) -> str:
        parts = [f"{self.latency_ms}ms"]
        if self.completion_tokens:
            parts.append(f"{self.completion_tokens}tok")
        if self.tok_per_sec:
            parts.append(f"{self.tok_per_sec:.1f}t/s")
        return " ".join(parts)

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
            "options": options or {"temperature": 0.2, "num_predict": 1024},
        }).encode()

        req = urllib.request.Request(
            OLLAMA_BASE + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        latency_ms = int((time.perf_counter() - t0) * 1000)

        # Ollama returns nanosecond timing fields
        prompt_tok = data.get("prompt_eval_count", 0)
        comp_tok = data.get("eval_count", 0)
        eval_ns = data.get("eval_duration", 0) or 1
        tps = (comp_tok / (eval_ns / 1e9)) if eval_ns else 0.0

        return ModelResponse(
            content=data["message"]["content"],
            provider="ollama",
            model=self.model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tok,
            completion_tokens=comp_tok,
            tok_per_sec=tps,
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
        elif self.provider_type == "google":
            return self._call_google(messages)
        else:
            raise ValueError(f"Unsupported API provider: {self.provider_type}")

    def _call_google(self, messages: List[Dict[str, str]]) -> ModelResponse:
        """
        Google AI Studio (Gemini + Gemma). Free tier supports models like
        gemma-3-27b-it, gemma-3-12b-it, gemini-2.0-flash.
        Gemma variants do NOT support systemInstruction — we prepend system
        as a leading user turn instead.
        """
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        is_gemma = self.model.lower().startswith("gemma")

        system_text = ""
        contents = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system_text = (system_text + "\n" + m["content"]).strip()
                continue
            g_role = "user" if role == "user" else "model"
            # Merge consecutive same-role turns (Gemma requires strict alternation)
            if contents and contents[-1]["role"] == g_role:
                contents[-1]["parts"][0]["text"] += "\n\n" + m["content"]
            else:
                contents.append({"role": g_role, "parts": [{"text": m["content"]}]})

        # First turn must be user
        if contents and contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": "(continuing conversation)"}]})

        if system_text and is_gemma:
            # Inject system as first user turn with a model ack
            contents = [
                {"role": "user", "parts": [{"text": f"[System instructions]\n{system_text}"}]},
                {"role": "model", "parts": [{"text": "Understood."}]},
            ] + contents

        body_dict = {"contents": contents}
        if system_text and not is_gemma:
            body_dict["systemInstruction"] = {"parts": [{"text": system_text}]}

        body = json.dumps(body_dict).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            logger.error(f"[google] HTTP {he.code} — body: {err_body}")
            raise RuntimeError(f"Google API HTTP {he.code}: {err_body}") from he
        latency_ms = int((time.perf_counter() - t0) * 1000)

        # Detect safety block / empty candidate
        if not data.get("candidates"):
            pf = data.get("promptFeedback", {})
            raise RuntimeError(f"Google API returned no candidates (promptFeedback={pf})")
        cand = data["candidates"][0]
        if cand.get("finishReason") in ("SAFETY", "RECITATION", "BLOCKLIST"):
            raise RuntimeError(f"Google blocked response: {cand.get('finishReason')} (safetyRatings={cand.get('safetyRatings')})")
        try:
            content = cand["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Google API unexpected response: {data}") from e

        usage = data.get("usageMetadata", {})
        pt = usage.get("promptTokenCount", 0)
        ct = usage.get("candidatesTokenCount", 0)
        tps = (ct / (latency_ms / 1000)) if latency_ms else 0.0
        return ModelResponse(content=content, provider="google", model=self.model,
                             latency_ms=latency_ms, prompt_tokens=pt, completion_tokens=ct, tok_per_sec=tps)

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
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        latency_ms = int((time.perf_counter() - t0) * 1000)

        content = data["content"][0]["text"]
        usage = data.get("usage", {})
        pt = usage.get("input_tokens", 0)
        ct = usage.get("output_tokens", 0)
        tps = (ct / (latency_ms / 1000)) if latency_ms else 0.0
        return ModelResponse(content=content, provider="anthropic", model=self.model,
                             latency_ms=latency_ms, prompt_tokens=pt, completion_tokens=ct, tok_per_sec=tps)

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
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        latency_ms = int((time.perf_counter() - t0) * 1000)

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        tps = (ct / (latency_ms / 1000)) if latency_ms else 0.0
        return ModelResponse(content=content, provider="openai", model=self.model,
                             latency_ms=latency_ms, prompt_tokens=pt, completion_tokens=ct, tok_per_sec=tps)

class SmartProvider:
    def __init__(self):
        self.reload()

    def reload(self):
        """Rebuild primary/fallback from the current config.MODEL_PROVIDERS dict."""
        import config as _config
        primary_cfg = _config.MODEL_PROVIDERS["primary"]
        fallback_cfg = _config.MODEL_PROVIDERS["fallback"]
        self.primary = self._create_provider(primary_cfg)
        self.fallback = self._create_provider(fallback_cfg)
        self._last_provider = None
        self._last_model = None
        logger.info(f"SmartProvider reloaded. Primary={primary_cfg}, Fallback={fallback_cfg}")

    def _create_provider(self, cfg: Dict) -> BaseProvider:
        if cfg["type"] == "local":
            return LocalProvider(cfg["model"])
        elif cfg["type"] == "api":
            return APIProvider(cfg["provider"], cfg["api_key"], cfg["model"])
        else:
            raise ValueError(f"Unknown provider type: {cfg['type']}")

    def describe(self) -> Dict[str, str]:
        """Return a snapshot of what the primary and fallback are set to."""
        return {
            "primary_provider": getattr(self.primary, "provider_type", "ollama"),
            "primary_model": self.primary.model,
            "fallback_provider": getattr(self.fallback, "provider_type", "ollama"),
            "fallback_model": self.fallback.model,
            "last_used_provider": getattr(self, "_last_provider", "unknown"),
            "last_used_model": getattr(self, "_last_model", "unknown"),
        }

    def chat(self, messages: List[Dict[str, str]], options: Optional[Dict] = None) -> ModelResponse:
        try:
            # Try primary provider
            logger.info(f"Attempting chat with primary provider ({self.primary.__class__.__name__})")
            resp = self.primary.chat(messages, options)
            self._last_provider = resp.provider
            self._last_model = resp.model
            self._last_timing = resp.timing_str()
            logger.info(f"[llm] primary ok — {resp.provider}/{resp.model} {resp.timing_str()}")
            return resp
        except Exception as e:
            # Check if it's a token/quota error or connection error
            error_msg = str(e).lower()
            if "rate_limit" in error_msg or "quota" in error_msg or "429" in error_msg or "401" in error_msg:
                logger.warning(f"Primary provider failed (Token/Quota/Auth): {e}. Falling back to local model...")
            else:
                logger.error(f"Primary provider unexpected error: {e}. Falling back to local model...")

            # Fallback to local
            resp = self.fallback.chat(messages, options)
            self._last_provider = resp.provider
            self._last_model = resp.model
            self._last_timing = resp.timing_str()
            logger.info(f"[llm] fallback used — {resp.provider}/{resp.model} {resp.timing_str()}")
            return resp

# Global instance for the system
smart_provider = SmartProvider()
