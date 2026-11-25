"""Voice notes -> text an order reader can trust.

Measured on a 102-clip benchmark of Urdu, Roman-Urdu/code-mixed and English medicine orders under clean, noisy and
harsh WhatsApp conditions (see backend/evals/voice/). What helped, in order of effect:

1. Catalogue prompt: Whisper is told the business's own product names, so "Klaricid" is not written "Claracid".
2. Clean-up before recognition: high-pass (hum, rumble), FFT noise reduction and level normalisation (+3.5 points
   overall, +11.6 on Urdu speech; RNNoise was worse on harsh audio).
3. Sound-alike hints for the order reader: catalogue products whose names sound like any heard word in any script
   (phonetic.py), so "Raizet" or "رائزیک" becomes RISEK. This more than doubled end-to-end item accuracy.
4. Script/language repair: Whisper's language detection is unreliable on noisy Urdu. Hindi-script output is turned
   into Latin letters (the words are usually right, only the script is wrong); a wrong language (Slovenian, Hebrew,
   Persian ...) is heard again with the language fixed to Urdu. Showing the reader two hearings every time was
   measured and did not help (it confused noisy notes), so the other hearing is kept for staff only.

Fine-tuned Urdu models from Hugging Face (whisper-large-v3-urdu, whisper-small-roman-urdu, wav2vec2-xls-r-urdu) were
benchmarked locally and scored far lower on this task (English and brand names break them), so Groq's
whisper-large-v3 stays the recogniser (free tier: 2,000 requests/day).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ordexa.modules.ai import groq, phonetic

log = logging.getLogger(__name__)

# ffmpeg filter chain: cut rumble/hum below 90 Hz and hiss above 7.6 kHz, FFT denoise tracking the noise floor,
# then even out a voice that fades in and out.
CLEAN_FILTER = "highpass=f=90,lowpass=f=7600,afftdn=nr=14:nf=-35:tn=1,dynaudnorm=f=150:g=11"
PROMPT_CHARS = 600  # Whisper reads at most 224 prompt tokens
LANG_CODES = {"urdu": "ur", "english": "en", "hindi": "hi", "ur": "ur", "en": "en", "hi": "hi"}
CLEAR, UNCLEAR = 0.62, 0.45  # confidence bands (exp of Whisper's average log-probability)


@dataclass
class Hearing:
    text: str  # the transcript the order is read from (shown to staff)
    alt: str | None = None  # the other hearing, when it says something different
    language: str | None = None  # ur | en | hi | other
    confidence: float = 0.0  # 0..1
    no_speech: float = 0.0
    hints: list[dict] = field(default_factory=list)
    passes: list[dict] = field(default_factory=list)
    cleaned: bool = False
    error: str | None = None

    @property
    def clarity(self) -> str:
        if not self.text:
            return "none"
        if self.no_speech > 0.6 or self.confidence < UNCLEAR:
            return "unclear"
        return "clear" if self.confidence >= CLEAR else "fair"

    def reader_text(self) -> str:
        """What the order reader (LLM) gets: both hearings and the catalogue sound-alikes."""
        parts = ["[Voice note: speech recognition output, may contain mistakes]", f"Heard: {self.text}"]
        if self.hints:
            lines = [f'- "{h["heard"]}" sounds like: {" | ".join(h["products"][:3])}' for h in self.hints]
            parts.append("Catalogue products that sound like words in the note:\n" + "\n".join(lines))
        return "\n".join(parts)

    def source_text(self) -> str:
        """What the shop said, for guardrails (never the hints)."""
        return self.text

    def trace(self) -> dict:
        return {
            "step": "speech_to_text",
            "provider": "groq",
            "text": self.text,
            "alt": self.alt,
            "language": self.language,
            "confidence": self.confidence,
            "clarity": self.clarity,
            "cleaned": self.cleaned,
            "hints": [{"heard": h["heard"], "products": h["products"][:3], "score": h["score"]} for h in self.hints[:12]],
            "passes": self.passes,
            **({"error": self.error} if self.error else {}),
        }


def clean_audio(data: bytes, media_type: str) -> tuple[bytes, str, bool]:
    """Decode any WhatsApp audio, mono 16 kHz, cleaned, as FLAC. Returns the original when ffmpeg is unavailable or
    fails (recognition still works, just without the clean-up)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return data, media_type, False
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-i", "pipe:0", "-af", CLEAN_FILTER, "-ac", "1", "-ar", "16000",
             "-c:a", "flac", "-f", "flac", "pipe:1"],
            input=data, capture_output=True, timeout=60, check=True,
        ).stdout  # fmt: skip
        if len(out) > 1000:
            return out, "audio/flac", True
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("audio clean-up failed, using the original: %s", exc)
    return data, media_type, False


def product_names(session: Session, tenant_id) -> list[str]:
    from ordexa.modules.catalogue.models import Product

    return list(session.scalars(select(Product.name).where(Product.tenant_id == tenant_id, Product.is_active.is_(True))))


def catalogue_prompt(session: Session, tenant_id, names: list[str] | None = None) -> str:
    """Whisper prompt with this business's brands: most sold in the last 90 days first, then the rest A-Z."""
    from ordexa.modules.catalogue.models import Product
    from ordexa.modules.sales.models import Invoice, InvoiceLine

    since = datetime.now(UTC) - timedelta(days=90)
    top = session.execute(
        select(Product.name, func.count())
        .join(InvoiceLine, InvoiceLine.product_id == Product.id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Product.tenant_id == tenant_id, Invoice.issued_at >= since)
        .group_by(Product.name)
        .order_by(func.count().desc())
        .limit(80)
    ).all()
    ordered = [n for n, _ in top] + sorted(names if names is not None else product_names(session, tenant_id))
    brands: list[str] = []
    for n in ordered:
        b = phonetic.brand_of(n)
        if len(b) >= 4 and b not in brands:
            brands.append(b)
    head = "Medicine order. Products: "
    tail = ". Units: box, dabba, patta, strip, carton, botal."
    body = ""
    for b in brands:
        piece = (", " if body else "") + b.title()
        if len(head) + len(body) + len(piece) + len(tail) > PROMPT_CHARS:
            break
        body += piece
    return head + body + tail if body else groq.STT_PROMPT


def _script(text: str) -> str:
    if any("ऀ" <= c <= "ॿ" for c in text):
        return "devanagari"
    if any("؀" <= c <= "ۿ" for c in text):
        return "urdu"
    if any(c.isalpha() and not c.isascii() for c in text):
        return "foreign"  # Hebrew, Cyrillic, accented Latin ... = a misdetected language
    return "latin"


def _wrong_language(p: dict) -> bool:
    """Whisper guessed a language nobody here speaks (or wrote foreign letters): hear it again as Urdu."""
    lang = LANG_CODES.get(p.get("language") or "", "other")
    return bool(p["text"]) and (lang not in ("ur", "en", "hi") or _script(p["text"]) == "foreign")


def hear(session: Session, tenant_id, data: bytes, media_type: str) -> Hearing:
    """Recognise a voice note. Raises groq.RateLimited (the AI worker retries later)."""
    names = product_names(session, tenant_id)
    prompt = catalogue_prompt(session, tenant_id, names)
    audio, audio_type, cleaned = clean_audio(data, media_type)
    first = groq.transcribe_detailed(audio, audio_type, prompt=prompt)
    passes = [{"mode": "auto", **first}]
    main, alt = first, None
    if _wrong_language(first):
        try:
            second = groq.transcribe_detailed(audio, audio_type, prompt=prompt, language="ur")
            passes.append({"mode": "urdu", **second})
            if second["text"]:
                main, alt = second, first["text"]
        except groq.RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001 - the first hearing is still useful
            passes.append({"mode": "urdu", "error": str(exc)[:200]})
    text = main["text"]
    if _script(text) == "devanagari":
        alt, text = text, phonetic.to_latin(text)  # right words, wrong script: read it in Latin letters
    lang = "ur" if main is not first else LANG_CODES.get(main.get("language") or "", "other")
    h = Hearing(text=text, alt=alt, language=lang, confidence=main.get("confidence", 0.0),
                no_speech=main.get("no_speech", 0.0), passes=passes, cleaned=cleaned)  # fmt: skip
    h.hints = phonetic.candidates([h.text], names)
    return h
