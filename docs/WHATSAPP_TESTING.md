# Testing WhatsApp ordering with Meta's free test number

You need about 20 minutes, a Facebook account and your phone.

## 1. Start Ordexa on your PC
Open PowerShell in the `ordexa` folder:
```powershell
.\scripts\start-local.ps1
```
This starts the server (database, API, WhatsApp webhook, AI workers) and opens the desktop app. Sign in as an
administrator.

## 2. Give the server a public address
Open a **second** PowerShell window in the `ordexa` folder:
```powershell
.\scripts\start-tunnel.ps1
```
- Copy the `https://….trycloudflare.com` address it prints.
- Keep this window open while you test.
- The address changes each time you run the script. When it does, update it in Ordexa and in Meta.

## 3. Get your Meta test number
1. Go to **developers.facebook.com → My Apps → Create App** (type *Business*) and add the **WhatsApp** product.
2. Open **WhatsApp → API Setup**:
   - copy the **Phone number ID** (the long number, not the phone number itself);
   - copy the **Temporary access token** (it expires after 24 hours; paste a new one in Ordexa when it does);
   - under **To**, add your own WhatsApp number and confirm the code Meta sends.
3. Open **App settings → Basic** and copy the **App secret**.

## 4. Connect it in Ordexa
1. Go to **Settings → WhatsApp connection**.
2. Paste the Phone number ID, the access token, the app secret and the tunnel address, then press **Save**.
3. Press **Test connection**. It should say "Connected to +1 555…".
4. Optional: press **Send test message** with your number. Meta's "Hello World" message arrives on your phone.
5. Copy the **Callback URL** and **Verify token** shown on the card.

## 5. Point Meta at Ordexa
1. In Meta, open **WhatsApp → Configuration → Webhook → Edit**.
2. Paste the Callback URL and Verify token, then press **Verify and save**.
3. Under **Webhook fields**, subscribe to **messages**.

## 6. Test from your phone
Send messages to the test number:

| Send | Expect |
|---|---|
| `hi` | Menu: registered shop (1) or new account (2) |
| `2`, then `Ali Medical, Gulberg, Ali` | "Team will verify" → approve it in **WhatsApp sign-ups** |
| `10 dabba panadol extra aur 2 botal calpol` | Order appears in **WhatsApp orders**, ready to approve; you get a confirmation |
| `3 box panadol` | The bot asks "which Panadol? 1) … 2) …". Reply `2` |
| A photo of a written order list | Items read from the photo (with a Groq key) |
| A voice note listing items | Transcribed and read (with a Groq key) |
| `balance` | Your outstanding balance |

Approve an order in **WhatsApp orders** and the shop receives "Order confirm ho gaya, Bill W-…".

**Tips:**
- **Shortcut to skip approval:** set a customer's phone (Customers → Edit) to your own WhatsApp number. Then reply
  `1` and that shop code, and you're verified instantly.
- **Avoid "registered shop" with a different number on file:** on a test number, replying `1` + a shop code whose
  phone on file is *another* number tries to send a one-time code, which needs an approved template you don't
  have yet. Use the shortcut above, or the new-account route.
- **If something doesn't arrive,** check **Settings → WhatsApp connection**, which shows the last error from Meta,
  and the message list in **WhatsApp sign-ups → Test chat** (load your number).
