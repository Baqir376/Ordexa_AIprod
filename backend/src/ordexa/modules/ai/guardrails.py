"""Guardrails around the AI order reader.

The model is treated as an untrusted proposal generator. These deterministic checks run on every extraction,
whatever the provider (Claude, Groq, rules):

1. Input hardening:   strip invisible characters, neutralise our prompt delimiters, cap length.
2. Injection detection: English + Roman Urdu attempts to instruct the system (discounts, approvals, credit,
   "ignore your rules", role play, links). Flagged for a person, never obeyed. The model has no tools anyway.
3. Grounding (anti-hallucination): every extracted item must be traceable to words in the shop's message, and
   every quantity must appear in it (digits or number words). Otherwise the line is blocked for review.
4. Sanity limits: absurd or fractional quantities, too many lines.
5. Safe echo: text repeated back to the shop over WhatsApp is stripped of links and capped.

Business facts (product identity, price, stock, credit) never come from the model; see ``service.evaluate``.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from ordexa.modules.ai.extract import NUMBER_WORDS, URDU_DIGITS, Extraction, normalise_phrase

MAX_INPUT_CHARS = 2000
MAX_LINES = 40
MAX_QTY = 1000  # per line; larger needs a person to confirm
COUNT_UNITS = {"box", "strip", "carton", "bottle", "pack", "piece", "tube", "sachet", "vial", "tablet", "dozen", None}

# Line flags that block automatic "ready to approve" until a person (or the shop's answer) confirms.
BLOCKING_FLAGS = {"not_in_message", "quantity_not_in_message", "unusual_quantity", "fractional_quantity"}

_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_DELIMS = re.compile(r"</?\s*(order_message|system|assistant|user|instructions?)\s*>", re.IGNORECASE)

# (category, pattern). Roman Urdu spellings vary a lot, so patterns are loose on vowels.
INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "override",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(rules?|instructions?|prompt|above|previous|system|policy)",
            re.I,
        ),
    ),
    (
        "override",
        re.compile(
            r"\b(system\s*prompt|developer\s*mode|jailbreak|you\s+are\s+now|act\s+as|pretend\s+to\s+be|new\s+instructions?)\b",
            re.I,
        ),
    ),
    (
        "approval",
        re.compile(
            r"\b(auto[\s-]?approve|approve\s+(it|this|order|kar)|approved\s+kar|confirm\s+kar\s+do\s+bina|without\s+(approval|review|checking))\b",
            re.I,
        ),
    ),
    (
        "pricing",
        re.compile(
            r"\b(discount|rate\s*kam|kam\s*rate|price\s*(kam|change|0|zero)|free\s+(of\s+cost|me|mein|ma)|muft|bonus\s+do|50\s*%|100\s*%|rs\.?\s*0\b)",
            re.I,
        ),
    ),
    (
        "credit",
        re.compile(
            r"\b(credit\s*limit|udhaar\s+badha|limit\s+(barha|barhao|increase)|balance\s+(zero|clear|khatam)|payment\s+(skip|mat))",
            re.I,
        ),
    ),
    (
        "admin",
        re.compile(r"\b(admin|password|token|api\s*key|database|sql|delete|drop\s+table|cancel\s+all|sab\s+cancel)\b", re.I),
    ),
    (
        "identity",
        re.compile(
            r"\b(i\s+am\s+(the\s+)?(owner|admin|manager)|main\s+(owner|malik|manager)\s+hoon|on\s+behalf\s+of\s+(another|other)\s+shop|dusre\s+shop)\b",
            re.I,
        ),
    ),
    (
        "pricing",
        re.compile(
            r"(rs\.?|rupees?)\s*\d+\s*(wala|wali|ka|ki|per|mein|me)?\s*(rate|price|qeemat)|rate\s+(lagana|lagao|laga\s+do|de\s+do|kar\s+do)|(apna|meri|mera)\s+rate",
            re.I,
        ),
    ),
    ("override", re.compile(r"(^|\s)(system|assistant|developer)\s*:", re.I)),
    ("payload", re.compile(r'[\{\[]\s*"\w+"\s*:', re.I)),  # pasted JSON pretending to be model output
    ("link", re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|pk|io|xyz|ly)\b)", re.I)),
]


def sanitize_input(text: str) -> str:
    """Normalise the shop's text before any model sees it (the raw text is still stored unchanged)."""
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZERO_WIDTH.sub("", text)
    text = _DELIMS.sub(" ", text)  # cannot close our <order_message> data block or fake a role
    text = re.sub(r"[ \t]+", " ", text)
    return text[:MAX_INPUT_CHARS].strip()


def detect_injection(text: str) -> list[str]:
    found: list[str] = []
    for category, pat in INJECTION_PATTERNS:
        if pat.search(text or "") and category not in found:
            found.append(category)
    if re.search(r"[A-Za-z0-9+/]{120,}={0,2}", text or ""):  # long encoded blobs
        found.append("encoded")
    return found


def _source_tokens(source: str) -> list[str]:
    return [t for t in re.split(r"[\s,.;:()/\-+&|]+", normalise_phrase(source.translate(URDU_DIGITS))) if t]


def _token_in(tok: str, source_tokens: list[str]) -> bool:
    for s in source_tokens:
        if tok == s or (len(tok) >= 4 and (s.startswith(tok) or tok.startswith(s) and len(s) >= 4)):
            return True
        if len(tok) >= 4 and SequenceMatcher(None, tok, s).ratio() >= 0.75:
            return True
    return False


def _has_non_latin_letters(text: str) -> bool:
    return any(ch.isalpha() and not ("a" <= ch.lower() <= "z") for ch in text)


def _quantity_in_source(q: float, source: str, source_tokens: list[str]) -> bool:
    nums = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", source.translate(URDU_DIGITS))}
    if q in nums:
        return True
    words = {float(v) for t, v in NUMBER_WORDS.items() if t in source_tokens}
    if q in words:
        return True
    # "a dozen" = 12, "aik/ek/one" handled above; English number words beyond the table:
    return False


def check(extraction: Extraction, source: str | None, *, from_photo: bool = False) -> list[dict]:
    """Flag each line in place (``line.flags``) and return order-level warnings."""
    warnings: list[dict] = []
    if len(extraction.lines) > MAX_LINES:
        extraction.lines = extraction.lines[:MAX_LINES]
        warnings.append({"code": "too_many_lines", "message": f"Only the first {MAX_LINES} items were read."})
    src = sanitize_input(source or "")
    src_tokens = _source_tokens(src)
    verifiable = bool(src) and not from_photo and not _has_non_latin_letters(src)
    for line in extraction.lines:
        flags: list[str] = []
        phrase_tokens = [t for t in re.split(r"[\s\-/]+", normalise_phrase(line.phrase)) if len(t) >= 3 and not t.isdigit()]
        if verifiable and phrase_tokens and not any(_token_in(t, src_tokens) for t in phrase_tokens):
            flags.append("not_in_message")  # the model produced an item the shop never wrote
        if line.quantity is not None:
            if line.quantity > MAX_QTY:
                flags.append("unusual_quantity")
            if line.unit in COUNT_UNITS and line.quantity != int(line.quantity):
                flags.append("fractional_quantity")
            if verifiable and not _quantity_in_source(float(line.quantity), src, src_tokens):
                flags.append("quantity_not_in_message")
        line.flags = flags
    if from_photo and extraction.lines:
        warnings.append(
            {"code": "photo_check", "message": "Items were read from a photo: compare them with the picture before approving."}
        )
    elif src and not verifiable and extraction.lines:
        warnings.append(
            {"code": "script_check", "message": "Message is in Urdu script: check the items against it before approving."}
        )
    return warnings


def safe_echo(text: str, limit: int = 40) -> str:
    """Shop text repeated in our WhatsApp replies: no links, no control characters, short."""
    text = re.sub(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(com|net|org|pk|io|xyz|ly)\b\S*)", "[link]", text or "", flags=re.I)
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit].strip() + ("…" if len(text) > limit else "")
