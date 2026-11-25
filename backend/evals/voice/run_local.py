"""Runs Hugging Face speech models on the local GPU over every clip; results/<name>.json."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
import numpy as np  # noqa: E402
import torch  # noqa: E402
from transformers import pipeline  # noqa: E402

RES = ROOT / "results"
RES.mkdir(exist_ok=True)

MODELS = {
    "hf_turbo_urdu": ("kingabzpro/whisper-large-v3-turbo-urdu", {"language": "ur", "task": "transcribe"}),
    "hf_v3_urdu": ("kingabzpro/whisper-large-v3-urdu", {"language": "ur", "task": "transcribe"}),
    "hf_small_roman": ("cheetos18/whisper-small-roman-urdu", {}),
    "hf_turbo_roman": ("Farhan-SE/whisper-large-v3-turbo-roman-urdu-model", {}),
    "hf_wav2vec2_urdu": ("kingabzpro/wav2vec2-large-xls-r-300m-Urdu", None),
    "hf_turbo_base": ("openai/whisper-large-v3-turbo", {"task": "transcribe"}),
}


def load(path: Path) -> np.ndarray:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.float32).copy()


def run(name: str) -> None:
    repo, gen = MODELS[name]
    out = RES / f"{name}.json"
    if out.exists():
        print(name, "exists")
        return
    t0 = time.time()
    asr = pipeline("automatic-speech-recognition", model=repo, torch_dtype=torch.float16, device=0)
    print(name, "loaded in", round(time.time() - t0), "s", flush=True)
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    for clip in manifest:
        audio = load(ROOT / clip["file"])
        t = time.time()
        kw = {"generate_kwargs": {**gen, "num_beams": 1}} if gen is not None else {}
        text = asr({"raw": audio, "sampling_rate": 16000}, **kw)["text"]
        rows.append({**clip, "text": text.strip(), "sec": round(time.time() - t, 2)})
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(name, "done", flush=True)
    del asr
    torch.cuda.empty_cache()


if __name__ == "__main__":
    for n in sys.argv[1:]:
        try:
            run(n)
        except Exception as exc:  # noqa: BLE001 - keep benchmarking the other models
            print(n, "FAILED", type(exc).__name__, str(exc)[:400], flush=True)
