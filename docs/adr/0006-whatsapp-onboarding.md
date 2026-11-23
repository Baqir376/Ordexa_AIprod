# ADR 0006: WhatsApp sign-up and verified ordering

**Status:** Accepted · 2026-11-17 · Code: `backend/src/ordexa/modules/channels/`, tests: `backend/tests/test_whatsapp.py`

## Decision
A **fixed state machine** (not the LLM) runs the conversation. Only numbers **verified for a customer** can
order. A shop code alone is never enough, because codes like `101` are easy to guess.

```text
unknown number ─► menu: 1) registered shop  2) new account
  1 ─► shop code ─► number on file for that shop?  yes ─► verified
                                                    no, but shop has a phone ─► 6-digit code sent to THAT phone
                                                                                (template message, 10 min, hashed)
                                                    no phone on file ─► staff approval
  2 ─► "shop, area, owner" ─► staff approval (link to an existing customer or create one)
verified ─► every message becomes an AI order request; "balance" replies with the ledger balance
```

**Safeguards:**
- 5 wrong codes lock the number for 1 hour.
- Staff can unlink or block any number.
- Duplicate webhook deliveries are ignored (unique WhatsApp message id).
- Webhook signatures are verified with HMAC-SHA256.

**WhatsApp rules:**
- Free-text replies are sent only within 24 hours of the shop's last message.
- Outside that window, and for codes sent to a different number, an approved template (`ordexa_otp`) is required.

## Processes
| Step | Process | Queue |
|---|---|---|
| Receive, verify signature, enqueue | `webhook` | `channels` |
| Bot conversation, verification | `worker --queue channels` | |
| Read the order | `worker --queue ai` | |
| Send replies (Cloud API, or "simulated" without a token) | `worker --queue outbox` | |

## Going live (needs your Meta account)
1. In Meta Business: create a WhatsApp Business app and add the distributor's number.
2. Create a permanent system-user token.
3. Get a utility template named `ordexa_otp` approved, with one body variable.
4. Set the environment variables:
   - `ORDEXA_WHATSAPP_TOKEN`
   - `ORDEXA_WHATSAPP_PHONE_NUMBER_ID`
   - `ORDEXA_WHATSAPP_APP_SECRET`
   - `ORDEXA_WHATSAPP_VERIFY_TOKEN`
5. Point the webhook at `https://<server>/webhooks/whatsapp` (served by the `webhook` process).
6. Register the number for the business: `POST /api/v1/channels/whatsapp/numbers`.

Until then, **Settings → WhatsApp sign-ups → Test chat** runs the same bot and AI end to end.

## Addendum: answers, photos and voice notes (2026-11-23)
- **Answers to questions.**
  - The bot asks one question at a time.
  - A short reply ("2", "extra wali", "12") from a shop with an open order is applied to that order instead of
    starting a new one, within 24 hours. A reply that names a product with a quantity is still treated as a new order.
  - A shop's choice is marked `resolution: customer`. It is **not** saved as approved memory; only staff approve
    aliases.
- **Photo of an order list.**
  - The worker downloads the image through the Cloud API and stores it with the order (`ai_attachments`).
  - Claude reads the photo with the same JSON schema as text.
  - Without a Claude key, the photo waits on the order screen for staff to add the items.
- **Voice note.** Stored with the order and playable on the order screen; staff listen and add the items.
  `AiAttachment.transcript` is ready for a speech-to-text provider (Google Speech-to-Text or Whisper; Claude does not
  take audio input). Once a provider is configured, the transcript goes through the same pipeline as text.
- The simulator accepts photos and voice files, so all of this can be tested without Meta.
