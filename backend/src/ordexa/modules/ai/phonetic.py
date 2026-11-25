"""Cross-script "sounds like" matching for voice notes.

Whisper writes the same spoken medicine name in Urdu script (رائزیک), Hindi script (राइज़ेक) or a near-miss English
spelling ("Raizet"). This module reads all three into rough Latin letters, reduces them to a consonant outline and
finds catalogue products that sound alike, so the order reader gets "the shop probably said one of: Risek 20mg ..."
instead of guessing. Pure functions, standard library only.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_URDU = {
    "ا": "a", "آ": "a", "أ": "a", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h",
    "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s",
    "ض": "z", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "k", "ک": "k", "ك": "k", "گ": "g", "ل": "l",
    "م": "m", "ن": "n", "ں": "n", "و": "o", "ؤ": "o", "ہ": "h", "ۃ": "h", "ھ": "h", "ء": "", "ی": "i", "ي": "i",
    "ئ": "i", "ے": "e", "ۓ": "e", "ٰ": "", "ً": "", "َ": "a", "ُ": "u", "ِ": "i", "ّ": "", "ْ": "",
}  # fmt: skip
_DEVA = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o",
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ट": "t", "ठ": "th", "ड": "d",
    "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "f", "ब": "b", "भ": "bh",
    "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ं": "n",
    "ँ": "n", "्": "", "़": "",
}  # fmt: skip
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩०१२३४५६७८९", "012345678901234567890123456789")
_SPLIT = re.compile(r"[\s,.!?;:\-،۔।'\"()/]+")

# Words that are never a product name: units, fillers, verbs, number words (Latin, Urdu and Hindi script).
STOP = set(
    """and of the a an to my me i please send need add by last order with hello hi bhai jaan ji aur ke ki ka ko
wali wale wala wally vali vale walay
se do dein de kar karo box boxes bottle bottles strip strips carton cartons tube tubes piece pieces pack
packs syrup gel tablet tablets tab tabs cap caps capsule capsules dabba dabbe dabbi dubbi patte patta botal bottal
chahiye bhej bhejo bhejna urgent jaldi aaj sham tak mg ml wali assalam alaikum assalamu salam or
one two three four five six seven eight nine ten eleven twelve fifteen twenty thirty forty fifty hundred
aik ek teen char chaar panch paanch chhe saat aath nau das barah pandrah bees tees chalis pachas sau
اور کے کی کا کو والی والے والا سے بھائی جان ڈبے ڈبہ ڈبی پتے پتہ بوتل بوتلیں کارٹن ٹیوب ٹیوبیں پیس سیرپ جیل گولیاں
گولی بھیج بھیجیں دیں دو کر مجھے دبے دبہ اسلام چاہیے السلام علیکم آج شام تک جلدی بس اس میں
ایک تین چار پانچ چھ چھے سات آٹھ نو دس بارہ پندرہ بیس پچیس تیس چالیس پچاس سو
चाहिए और के की का डिब्बे डिब्बा पत्ते बोतल ट्यूब भेज भेजना दो सिरप भाई मुझे
एक तीन चार पांच पाँच छह सात आठ नौ दस बारह पंद्रह बीस तीस चालीस पचास सौ""".split()
)


def to_latin(text: str) -> str:
    """Urdu or Hindi script -> rough Latin letters (رائزیک -> raizik, राइज़ेक -> raizek); Latin passes through."""
    t = (text or "").translate(_DIGITS).replace("ज़", "z").replace("फ़", "f").replace("ड़", "r")
    out = []
    for i, c in enumerate(t):
        out.append("v" if c == "و" and (i == 0 or t[i - 1] in "اآ") else _URDU.get(c, _DEVA.get(c, c)))
        # Hindi consonants carry an "a" unless a vowel sign or virama follows, or the word ends (दस -> das)
        if "क" <= c <= "ह" and i + 1 < len(t) and "क" <= t[i + 1] <= "ह":
            out.append("a")
    return "".join(out).lower()


def skeleton(text: str) -> str:
    """Consonant outline (klaricid / کلیریسڈ / Claracid -> klrsd)."""
    t = re.sub(r"c(?=[eiy])", "s", text.lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("x", "ks"), ("c", "k"), ("q", "k"), ("w", "v"), ("y", "i"), ("ee", "i"), ("z", "s")):
        t = t.replace(a, b)
    out: list[str] = []
    for ch in t:
        if ch.isalpha() and ch not in "aeiouh" and (not out or out[-1] != ch):
            out.append(ch)
    return "".join(out)


_COARSE = str.maketrans("bdgjvmz", "ptkkfns")


def coarse(skel: str) -> str:
    """Sound classes noisy audio confuses: b/p, d/t, g/j/k, v/f, m/n; "v" may also be heard as a vowel (و)."""
    out: list[str] = []
    for ch in skel.translate(_COARSE).replace("f", ""):
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def words(text: str) -> list[str]:
    """Content words of a transcript (fillers, units and number words removed)."""
    return [w for w in _SPLIT.split((text or "").translate(_DIGITS)) if w and w.lower() not in STOP and not w.isdigit()]


def similarity(heard: str, brand: str) -> float:
    """0..1: how much a heard word (any script) sounds like a brand name."""
    w, b = to_latin(heard), brand.lower()
    sw, sb = skeleton(w), skeleton(b)
    if len(sb) <= 2:  # ORS, CAC ...: too short to guess; only a near-exact hearing counts
        return 1.0 if sw == sb and abs(len(w) - len(b)) <= 1 else 0.0
    best = SequenceMatcher(None, w, b).ratio()
    # Same first consonant, or both start with the same vowel (آرمنٹن ~ augmentin: the first consonant is often misheard)
    same_start = bool(sw) and (sw[0] == sb[0] or (w[:1] == b[:1] and w[:1] in "aeiou"))
    if same_start and abs(len(sw) - len(sb)) <= 1:
        start = 1.0 if w[:1] == b[:1] else 0.9  # amoxil / mixel share an outline but not a first sound
        best = max(best, SequenceMatcher(None, sw, sb).ratio() * (1.0 if len(sb) >= 4 else 0.9) * start)
        if len(sw) == len(sb) >= 3 and sw[:-1] == sb[:-1]:
            best = max(best, 0.76)  # only the last consonant differs: Raizet ~ Risek
    cw, cb = coarse(sw), coarse(sb)
    if len(cb) >= 3 and cw and cw[0] == cb[0] and abs(len(cw) - len(cb)) <= 1 and SequenceMatcher(None, w, b).ratio() >= 0.45:
        best = max(best, SequenceMatcher(None, cw, cb).ratio() * (0.88 if len(cb) >= 4 else 0.8))
    return best


def brand_of(product_name: str) -> str:
    """The brand word of a catalogue name: "KLARICID 500MG TAB" -> "klaricid"."""
    for tok in re.split(r"[\s\-/,()]+", product_name.lower()):
        if len(tok) >= 3 and tok.isalpha():
            return tok
    return product_name.lower().split()[0] if product_name.strip() else ""


def candidates(
    texts: list[str], product_names: list[str], *, threshold: float = 0.72, per_word: int = 3, limit: int = 20
) -> list[dict]:
    """Catalogue products that sound like something said in any of the transcripts.

    Returns [{"heard": word(s), "brand": brand, "products": [full names], "score": 0..1}], best first. One- and
    two-word windows are tried (Urdu often splits a name: سیف اسپین = Cefspan)."""
    brands: dict[str, list[str]] = {}
    for name in product_names:
        b = brand_of(name)
        if len(b) >= 3:
            brands.setdefault(b, []).append(name)
    skel = {b: skeleton(b) for b in brands}
    found: dict[str, dict] = {}
    for text in texts:
        ws = words(text)
        for n in (1, 2):
            for i in range(len(ws) - n + 1):
                heard = " ".join(ws[i : i + n])
                lat = to_latin("".join(ws[i : i + n]))
                sh = skeleton(lat)
                if len(sh) < 2:
                    continue
                scored = []
                for b, sb in skel.items():
                    if not sb or (coarse(sb)[:1] != coarse(sh)[:1] and lat[:1] != b[:1]):
                        continue
                    if abs(len(sb) - len(sh)) > 2:
                        continue
                    s = similarity(lat, b)
                    if s >= threshold:
                        scored.append((s, b))
                scored.sort(reverse=True)
                if scored and scored[0][0] >= 0.95:  # heard clearly: weaker look-alikes of the same word are noise
                    scored = [x for x in scored if x[0] >= 0.95]
                for s, b in scored[:per_word]:
                    if b not in found or found[b]["score"] < s:
                        found[b] = {"heard": heard, "brand": b, "products": brands[b][:4], "score": round(s, 3)}
    return sorted(found.values(), key=lambda d: -d["score"])[:limit]


def alike(heard_phrase: str, product_name: str, threshold: float = 0.7) -> bool:
    """Guard on a correction the order reader proposes for a voice note: some heard word (or two adjacent words) must
    sound like the product's brand. Filler words never count. Stops the model swapping in an unrelated medicine."""
    brand = brand_of(product_name)
    ws = words(heard_phrase)
    for n in (1, 2):
        for i in range(len(ws) - n + 1):
            joined = "".join(ws[i : i + n])
            if len(skeleton(to_latin(joined))) >= 2 and similarity(joined, brand) >= threshold:
                return True
    return False
