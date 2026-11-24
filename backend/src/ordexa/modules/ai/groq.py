"""Groq: the free-tier AI provider (no credit card needed; rate-limited).

* Order text  -> chat model with strict JSON-schema output (default ``openai/gpt-oss-120b``)
* Order photo -> vision model, same schema (default ``qwen/qwen3.8-27b``, a preview model)
* Voice note  -> Whisper speech-to-text (default ``whisper-large-v3``), then read like a text order

Uses Groq's OpenAI-compatible HTTP API through the standard library (no extra dependency). Any failure (rate
limit, outage, retired model) raises; callers fall back to the rule parser or leave the order for staff.
Model IDs change over time: override with ORDEXA_GROQ_MODEL / ORDEXA_GROQ_VISION_MODEL / ORDEXA_GROQ_STT_MODEL.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
import uuid

from pydantic import ValidationError

from ordexa.config import get_settings

API = "https://api.groq.com/openai/v1"
STT_PROMPT = (
    "Pakistani pharmacy order in Urdu, Roman Urdu or English. Product names like Panadol, Brufen, Calpol, Augmentin, "
    "Risek. Units like box, dabba, patta, strip, carton, botal. Numbers like 10, das, paanch."
)


MAX_RETRY_WAIT = 10.0  # seconds


def _retry_after(value: str | None) -> float | None:
    try:
        return max(float(value), 0.5) if value is not None else None
    except ValueError:
        return None


def configured() -> bool:
    """A Groq key is set and AI is not switched off (ORDEXA_AI_PROVIDER=rules)."""
    s = get_settings()
    return bool(s.groq_api_key) and s.ai_provider != "rules"


def _headers(content_type: str) -> dict:
    return {"Authorization": f"Bearer {get_settings().groq_api_key}", "Content-Type": content_type, "User-Agent": "ordexa-erp"}


def _post(path: str, body: bytes, content_type: str, timeout: int = 60, _retry: bool = True) -> dict:
    req = urllib.request.Request(f"{API}{path}", data=body, method="POST", headers=_headers(content_type))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https API base
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 429:
            # Free tier: per-minute token limits. Wait once if Groq says it is short, else let callers fall back.
            wait = _retry_after(e.headers.get("retry-after"))
            if _retry and wait is not None and wait <= MAX_RETRY_WAIT:
                time.sleep(wait)
                return _post(path, body, content_type, timeout, _retry=False)
            raise RuntimeError(f"Groq free-tier rate limit reached; try again shortly ({detail})") from e
        raise RuntimeError(f"Groq API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Groq unreachable: {e.reason}") from e


class GroqExtractor:
    def __init__(self):
        s = get_settings()
        self.text_model = s.groq_model
        self.vision_model = s.groq_vision_model
        self.name = f"groq:{self.text_model}"

    def extract(self, text: str, image: tuple[bytes, str] | None = None):
        from ordexa.modules.ai.extract import EXTRACTION_SCHEMA, SYSTEM_PROMPT, Extraction, ExtractResult

        user: list[dict] | str
        model = self.text_model
        if image is not None:
            data, media_type = image
            model = self.vision_model
            user = [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{base64.b64encode(data).decode()}"}},
                {"type": "text", "text": f"<order_message>\n{text}\n</order_message>"},
            ]
        else:
            user = f"<order_message>\n{text}\n</order_message>"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "order_extraction", "strict": True, "schema": EXTRACTION_SCHEMA},
            },
            "temperature": 0,
            "max_completion_tokens": 4000,
        }
        res = _post("/chat/completions", json.dumps(body).encode(), "application/json")
        choice = res["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("model output was cut off")
        try:
            extraction = Extraction.model_validate(json.loads(choice["message"]["content"] or ""))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise RuntimeError(f"model output failed schema validation: {exc}") from exc
        usage = res.get("usage") or {}
        name = f"groq:{model}"
        return ExtractResult(
            extraction,
            name,
            [
                {
                    "step": "extract",
                    "extractor": name,
                    "lines": len(extraction.lines),
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                }
            ],
        )


def transcribe(data: bytes, media_type: str) -> str:
    """Speech-to-text with Whisper. Language is auto-detected (shops mix Urdu and English); the prompt biases it
    towards medicine names and Roman Urdu units."""
    s = get_settings()
    if len(data) > 25 * 1024 * 1024:
        raise RuntimeError("voice note is larger than the 25 MB free-tier limit")
    ext = {
        "audio/ogg": "ogg",
        "audio/opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/aac": "m4a",
        "audio/amr": "amr",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/webm": "webm",
    }.get(media_type, "ogg")
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("model", s.groq_stt_model), ("response_format", "json"), ("temperature", "0"), ("prompt", STT_PROMPT)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.{ext}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    res = _post("/audio/transcriptions", b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=120)
    return (res.get("text") or "").strip()


def chat_json(system: str, user: str, schema: dict, name: str) -> dict:
    """One strict-JSON-schema chat call on the text model (used for follow-up messages about an open order)."""
    body = {
        "model": get_settings().groq_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        "temperature": 0,
        "max_completion_tokens": 2000,
    }
    res = _post("/chat/completions", json.dumps(body).encode(), "application/json")
    choice = res["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("model output was cut off")
    return json.loads(choice["message"]["content"] or "")
