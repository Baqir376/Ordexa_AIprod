"""Scores transcripts on what an order needs: each medicine name heard (any script) and its quantity present.
Brand matching uses the same consonant-skeleton idea as the app's sounds_alike()."""

import json
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).parent

URDU = {
    "ا": "a", "آ": "a", "أ": "a", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h",
    "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s",
    "ض": "z", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "k", "ک": "k", "ك": "k", "گ": "g", "ل": "l",
    "م": "m", "ن": "n", "ں": "n", "و": "o", "ؤ": "o", "ہ": "h", "ۃ": "h", "ھ": "h", "ء": "", "ی": "i", "ي": "i",
    "ئ": "i", "ے": "e", "ۓ": "e", "ٰ": "", "ً": "", "َ": "a", "ُ": "u", "ِ": "i", "ّ": "", "ْ": "",
}  # fmt: skip
DEVA = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o",
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ट": "t", "ठ": "th", "ड": "d",
    "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "f", "ब": "b", "भ": "bh",
    "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ज़": "z", "फ़": "f",
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ं": "n",
    "ँ": "n", "्": "", "़": "", "ड़": "r",
}  # fmt: skip
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩०१२३४५६७८९", "012345678901234567890123456789")
NUM = {
    # English
    "one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    # Roman Urdu / Hindi
    "aik": 1, "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "chaar": 4, "panch": 5, "paanch": 5, "pach": 5,
    "chhe": 6, "che": 6, "chay": 6, "chh": 6, "saat": 7, "sat": 7, "aath": 8, "aat": 8, "nau": 9, "no": 9, "das": 10,
    "barah": 12, "bara": 12, "baarah": 12, "pandrah": 15, "pandra": 15, "bees": 20, "bis": 20, "pachees": 25, "tees": 30,
    "tis": 30, "chalis": 40, "chaalis": 40, "pachas": 50, "pachaas": 50, "sau": 100, "so": 100,
    # Urdu script
    "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5, "چھ": 6, "چھے": 6, "سات": 7, "آٹھ": 8, "نو": 9, "دس": 10,
    "بارہ": 12, "پندرہ": 15, "بیس": 20, "پچیس": 25, "تیس": 30, "چالیس": 40, "پچاس": 50, "سو": 100,
    # Devanagari
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
    "बारह": 12, "पंद्रह": 15, "बीस": 20, "पच्चीस": 25, "तीस": 30, "चालीस": 40, "पचास": 50, "सौ": 100,
}  # fmt: skip


def script(t: str) -> str:
    if any("ऀ" <= c <= "ॿ" for c in t):
        return "devanagari"
    if any("؀" <= c <= "ۿ" for c in t):
        return "urdu"
    return "latin"


def to_latin(t: str) -> str:
    t = t.translate(DIGITS).replace("ज़", "z").replace("फ़", "f").replace("ड़", "r")
    out = []
    for i, c in enumerate(t):
        out.append("v" if c == "و" and (i == 0 or t[i - 1] in "اآ") else URDU.get(c, DEVA.get(c, c)))  # و starting a syllable is v
    return "".join(out).lower()


STOP = set("""and of the a an to my me i please send need add by this last order with bhai jaan ji aur ke ki ka ko wali wale wala se
hi do dein de kar box boxes bottle bottles strip strips carton cartons tube tubes piece pieces syrup gel tablet tablets
dabba dabbe dabbi patte patta botal bottal chahiye bhej bhejo bhejna urgent jaldi aaj sham tak
اور کے کی کا کو والی والے والا سے بھائی جان ڈبے ڈبہ ڈبی پتے بوتل بوتلیں کارٹن ٹیوبیں پیس سیرپ جیل گولیاں بھیج دیں دو
चाहिए और के की का डिब्बे पत्ते बोतल ट्यूब भेज भेजना दो सिरप""".split())


def skeleton(t: str) -> str:
    t = t.lower()
    t = re.sub(r"c(?=[eiy])", "s", t)
    for a, b in (("ph", "f"), ("ck", "k"), ("x", "ks"), ("c", "k"), ("q", "k"), ("w", "v"), ("y", "i"), ("ee", "i"), ("z", "s")):
        t = t.replace(a, b)
    out = []
    for ch in t:
        if ch.isalpha() and ch not in "aeiouh" and (not out or out[-1] != ch):
            out.append(ch)
    return "".join(out)


def tokens(t: str) -> list[str]:
    return [w for w in re.split(r"[\s,.!?;:\-،۔।'\"()]+", t.translate(DIGITS)) if w]


def brand_heard(transcript: str, brand: str) -> bool:
    """True when one word, or two adjacent words (Urdu often splits names: سیف اسپین), sound like the brand.
    Filler and number words never count, so "four boxes" cannot pass for Surbex."""
    toks = [t for t in tokens(transcript) if t.lower() not in STOP and t.lower() not in NUM and t not in NUM]
    b = skeleton(brand)
    for n in (1, 2):
        for i in range(len(toks) - n + 1):
            w = to_latin("".join(toks[i : i + n]))
            if SequenceMatcher(None, w, brand).ratio() >= 0.8:
                return True
            s = skeleton(w)
            if not s or abs(len(s) - len(b)) > 1 or s[0] != b[0]:
                continue
            if (len(b) >= 4 and SequenceMatcher(None, s, b).ratio() >= 0.75) or s == b:
                return True
    return False


def numbers(transcript: str) -> set[float]:
    out = set()
    for w in tokens(transcript):
        lw = w.lower()
        if re.fullmatch(r"\d+", lw):
            out.add(float(lw))
        elif lw in NUM:
            out.add(float(NUM[lw]))
        elif w in NUM:
            out.add(float(NUM[w]))
    return out


def score(transcript: str, items: list) -> dict:
    nums = numbers(transcript)
    got = [brand_heard(transcript, b) and float(q) in nums for b, q in items]
    brands = [brand_heard(transcript, b) for b, _ in items]
    return {"items": len(items), "items_ok": sum(got), "brands_ok": sum(brands), "qty_ok": sum(float(q) in nums for _, q in items),
            "all_ok": all(got), "script": script(transcript)}


def report(results_file: Path) -> dict:
    rows = json.loads(results_file.read_text(encoding="utf-8"))
    agg = defaultdict(lambda: defaultdict(float))
    for r in rows:
        s = score(r["text"], r["items"])
        for key in ("ALL", r["group"], r["cond"], f'{r["group"]}/{r["cond"]}'):
            a = agg[key]
            a["clips"] += 1
            a["items"] += s["items"]
            a["items_ok"] += s["items_ok"]
            a["brands_ok"] += s["brands_ok"]
            a["qty_ok"] += s["qty_ok"]
            a["orders_ok"] += s["all_ok"]
            a["deva"] += s["script"] == "devanagari"
            a["sec"] += r.get("sec", 0)
    return {k: {"item_acc": round(100 * v["items_ok"] / v["items"], 1), "brand": round(100 * v["brands_ok"] / v["items"], 1),
                "qty": round(100 * v["qty_ok"] / v["items"], 1), "order_acc": round(100 * v["orders_ok"] / v["clips"], 1),
                "hindi_script": int(v["deva"]), "avg_sec": round(v["sec"] / v["clips"], 2)} for k, v in agg.items()}


if __name__ == "__main__":
    for f in sys.argv[1:]:
        rep = report(Path(f))
        print(f"\n== {Path(f).stem}")
        for k in ["ALL", "urdu", "roman", "english", "clean", "noisy", "harsh"]:
            if k in rep:
                print(f"  {k:8s} {rep[k]}")
