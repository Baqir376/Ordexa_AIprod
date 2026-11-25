"""End-to-end: transcript(s) (+ catalogue sound-alike hints) -> LLM order lines; scored on product + quantity."""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import openpyxl

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parents[1] / "src/ordexa/modules/ai"))
import phonetic as ph  # noqa: E402

KEY = next(l.split("=", 1)[1].strip() for l in (ROOT.parents[1] / ".env").read_text().splitlines() if l.startswith("GROQ_API_KEY="))
NAMES = [str(r[1]).strip() for r in openpyxl.load_workbook(ROOT.parents[2] / "medicine_data.xlsx", read_only=True).active.iter_rows(values_only=True) if r[1]]

SYSTEM = """You read WhatsApp voice notes that Pakistani pharmacies send to a medicine distributor. Speech recognition has
already turned the voice note into text, sometimes in two hearings. The text can be wrong: medicine names misspelled or
written in Urdu or Hindi script, numbers as words in any language, and noisy notes may even be wrongly translated.
Work out what the shop actually asked for.
- "Catalogue products that sound like words in the note" lists real products whose names sound like something heard.
  When a heard word is a misspelling of one of them, use that product. Never pick a product that nothing in the note
  sounds like.
- If the two hearings disagree, prefer the one that makes sense as a medicine order.
- qty is the number of units ordered (10 dabbe = 10), not the strength (Klaricid 500 = 500 mg strength).
Return every ordered item."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                        "required": ["product", "qty", "unit"],
                                                        "properties": {"product": {"type": "string"}, "qty": {"type": "number"},
                                                                       "unit": {"type": "string"}}}}},
}


def user_prompt(texts: list[str], hints: bool) -> str:
    parts = [f"Hearing {i + 1}: {t}" for i, t in enumerate(texts)] if len(texts) > 1 else [f"Voice note text: {texts[0]}"]
    if hints:
        c = ph.candidates(texts, NAMES)
        lines = [f'- heard "{x["heard"]}" -> {" | ".join(x["products"][:3])}' for x in c]
        parts.append("Catalogue products that sound like words in the note:\n" + ("\n".join(lines) if lines else "(none)"))
    return "\n\n".join(parts)


def call(model: str, user: str) -> dict:
    extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
    body = json.dumps({"model": model, "temperature": 0, "max_completion_tokens": 4000, **extra,
                       "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                       "response_format": {"type": "json_schema", "json_schema": {"name": "order", "strict": True, "schema": SCHEMA}}}).encode()
    for _ in range(10):
        req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": "voicebench"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.loads(r.read())
                return {**json.loads(res["choices"][0]["message"]["content"]), "tokens": res["usage"]["total_tokens"]}
        except urllib.error.HTTPError as e:
            msg = e.read()[:400].decode(errors="replace")
            if e.code == 429:
                if "per day" in msg:
                    raise RuntimeError("daily token limit: " + msg) from e
                time.sleep(min(float(e.headers.get("retry-after") or 15), 90) + 1)
                continue
            if e.code == 400 and "json_validate_failed" in msg:
                bad = locals().get("bad", 0) + 1
                if bad >= 3:
                    return {"items": [], "tokens": 0}
                continue
            raise RuntimeError(f"{e.code} {msg}") from e
    raise RuntimeError("rate limited too long")


def correct(items_out: list[dict], expected: list) -> int:
    ok = 0
    for brand, qty in expected:
        if any(brand in o["product"].lower().replace(" ", "") and abs(float(o["qty"]) - qty) < 1e-6 for o in items_out):
            ok += 1
    return ok


def run(name: str, sources: list[str], hints: bool, model: str) -> None:
    import os

    out = ROOT / "results" / f"llm{os.environ.get('BENCH_TAG', '')}_{name}.json"
    done = {r["id"]: r for r in json.loads(out.read_text(encoding="utf-8"))} if out.exists() else {}
    src = [{r["id"]: r for r in json.loads((ROOT / "results" / f"{s}.json").read_text(encoding="utf-8"))} for s in sources]
    for cid, clip in src[0].items():
        if cid in done:
            continue
        texts = [s[cid]["text"] for s in src]
        res = call(model, user_prompt(texts, hints))
        done[cid] = {"id": cid, "group": clip["group"], "cond": clip["cond"], "items": clip["items"], "out": res["items"],
                     "ok": correct(res["items"], clip["items"]), "tokens": res["tokens"]}
        out.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(4.5)  # 8k tokens/minute on the free tier
    print(name, "done", flush=True)


def report(name: str) -> None:
    import os

    tag = os.environ.get("BENCH_TAG", "")
    rows = json.loads((ROOT / "results" / f"llm{tag}_{name}.json").read_text(encoding="utf-8"))
    print(f"== llm{tag}_{name}  ({sum(r['tokens'] for r in rows) / len(rows):.0f} tokens/note)")
    for key in ["ALL", "urdu", "roman", "english", "clean", "noisy", "harsh"]:
        sel = [r for r in rows if key == "ALL" or key in (r["group"], r["cond"])]
        if sel:
            items = sum(len(r["items"]) for r in sel)
            print(f"  {key:8s} item_acc {100 * sum(r['ok'] for r in sel) / items:5.1f}   order_acc {100 * sum(r['ok'] == len(r['items']) for r in sel) / len(sel):5.1f}")


RUNS = {
    "prod_plain": (["g_v3_prod"], False),
    "cat_plain": (["g_v3_cat"], False),
    "cat_hints": (["g_v3_cat"], True),
    "fused_hints": (["g_v3_cat", "g_v3_ur"], True),
    "final_hints": (["final"], True),
}

if __name__ == "__main__":
    import os

    model = os.environ.get("BENCH_MODEL", "openai/gpt-oss-20b")
    tag = os.environ.get("BENCH_TAG", "")
    for n in sys.argv[1:]:
        if n == "report":
            for k in RUNS:
                if (ROOT / "results" / f"llm{tag}_{k}.json").exists():
                    report(k)
            continue
        run(n, *RUNS[n], model)
