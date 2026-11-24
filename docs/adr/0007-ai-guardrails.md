# ADR 0007: AI guardrails and red-team testing

**Status:** Accepted · 2026-11-24 · Code: `backend/src/ordexa/modules/ai/guardrails.py`

## Principle
The AI model (Groq, Claude, or the rule parser) is an **untrusted proposal generator**:
- It has no tools and no database access, and cannot change anything.
- It only returns schema-validated item lines.
- Every business fact comes from the ERP: product identity, price, stock, credit and the customer (from the verified
  WhatsApp number).
- A person with `sales_order.approve` turns a draft into a bill.

## Guardrails, run on every reading
| Risk | Guardrail |
|---|---|
| Prompt injection | English and Roman Urdu patterns are flagged and never obeyed: overrides, "approve kar do", discount/price/rate requests, credit changes, admin/SQL, identity claims, links, pasted JSON. Flagged orders always need a person |
| Breaking out of the prompt | Delimiter tags (`</order_message>`, `<system>`) and invisible characters are removed before any model sees the text |
| Hallucinated items | Each item must be traceable to words in the shop's message (spelling-tolerant), otherwise it's flagged `not_in_message` |
| Hallucinated quantities | Each quantity must appear in the message (digits or Urdu/English number words), otherwise it's flagged |
| Absurd values | More than 1,000 per line, or fractional counts, are flagged |
| Model trying to clear its own flags | Flags are set only by our code; any flags the model returns are discarded |
| Photos and Urdu script | These can't be checked word by word, so the order shows "compare with the picture/message before approving" |
| Spam and abuse | 30 messages per hour per number; links are never echoed back to shops |
| Provider limits or outage | One short wait-and-retry on a rate limit, then the rule parser; photos and voice notes wait for staff. Nothing is lost |

Flags block "ready to approve". Only a staff confirmation, or the shop's own answer to the quantity question,
clears them.

## Testing
- **`tests/test_guardrails.py`** (runs in CI): unit tests for the patterns, sanitising and grounding. Integration
  tests use a deliberately misbehaving fake model: invented items, inflated quantities, a missed injection,
  self-set flags, a delimiter break-out, a customer and price spoof.
- **`evals/run_redteam.py`**: 16 live attack and edge cases sent through the real WhatsApp pipeline to the real
  AI provider. The result on 2026-11-24 with Groq `openai/gpt-oss-120b` was **16/16 passed**. Re-run it after
  changing models or prompts:
  ```
  python evals/run_redteam.py --business <code> --password <admin password> --delay 10
  ```

## Limits
- Pattern detection is a safety net, not a guarantee. A determined attacker may phrase things the patterns miss.
- That is why the real protection is structural: the model can't act, the ERP decides facts, and a person approves.
