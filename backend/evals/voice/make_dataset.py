"""Builds the voice-order benchmark: TTS speech in Urdu / code-mixed Roman Urdu / English, then three degradations
that mimic real WhatsApp voice notes (clean opus, noisy shop, harsh phone). Writes clips/*.ogg and manifest.json."""

import asyncio
import json
import random
import subprocess
from pathlib import Path

import edge_tts
import numpy as np
import soundfile as sf

ROOT = Path(__file__).parent
RAW = ROOT / "raw"
CLIPS = ROOT / "clips"
RAW.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)

UR = ["ur-PK-AsadNeural", "ur-PK-UzmaNeural", "ur-IN-SalmanNeural", "ur-IN-GulNeural"]
EN = ["en-IN-PrabhatNeural", "en-IN-NeerjaNeural"]
HI = ["hi-IN-MadhurNeural", "hi-IN-SwaraNeural"]

# (id, language group, voice pool, text spoken, expected items [(brand, qty)])
ITEMS = [
    ("U01", "urdu", UR, "السلام علیکم بھائی، دس ڈبے آگمنٹن اور پانچ ڈبے رائزیک بھیج دیں", [("augmentin", 10), ("risek", 5)]),
    ("U02", "urdu", UR, "مجھے بیس پتے فلیجل چاہیے اور تین بوتل موٹیلیم", [("flagyl", 20), ("motilium", 3)]),
    ("U03", "urdu", UR, "بھائی جان، پچاس ڈبے ڈسپرین اور دو کارٹن سربیکس آج شام تک پہنچا دیں", [("disprin", 50), ("surbex", 2)]),
    ("U04", "urdu", UR, "ایک ڈبہ نیکسم چالیس اور چار ڈبے ویلوسیف", [("nexum", 1), ("velosef", 4)]),
    ("U05", "urdu", UR, "پندرہ بوتل وینٹولین سیرپ اور چھ بوتل میوکین", [("ventolin", 15), ("mucaine", 6)]),
    ("U06", "urdu", UR, "کلیریسڈ پانچ سو والی آٹھ ڈبے، اور لیفلوکس بارہ ڈبے", [("klaricid", 8), ("leflox", 12)]),
    ("U07", "urdu", UR, "تیس ڈبے گلوکوفیج اور دس ڈبے کونکور پانچ ملی گرام", [("glucophage", 30), ("concor", 10)]),
    ("U08", "urdu", UR, "بھائی پچھلی دفعہ والا آرڈر، بس اس میں سات ڈبے لوپرن زیادہ کر دیں", [("loprin", 7)]),
    ("U09", "urdu", UR, "نوویڈیٹ کی پچیس گولیوں والے دو ڈبے اور پولی فیکس کی چار ٹیوبیں", [("novidat", 2), ("polyfax", 4)]),
    ("U10", "urdu", UR, "گیوسکون کی نو بوتلیں اور بروفن سیرپ کی سات بوتلیں بھیج دو", [("gaviscon", 9), ("brufen", 7)]),
    ("U11", "urdu", UR, "ریجکس کے سو پتے اور لپی جیٹ کے بیس ڈبے", [("rigix", 100), ("lipiget", 20)]),
    ("U12", "urdu", UR, "سیف اسپین چار سو کے چھ ڈبے، جلدی بھیجنا", [("cefspan", 6)]),
    ("U13", "urdu", UR, "اموکسل پانچ سو کے تین ڈبے اور ڈائیکلوران جیل کے دس پیس", [("amoxil", 3), ("dicloran", 10)]),
    ("U14", "urdu", UR, "وورین پچاس کے پندرہ ڈبے، اور موٹیلیم گولیاں چار ڈبے", [("voren", 15), ("motilium", 4)]),
    ("R01", "roman", EN, "Assalam o alaikum bhai, 10 box Augmentin aur 5 box Nexum bhej do please", [("augmentin", 10), ("nexum", 5)]),
    ("R02", "roman", EN, "Flagyl ke 20 patte aur Motilium ki 3 bottle chahiye, urgent", [("flagyl", 20), ("motilium", 3)]),
    ("R03", "roman", EN, "Bhai jaan 12 carton Disprin aur 8 box Velosef aaj hi send kar dein", [("disprin", 12), ("velosef", 8)]),
    ("R04", "roman", EN, "Ventolin syrup 6 bottle, Mucaine 4 bottle aur Gaviscon 2 bottle", [("ventolin", 6), ("mucaine", 4), ("gaviscon", 2)]),
    ("R05", "roman", EN, "Klaricid 500 wali 3 dabbi aur Leflox 250 ke 15 box", [("klaricid", 3), ("leflox", 15)]),
    ("R06", "roman", EN, "Glucophage 30 box, Concor 10 box, aur Loprin 7 box chahiye", [("glucophage", 30), ("concor", 10), ("loprin", 7)]),
    ("R07", "roman", HI, "भाई दस डिब्बे ऑगमेंटिन और पाँच डिब्बे राइज़ेक भेज दो", [("augmentin", 10), ("risek", 5)]),
    ("R08", "roman", HI, "बीस पत्ते फ्लैजिल और तीन बोतल मोटिलियम चाहिए", [("flagyl", 20), ("motilium", 3)]),
    ("R09", "roman", HI, "नोविडैट के दो डिब्बे और पॉलीफैक्स की चार ट्यूब भेजना", [("novidat", 2), ("polyfax", 4)]),
    ("R10", "roman", HI, "सर्बेक्स के पचास डिब्बे और ब्रूफ़ेन सिरप की सात बोतल", [("surbex", 50), ("brufen", 7)]),
    ("E01", "english", EN, "Hello, please send ten boxes of Augmentin and five boxes of Risek.", [("augmentin", 10), ("risek", 5)]),
    ("E02", "english", EN, "I need twenty strips of Flagyl and three bottles of Motilium.", [("flagyl", 20), ("motilium", 3)]),
    ("E03", "english", EN, "Send fifty boxes of Disprin and two cartons of Surbex by this evening.", [("disprin", 50), ("surbex", 2)]),
    ("E04", "english", EN, "One box of Nexum forty and four boxes of Velosef please.", [("nexum", 1), ("velosef", 4)]),
    ("E05", "english", EN, "Fifteen bottles of Ventolin syrup and six bottles of Mucaine.", [("ventolin", 15), ("mucaine", 6)]),
    ("E06", "english", EN, "Eight boxes of Klaricid five hundred and twelve boxes of Leflox.", [("klaricid", 8), ("leflox", 12)]),
    ("E07", "english", EN, "Thirty boxes of Glucophage and ten boxes of Concor.", [("glucophage", 30), ("concor", 10)]),
    ("E08", "english", EN, "Please add seven boxes of Loprin to my last order.", [("loprin", 7)]),
    ("E09", "english", EN, "Nine bottles of Gaviscon and seven bottles of Brufen syrup.", [("gaviscon", 9), ("brufen", 7)]),
    ("E10", "english", EN, "A hundred strips of Rigix and twenty boxes of Lipiget.", [("rigix", 100), ("lipiget", 20)]),
]

SR = 16000


async def tts_all() -> None:
    rng = random.Random(7)
    for i, (cid, _, pool, text, _) in enumerate(ITEMS):
        out = RAW / f"{cid}.mp3"
        if out.exists():
            continue
        voice = pool[i % len(pool)]
        rate = rng.choice(["-12%", "-5%", "+0%", "+10%", "+18%"])  # slow and fast talkers
        pitch = rng.choice(["-6Hz", "+0Hz", "+8Hz"])
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out))
        print(cid, voice, rate, pitch)


def load(path: Path) -> np.ndarray:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.float32).copy()


def mix(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    noise = np.resize(noise, speech.shape)
    ps, pn = np.mean(speech**2) + 1e-12, np.mean(noise**2) + 1e-12
    return speech + noise * np.sqrt(ps / (pn * 10 ** (snr_db / 10)))


def pink(n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n)
    f = np.fft.rfft(white)
    f /= np.sqrt(np.arange(1, len(f) + 1))
    return np.fft.irfft(f, n)


def crackle(n: int, rng: np.random.Generator, rate: float) -> np.ndarray:
    x = np.zeros(n)
    idx = rng.choice(n, int(n * rate), replace=False)
    x[idx] = rng.uniform(-1, 1, len(idx))
    return x


def encode(x: np.ndarray, out: Path, kbps: int, extra: str = "") -> None:
    wav = out.with_suffix(".tmp.wav")
    sf.write(wav, np.clip(x, -1, 1), SR)
    af = ["-af", extra] if extra else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), *af, "-c:a", "libopus", "-b:a", f"{kbps}k", "-ar", "48000",
                    "-application", "voip", str(out)], check=True)
    wav.unlink()


def degrade() -> list[dict]:
    rng = np.random.default_rng(11)
    voices = {cid: load(RAW / f"{cid}.mp3") for cid, *_ in ITEMS}
    manifest = []
    for cid, group, _, text, items in ITEMS:
        s = voices[cid]
        s = np.concatenate([np.zeros(int(0.4 * SR)), s, np.zeros(int(0.5 * SR))]).astype(np.float64)
        s *= 0.5 / (np.max(np.abs(s)) + 1e-9)
        others = [v for k, v in voices.items() if k != cid]
        babble = sum(np.resize(others[j], s.shape) for j in rng.choice(len(others), 4, replace=False))
        hum = 0.3 * np.sin(2 * np.pi * 50 * np.arange(len(s)) / SR)  # fan / UPS hum
        street = pink(len(s), rng) + 0.5 * babble / (np.std(babble) + 1e-9) * np.std(pink(len(s), rng))

        conds = {
            "clean": (s, 24, ""),
            "noisy": (mix(s, street + hum * 0.2, 8) + 0.02 * crackle(len(s), rng, 0.002), 16, ""),
            # 0-2 dB crowd, phone band, clipping, wobbling volume (tremolo) and pitch (vibrato), very low bitrate
            "harsh": (np.clip(1.8 * mix(s, babble + pink(len(s), rng) + hum, 2), -0.6, 0.6) + 0.05 * crackle(len(s), rng, 0.004),
                      8, "highpass=f=300,lowpass=f=3400,tremolo=f=3:d=0.4,vibrato=f=5:d=0.15"),
        }
        for cond, (x, kbps, extra) in conds.items():
            out = CLIPS / f"{cid}_{cond}.ogg"
            encode(x, out, kbps, extra)
            manifest.append({"id": f"{cid}_{cond}", "utt": cid, "group": group, "cond": cond, "text": text,
                             "items": items, "file": str(out.relative_to(ROOT)).replace("\\", "/")})
    return manifest


if __name__ == "__main__":
    asyncio.run(tts_all())
    m = degrade()
    (ROOT / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(m), "clips")
