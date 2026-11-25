"""Runs one Groq Whisper configuration over every clip; results/<name>.json (resumable)."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import openpyxl

ROOT = Path(__file__).parent
RES = ROOT / "results"
RES.mkdir(exist_ok=True)
KEY = next(l.split("=", 1)[1].strip() for l in (ROOT.parents[1] / ".env").read_text().splitlines() if l.startswith("GROQ_API_KEY="))

PROD_PROMPT = (
    "Pakistani pharmacy order in Urdu, Roman Urdu or English. Product names like Panadol, Brufen, Calpol, Augmentin, "
    "Risek. Units like box, dabba, patta, strip, carton, botal. Numbers like 10, das, paanch."
)
TESTED = ["Augmentin", "Risek", "Flagyl", "Motilium", "Disprin", "Surbex", "Nexum", "Velosef", "Ventolin", "Mucaine", "Klaricid",
          "Leflox", "Glucophage", "Concor", "Loprin", "Novidat", "Polyfax", "Gaviscon", "Brufen", "Rigix", "Lipiget", "Cefspan",
          "Amoxil", "Dicloran", "Voren"]


def catalogue_names(n_extra: int = 35) -> list[str]:
    """The tested brands plus unrelated catalogue brands (a shop's real prompt is its most-ordered products)."""
    ws = openpyxl.load_workbook(ROOT.parents[2] / "medicine_data.xlsx", read_only=True).active
    import random

    brands = sorted({str(r[1]).split()[0].title() for r in ws.iter_rows(values_only=True) if r[1] and str(r[1])[0].isalpha()})
    brands = [b for b in brands if len(b) > 3 and b not in TESTED]
    random.Random(3).shuffle(brands)
    names = TESTED + brands[:n_extra]
    random.Random(5).shuffle(names)
    return names


CAT = catalogue_names()
CAT_PROMPT = "Medicine order. Products: " + ", ".join(CAT) + ". Units: box, dabba, patta, strip, carton, botal."
CAT_PROMPT_UR = "دوائیوں کا آرڈر۔ " + ", ".join(CAT) + "۔ دس ڈبے، پانچ بوتل، بیس پتے۔"

PRE = {
    "none": None,
    "fft": "highpass=f=90,lowpass=f=7600,afftdn=nr=14:nf=-35:tn=1,dynaudnorm=f=150:g=11",
    "rnn": "highpass=f=90,arnndn=m=rnn/sh.rnnn,dynaudnorm=f=150:g=11",
    "norm": "highpass=f=90,dynaudnorm=f=150:g=11",
}

CONFIGS = {
    "g_v3_prod": dict(model="whisper-large-v3", prompt=PROD_PROMPT),
    "g_turbo_prod": dict(model="whisper-large-v3-turbo", prompt=PROD_PROMPT),
    "g_v3_cat": dict(model="whisper-large-v3", prompt=CAT_PROMPT),
    "g_v3_ur": dict(model="whisper-large-v3", prompt=PROD_PROMPT, language="ur"),
    "g_v3_ur_catur": dict(model="whisper-large-v3", prompt=CAT_PROMPT_UR, language="ur"),
    "g_v3_cat_fft": dict(model="whisper-large-v3", prompt=CAT_PROMPT, pre="fft"),
    "g_v3_cat_rnn": dict(model="whisper-large-v3", prompt=CAT_PROMPT, pre="rnn"),
    "g_v3_cat_norm": dict(model="whisper-large-v3", prompt=CAT_PROMPT, pre="norm"),
}


def prepare(path: Path, pre: str | None) -> tuple[bytes, str, str]:
    if not pre:
        return path.read_bytes(), "audio/ogg", "voice.ogg"
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-af", PRE[pre], "-ac", "1", "-ar", "16000", "-c:a", "flac", "-f", "flac", "-"],
                         capture_output=True, check=True, cwd=ROOT).stdout
    return out, "audio/flac", "voice.flac"


def transcribe(data: bytes, ctype: str, fname: str, model: str, prompt: str, language: str | None) -> dict:
    boundary = uuid.uuid4().hex
    fields = {"model": model, "response_format": "verbose_json", "temperature": "0", "prompt": prompt}
    if language:
        fields["language"] = language
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    for attempt in range(8):
        req = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": f"multipart/form-data; boundary={boundary}",
                                              "User-Agent": "voicebench"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(e.headers.get("retry-after") or 20)
                print(f"  rate limited, waiting {wait:.0f}s", flush=True)
                time.sleep(min(wait, 120) + 1)
                continue
            raise RuntimeError(f"{e.code} {e.read()[:300]!r}") from e
    raise RuntimeError("gave up after rate limits")


def run(name: str) -> None:
    cfg = CONFIGS[name]
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    out = RES / f"{name}.json"
    done = {r["id"]: r for r in json.loads(out.read_text(encoding="utf-8"))} if out.exists() else {}
    for clip in manifest:
        if clip["id"] in done:
            continue
        data, ctype, fname = prepare(ROOT / clip["file"], cfg.get("pre"))
        t0 = time.time()
        res = transcribe(data, ctype, fname, cfg["model"], cfg["prompt"], cfg.get("language"))
        segs = res.get("segments") or []
        done[clip["id"]] = {**clip, "text": (res.get("text") or "").strip(), "language": res.get("language"), "sec": round(time.time() - t0, 2),
                            "avg_logprob": round(sum(s.get("avg_logprob", 0) for s in segs) / max(1, len(segs)), 3),
                            "no_speech": round(max([s.get("no_speech_prob", 0) for s in segs] or [0]), 3)}
        out.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(3.1)  # 20 requests/minute on the free tier
    print(name, "done", len(done), flush=True)


if __name__ == "__main__":
    for n in sys.argv[1:]:
        run(n)
