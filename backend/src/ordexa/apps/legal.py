"""Public privacy policy and data-deletion pages (Meta requires both URLs to publish a WhatsApp app)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["legal"])

_STYLE = """<style>body{font-family:system-ui,Segoe UI,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;line-height:1.6;
color:#17202a}h1{font-size:26px}h2{font-size:18px;margin-top:28px}</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
        f"content='width=device-width,initial-scale=1'><title>{title}</title>{_STYLE}</head><body>"
        f"<h1>{title}</h1>{body}<p><small>Ordexa ERP · last updated 24 November 2026</small></p></body></html>"
    )


@router.get("/legal/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy():
    return _page(
        "Privacy Policy",
        """
<p>Ordexa ERP is order and billing software used by medicine distributors ("the business") to receive orders from
their retail customers over WhatsApp.</p>
<h2>What we collect</h2>
<p>When you message a business that uses Ordexa on WhatsApp we receive: your WhatsApp phone number, the messages you
send (text, photos of order lists, voice notes), and the shop details you give when signing up.</p>
<h2>How we use it</h2>
<p>Only to identify your shop, read your orders, prepare bills, answer your questions (for example your balance) and
keep the business's legally required records. We do not sell your data or use it for advertising.</p>
<h2>Who processes it</h2>
<p>The business you order from; Meta (WhatsApp) to deliver messages; and an AI service (Groq) that reads order text,
photos and voice notes so the business can prepare a draft bill. A person at the business checks every order before
a bill is made.</p>
<h2>How long we keep it</h2>
<p>Orders and bills are kept as long as the business must keep accounting records. Messages, photos and voice notes
that are not part of a bill can be deleted on request.</p>
<h2>Your choices</h2>
<p>You can stop ordering on WhatsApp at any time and ask for your data to be deleted: see the
<a href="/legal/data-deletion">data deletion instructions</a>.</p>
""",
    )


@router.get("/legal/data-deletion", response_class=HTMLResponse, include_in_schema=False)
def data_deletion():
    return _page(
        "Data Deletion Instructions",
        """
<p>To delete the data held about you by a business that uses Ordexa ERP:</p>
<ol>
<li>Contact the business you order from (by phone, in person, or on its WhatsApp number) and ask it to delete your data.</li>
<li>The business removes your WhatsApp contact, messages, photos and voice notes and unlinks your number within
30 days.</li>
<li>Bills and payment records that the business must keep by law are retained for the legal period and then
deleted.</li>
</ol>
""",
    )
