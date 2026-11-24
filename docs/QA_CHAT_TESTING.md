# Exploratory WhatsApp chat testing

`backend/evals/chat_scenarios.py` plays 50 shops talking to the bot, including deliberately difficult ones. It runs
against a running server **with the real AI**, through the simulator, on a separate test business. Nothing is sent
to WhatsApp.

## Run it
```powershell
docker compose exec api ordexa create-tenant --code qa-lab --name "QA Lab Distributors" --password "qa-pass-12345"
docker compose exec api ordexa seed-demo --code qa-lab
cd backend
.venv\Scripts\python evals\chat_scenarios.py --password qa-pass-12345 --out chat_results.json
# only some:  --only B1,C1,D3
```
The Groq free tier allows about 8,000 tokens a minute, so the script waits 12 s after each AI message (`--pace`).
A full run takes about 25 minutes and uses a good part of the day's free quota.

Every scenario prints `OK` or `CHK` with the automatic checks that failed. Then read the transcripts in the JSON file
yourself. The checks catch regressions, but only reading the chats shows whether the replies make sense.

## Who the scenarios play
| Group | Shops | Examples |
|---|---|---|
| A. Strangers / sign-up | Not registered yet | Orders straight away; "haan bhai" or a shop code at the menu; guessing codes; claims another shop's number; orders while approval is pending; asks another shop's balance |
| B. Ordering | Verified shops | Roman Urdu, formal English, heavy typos, numbers in words, Urdu script, bullet lists with emojis, 5000 boxes, 2.5 boxes, products we don't sell, generic names, the same item twice, "same as last time", order plus rate question, 0 / negative quantities, more than in stock, wrong unit, rates only |
| C. Tricks and abuse | Verified shops | Prompt injection, fake "SYSTEM:" price, asking for the system prompt, another shop's balance, bargaining, abuse, emoji only, 3,000-character spam, credit over the limit, HTML / script |
| D. Conversations | Verified shops | Invalid option number, "dono", cancel then undo, change → add → remove, delivery question, adding to a billed order, asking for the bill, balance in different words, answering in words, ignoring the question, thanks, rejection and "why?", balance while an order is open, "haan" to a suggestion, complaint plus order |

## What the bot must always do
- **Never confirm an empty order.** A message that is not an order (balance, bill, status, rates, complaint,
  question, thanks) gets the matching answer. Only complaints and questions go to staff, as **Message for you**.
- **Repeat what it understood:** items, quantities and an estimated total, so the shop can correct mistakes at once.
- **Take everything from the database:** rates, balance, bills and status. The AI never writes the reply.
- **Never act on instructions in a message:** no discounts, price changes or approvals. Such orders always wait for
  a person.
- **Never show one shop's data to another.**
- **Tell the shop when staff reject an order, and why.**
