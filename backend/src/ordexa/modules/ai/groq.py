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


class RateLimited(RuntimeError):
    """Free-tier per-minute limit: worth retrying in a minute (orders are read in the background anyway)."""


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
            raise RateLimited(f"Groq free-tier rate limit reached; try again shortly ({detail})") from e
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
        res = _chat(body)
        model = res.get("model") or model
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


AUDIO_EXT = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "m4a", "audio/amr": "amr",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "audio/flac": "flac",
}  # fmt: skip


def transcribe(data: bytes, media_type: str) -> str:
    """Speech-to-text with Whisper (language auto-detected, generic pharmacy prompt). Voice orders use
    ``voice.hear()``, which adds audio clean-up, a catalogue prompt and a second hearing."""
    return transcribe_detailed(data, media_type, prompt=STT_PROMPT)["text"]


def transcribe_detailed(
    data: bytes, media_type: str, *, prompt: str, language: str | None = None, model: str | None = None
) -> dict:
    """Whisper with segment details: {"text", "language", "confidence" (0..1), "no_speech" (0..1)}.
    ``language`` (ISO code, e.g. "ur") forces the language instead of auto-detection."""
    import math

    s = get_settings()
    if len(data) > 25 * 1024 * 1024:
        raise RuntimeError("voice note is larger than the 25 MB free-tier limit")
    ext = AUDIO_EXT.get(media_type, "ogg")
    boundary = uuid.uuid4().hex
    fields = {"model": model or s.groq_stt_model, "response_format": "verbose_json", "temperature": "0", "prompt": prompt}
    if language:
        fields["language"] = language
    crlf = chr(13) + chr(10)  # multipart line break
    disp = "Content-Disposition: form-data; name="
    parts = [f'--{boundary}{crlf}{disp}"{k}"{crlf}{crlf}{v}{crlf}'.encode() for k, v in fields.items()]
    head = f'--{boundary}{crlf}Content-Disposition: form-data; name="file"; filename="voice.{ext}"{crlf}'
    parts.append(f"{head}Content-Type: {media_type}{crlf}{crlf}".encode() + data + crlf.encode())
    parts.append(f"--{boundary}--{crlf}".encode())
    res = _post("/audio/transcriptions", b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=120)
    segs = res.get("segments") or []
    # Average per-token probability over the note (Whisper's avg_logprob, weighted by segment length).
    total = sum(max(1, len(seg.get("text", ""))) for seg in segs)
    logp = sum(seg.get("avg_logprob", 0.0) * max(1, len(seg.get("text", ""))) for seg in segs) / total if segs else -1.0
    return {
        "text": (res.get("text") or "").strip(),
        "language": (res.get("language") or "").lower() or None,
        "confidence": round(math.exp(min(0.0, logp)), 3),
        "no_speech": round(max([seg.get("no_speech_prob", 0.0) for seg in segs] or [0.0]), 3),
    }


def chat_json(system: str, user: str, schema: dict, name: str) -> dict:
    """One strict-JSON-schema chat call on the text model (used for follow-up messages about an open order)."""
    body = {
        "model": get_settings().groq_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        "temperature": 0,
        "max_completion_tokens": 2000,
    }
    res = _chat(body)
    choice = res["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("model output was cut off")
    return json.loads(choice["message"]["content"] or "")


def _chat(body: dict) -> dict:
    """Chat call on the main text model; when that model's free-tier limit is reached, the same request goes once
    to the fallback model (its own quota) before callers fall back to rules or retry later."""
    try:
        return _post("/chat/completions", json.dumps(body).encode(), "application/json")
    except RateLimited:
        fallback = get_settings().groq_fallback_model
        if not fallback or body["model"] == fallback or body["model"] == get_settings().groq_vision_model:
            raise
        return _post("/chat/completions", json.dumps({**body, "model": fallback}).encode(), "application/json")
