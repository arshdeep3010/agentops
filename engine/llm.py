"""Chat completions on any OpenAI-compatible endpoint (NVIDIA by default, the LLM Gateway later).

The engine treats model output as untrusted: it must parse as JSON and validate against the
step's pydantic schema, or the step retries with the validation error in the prompt.
"""

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class ProviderError(RuntimeError):
    """The model API failed (after its own retries). Retryable at the step level."""


@dataclass
class Reply:
    text: str
    tokens_in: int
    tokens_out: int
    truncated: bool = False  # the model hit max_tokens: the reply is cut off


class OpenAICompatibleChat:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 60.0):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, messages: list[dict], attempts: int = 3, max_tokens: int = 8192) -> Reply:
        if not self.api_key:
            raise ProviderError("no LLM_API_KEY configured (set LLM_MODE=fake to run offline)")
        payload = {"model": self.model, "messages": messages, "temperature": 0.1, "max_tokens": max_tokens,
                   "chat_template_kwargs": {"enable_thinking": False}}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(1, attempts + 1):
            try:
                resp = httpx.post(self.url, json=payload, headers=headers, timeout=self.timeout_s)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == attempts:
                    raise ProviderError(f"model API unreachable: {exc}") from exc
            else:
                if resp.status_code < 400:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    try:
                        text = data["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, TypeError) as exc:
                        raise ProviderError("unexpected response shape") from exc
                    truncated = data["choices"][0].get("finish_reason") == "length"
                    return Reply(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), truncated)
                if resp.status_code not in RETRYABLE_STATUS or attempt == attempts:
                    # Never echo request headers: they carry the key.
                    raise ProviderError(f"model API returned {resp.status_code}: {resp.text[:200]}")
            time.sleep(2 ** (attempt - 1))
        raise ProviderError("model API failed")


def extract_json(text: str) -> dict:
    """First JSON object in a reply (tolerates <think> blocks and ``` fences)."""
    cleaned = _THINK.sub("", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(cleaned[start:end + 1])


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
