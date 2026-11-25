# Voice-note benchmark (Urdu, Roman Urdu, English)

How Ordexa's WhatsApp voice orders are tested, and why the production pipeline (`src/ordexa/modules/ai/voice.py`)
is built the way it is.

## Test set

34 spoken medicine orders × 3 conditions = **102 clips** (`make_dataset.py`):

| Group | Clips | Speech |
|---|---|---|
| Urdu | 14 | Urdu sentences with brand names, 4 Pakistani/Indian Urdu voices (male/female) |
| Roman Urdu / code-mixed | 10 | "Flagyl ke 20 patte aur Motilium ki 3 bottle chahiye", spoken by South-Asian English voices, plus Hindustani speakers |
| English | 10 | "Send fifty boxes of Disprin and two cartons of Surbex" |

Talking speed (−12% … +18%) and pitch vary per clip. Each clip is then degraded three ways, all encoded as WhatsApp
Opus voice notes:

- **clean**: 24 kbps Opus
- **noisy**: street noise and crowd babble at 8 dB SNR, 50 Hz hum, crackle, 16 kbps
- **harsh**: crowd at 2 dB SNR, phone band (300–3400 Hz), clipping, a wobbling voice (tremolo and vibrato), crackle, 8 kbps

Brand names come from the real 845-product catalogue (`medicine_data.xlsx`): Klaricid, Leflox, Cefspan, Novidat, and so on.

**Metric.** An item counts as correct only if both the medicine and its quantity are right. `score.py` checks the
transcript, accepting names in any script; it was validated at 100% on the reference texts with 0 false matches
across 25 brands. `run_llm.py` checks end to end: transcript → LLM → catalogue product and quantity.

## Results: speech recognition (`run_groq.py`, `run_local.py`)

| Setup | All | Urdu | Roman | English | clean | noisy | harsh |
|---|---|---|---|---|---|---|---|
| Before (Groq whisper-large-v3, generic prompt) | 60.2 | 52.6 | 63.6 | 66.7 | 77.6 | 76.1 | 26.9 |
| Groq whisper-large-v3-turbo | 61.2 | 42.3 | 74.2 | 71.9 | 80.6 | 70.1 | 32.8 |
| large-v3, language forced to Urdu | 60.2 | 52.6 | 57.6 | 73.7 | 76.1 | 70.1 | 34.3 |
| large-v3 + **catalogue prompt** | 71.1 | 53.8 | 78.8 | 86.0 | 85.1 | 79.1 | 49.3 |
| + RNNoise clean-up | 67.2 | 55.1 | – | – | 88.1 | 83.6 | 29.9 |
| + level normalisation only | 73.6 | 55.1 | – | – | 88.1 | 80.6 | 52.2 |
| + **FFT denoise + normalisation (production)** | **74.6** | **65.4** | – | – | **88.1** | **85.1** | 50.7 |
| HF `kingabzpro/whisper-large-v3-urdu` (local GPU) | 34.3 | 56.4 | – | 0* | 49.3 | 41.8 | 11.9 |
| HF `kingabzpro/whisper-large-v3-turbo-urdu` | 22.9 | 42.3 | 19.7 | 0* | 35.8 | 25.4 | 7.5 |
| HF `cheetos18/whisper-small-roman-urdu` | 39.8 | 37.2 | 42.4 | 40.4 | 61.2 | 44.8 | 13.4 |
| HF `kingabzpro/wav2vec2-large-xls-r-300m-Urdu` | 10.9 | 23.1 | 6.1 | 0* | 17.9 | 14.9 | 0.0 |
| HF `openai/whisper-large-v3-turbo` (local) | 62.2 | 50.0 | – | – | 80.6 | 73.1 | 32.8 |

\* The Urdu fine-tunes write English numbers and brand names in Urdu letters, or drop them. They were trained on
read Common Voice sentences, not mixed-language medicine orders, so none of them is used.

Without help, Whisper's language detection on Urdu is unreliable: 34 of 102 transcripts came back in **Hindi
script**, and some in Slovenian, Hebrew, Persian or Tagalog, or translated into English.

## Results: end to end, LLM reading the order (`run_llm.py`, gpt-oss-20b)

| Input to the order reader | Items correct | Whole order correct |
|---|---|---|
| Before: transcript only | 28.9 | 9.8 |
| Catalogue-prompt transcript only | 39.3 | 22.5 |
| Catalogue-prompt transcript + **sound-alike product hints** | **64.7** | **53.9** |
| Two hearings (auto + Urdu) + hints | 41.9 vs 46.5 single (same 22 clips) | worse |

The sound-alike hints (`phonetic.py`) find catalogue products that sound like any heard word in Urdu, Hindi or
Latin letters, e.g. رائزیک / Raizet → RISEK. They more than double end-to-end accuracy. Showing the reader two hearings
every time confused it on noisy notes, so production re-hears only when the language is wrong.

## Production pipeline (what these numbers led to)

1. ffmpeg clean-up: `highpass=90 Hz, lowpass=7.6 kHz, afftdn, dynaudnorm`, mono 16 kHz FLAC.
2. Groq `whisper-large-v3` with a prompt listing this business's own brands, best sellers first.
3. Hindi-script output is converted into Latin letters. A wrong language (not Urdu, English or Hindi) is heard again
   with Urdu forced.
4. The order reader gets the transcript plus the sound-alike products. Its corrected spelling is accepted only if
   the heard word sounds like it (`phonetic.alike`).
5. Several close look-alikes (Rizet → Risek / Rovista) are never guessed: staff or the shop pick one.
6. Clarity from Whisper confidence: a hard-to-hear note gets a warning. Staff can play the note, **Fix what was
   said**, or press **Listen again**.

Hint recall on the final transcripts is 83%, with 5.5 suggestions per note (`cand_eval.py final`).

## Run it

```
python -m venv .venv && .venv\Scripts\pip install edge-tts numpy soundfile openpyxl
.venv\Scripts\python make_dataset.py                # needs ffmpeg on PATH; writes clips/ and manifest.json
.venv\Scripts\python run_groq.py g_v3_prod g_v3_cat_fft   # uses GROQ_API_KEY from backend/.env (free tier, ~4 s/clip)
.venv\Scripts\python score.py results\*.json
.venv\Scripts\python run_llm.py prod_plain cat_hints && .venv\Scripts\python run_llm.py report
# local Hugging Face models (GPU): pip install torch --index-url https://download.pytorch.org/whl/cu124 transformers accelerate
.venv\Scripts\python run_local.py hf_v3_urdu
# one live order through the running app (qa-lab test business only, never a real shop's number)
.venv\Scripts\python live_test.py U01_noisy
```

Limits: the speech is synthetic (Azure neural voices) with added noise. Real shop recordings are harder: accents,
several people talking, long pauses. Add real voice notes, with what was actually ordered, to `manifest.json` to
measure them. The end-to-end run for the final pipeline (`run_llm.py final_hints`) needs a fresh daily Groq quota.
