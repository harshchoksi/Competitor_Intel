"""Minimal Gemini REST client (generateContent). The API key travels in a header and is never logged."""
from __future__ import annotations

import json
import os
import re
import time

import httpx
from loguru import logger

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-lite"  # override with GEMINI_MODEL; free-tier model names change, check Google's docs


class LLMError(Exception):
    """The model could not give a usable answer (network, quota, blocked, or malformed output)."""


class GeminiClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 90.0, max_retries: int = 3, sleep=time.sleep) -> None:
        if not api_key:
            raise LLMError("no Gemini API key")
        self.model, self._key, self._max_retries, self._sleep = model, api_key, max_retries, sleep
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _payload(system: str, prompt: str, json_mode: bool) -> dict:
        cfg: dict = {"temperature": 0.2, "maxOutputTokens": 4096}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        return {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}

    def _post(self, payload: dict) -> tuple[int, dict]:
        url = f"{BASE_URL}/models/{self.model}:generateContent"
        last = "unknown error"
        for attempt in range(self._max_retries):
            try:
                r = self._http.post(url, json=payload, headers={"x-goog-api-key": self._key, "Content-Type": "application/json"})
            except httpx.TransportError as exc:
                last = f"network error ({type(exc).__name__})"
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                else:
                    try:
                        return r.status_code, r.json()
                    except ValueError:
                        raise LLMError(f"HTTP {r.status_code}: response was not JSON") from None
            if attempt < self._max_retries - 1:
                self._sleep(min(2 ** (attempt + 1), 20))
        raise LLMError(f"Gemini unavailable after {self._max_retries} attempts: {last}")

    def generate_json(self, system: str, prompt: str) -> dict:
        status, data = self._post(self._payload(system, prompt, json_mode=True))
        if status == 400:  # some models reject parts of generationConfig; retry once with the bare minimum
            logger.warning("Gemini rejected JSON mode ({}); retrying without it", str(data.get("error", {}).get("message", ""))[:120])
            status, data = self._post(self._payload(system, prompt, json_mode=False))
        if status != 200:
            msg = str((data.get("error") or {}).get("message", ""))[:200]
            raise LLMError(f"HTTP {status}: {msg}" if msg else f"HTTP {status}")
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMError(f"request blocked by Gemini ({block})")
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("Gemini returned no answer")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        try:
            return parse_json_object(text)
        except ValueError:
            why = " (answer was cut off)" if cands[0].get("finishReason") == "MAX_TOKENS" else ""
            raise LLMError("Gemini did not return valid JSON" + why) from None


def parse_json_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


def client_from_env() -> GeminiClient | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return GeminiClient(key, os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL) if key else None
