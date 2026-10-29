"""Pluggable chat models. Standard library only; API keys come from environment variables, never files.

Select with FENSHEN_LLM:
    ollama:qwen2.5:7b                 local Ollama (default), OLLAMA_HOST overrides http://localhost:11434
    openai:<model>                    any OpenAI-compatible endpoint: OPENAI_BASE_URL + OPENAI_API_KEY
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    data: dict
    raw: str
    latency_ms: int
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    name: str

    def decide(self, system: str, user: str, schema: dict) -> LLMReply: ...


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 180) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"{url} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"{url} unreachable: {e.reason}") from e


def _parse(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise LLMError(f"model did not return JSON: {raw[:200]!r}")
    return json.loads(text[start : end + 1])


def estimate_tokens(text: str) -> int:
    """Rough count for Qwen-style tokenizers: ~1 token per CJK character, ~3.2 characters per token otherwise."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return int(cjk + (len(text) - cjk) / 3.2)


class OllamaLLM:
    def __init__(self, model: str = "qwen2.5:7b", host: str | None = None, temperature: float = 0.0):
        self.model = model
        self.name = f"ollama:{model}"
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        self.temperature = temperature

    def decide(self, system: str, user: str, schema: dict) -> LLMReply:
        t = time.perf_counter()
        # The context size decides how much of the model fits on an 8 GB GPU shared with the emulator:
        # measured decode speed was ~12 tok/s at 4096 (10% on CPU) and ~21 tok/s at 2048 (8% on CPU).
        # Use 2048 unless the prompt might not fit; Ollama would silently cut the start (the system prompt).
        needed = estimate_tokens(system) + estimate_tokens(user) + 200
        num_ctx = int(os.environ.get("FENSHEN_NUM_CTX", 0)) or (2048 if needed <= 1900 else 4096)
        resp = _post_json(
            f"{self.host}/api/chat",
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "format": schema,  # structured output: the reply must match the action schema
                "stream": False,
                "keep_alive": "30m",
                "options": {"temperature": self.temperature, "num_ctx": num_ctx},
            },
        )
        raw = resp.get("message", {}).get("content", "")
        return LLMReply(_parse(raw), raw, int((time.perf_counter() - t) * 1000), self.name,
                        resp.get("prompt_eval_count", 0), resp.get("eval_count", 0))


class OpenAICompatLLM:
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None, temperature: float = 0.0):
        self.model = model
        self.name = f"openai:{model}"
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        self.temperature = temperature

    def decide(self, system: str, user: str, schema: dict) -> LLMReply:
        t = time.perf_counter()
        resp = _post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": self.temperature,
                "response_format": {"type": "json_schema", "json_schema": {"name": "action", "schema": schema}},
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        raw = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage", {})
        return LLMReply(_parse(raw), raw, int((time.perf_counter() - t) * 1000), self.name,
                        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def from_env(spec: str | None = None) -> LLM:
    spec = spec or os.environ.get("FENSHEN_LLM") or "ollama:qwen2.5:7b"
    kind, _, model = spec.partition(":")
    if kind == "ollama":
        return OllamaLLM(model or "qwen2.5:7b")
    if kind == "openai":
        return OpenAICompatLLM(model)
    raise LLMError(f"unknown FENSHEN_LLM {spec!r}")
