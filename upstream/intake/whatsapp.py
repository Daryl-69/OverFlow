"""WhatsApp Cloud API intake (optional).

Households send the card photo to the utility's WhatsApp number and get the
reading back as a reply; a one-word reply (fine / smell / dirty / ill)
attaches the quick answer to their latest reading.

Configure with environment variables:

* ``WHATSAPP_TOKEN`` — a system-user access token for the Graph API
* ``WHATSAPP_VERIFY_TOKEN`` — any string; enter the same one in the Meta app's
  webhook settings (callback URL: ``https://<host>/whatsapp/webhook``)
* ``WHATSAPP_APP_SECRET`` — the Meta app secret, used to check
  ``X-Hub-Signature-256`` on every delivery (strongly recommended)
* ``WHATSAPP_API_VERSION`` — Graph API version, default ``v21.0``

Phone numbers are never stored: replies go to the sender of the message
being handled, and the link from a sender to their latest report is kept
under a keyed hash.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .live import IntakeError, LiveService

ANSWER_WORDS = {
    "fine": "fine", "ok": "fine", "okay": "fine", "good": "fine", "clear": "fine",
    "smell": "smell", "smells": "smell", "smelly": "smell", "odour": "smell", "odor": "smell",
    "dirty": "dirty", "colour": "dirty", "color": "dirty", "brown": "dirty", "muddy": "dirty",
    "ill": "ill", "sick": "ill", "illness": "ill",
}
log = logging.getLogger("upstream.whatsapp")

HELP = ("Send one photo of your Upstream card with the glass and strip on it. "
        "Then reply with one word: fine, smell, dirty or ill.")


@dataclass
class WhatsAppConfig:
    token: str | None = None
    verify_token: str | None = None
    app_secret: str | None = None
    api_version: str = "v21.0"
    graph_url: str = "https://graph.facebook.com"
    sender_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        key = os.environ.get("WHATSAPP_APP_SECRET")
        return cls(token=os.environ.get("WHATSAPP_TOKEN"),
                   verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN"),
                   app_secret=key,
                   api_version=os.environ.get("WHATSAPP_API_VERSION", "v21.0"),
                   sender_key=(key or "").encode() or secrets.token_bytes(32))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.verify_token)


def parse_answer(text: str) -> str | None:
    words = [w.strip(".,!?;:").lower() for w in text.split()]
    for w in words:
        if w in ANSWER_WORDS:
            return ANSWER_WORDS[w]
    return None


class WhatsAppBot:
    def __init__(self, live: LiveService, config: WhatsAppConfig, client: httpx.Client | None = None) -> None:
        self.live = live
        self.cfg = config
        self.client = client or httpx.Client(timeout=30.0)

    def _sender_key(self, wa_id: str) -> str:
        return hmac.new(self.cfg.sender_key, wa_id.encode(), hashlib.sha256).hexdigest()

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.token}"}

    def download_media(self, media_id: str) -> bytes:
        base = f"{self.cfg.graph_url}/{self.cfg.api_version}"
        meta = self.client.get(f"{base}/{media_id}", headers=self._auth())
        meta.raise_for_status()
        url = meta.json()["url"]
        blob = self.client.get(url, headers=self._auth())
        blob.raise_for_status()
        return blob.content

    def send_text(self, phone_number_id: str, to: str, body: str) -> None:
        url = f"{self.cfg.graph_url}/{self.cfg.api_version}/{phone_number_id}/messages"
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                   "type": "text", "text": {"preview_url": False, "body": body[:4096]}}
        self.client.post(url, headers=self._auth(), json=payload).raise_for_status()

    def handle_message(self, phone_number_id: str, msg: dict) -> str | None:
        """Process one inbound message; returns the reply text (also sent)."""
        sender = msg.get("from")
        if not sender:
            return None
        kind = msg.get("type")
        key = self._sender_key(sender)
        ts = msg.get("timestamp")
        msg_time = None
        if ts and str(ts).isdigit():
            msg_time = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
        reply: str
        if kind == "image":
            image = msg.get("image", {})
            caption = image.get("caption") or ""
            try:
                data = self.download_media(image["id"])
                out = self.live.submit_photo(data, answer=parse_answer(caption), source="whatsapp",
                                             message_time=msg_time)
                self.live.store.set_session(key, out["report"]["id"])
                r = out["report"]
                bits = []
                if r.get("turbidity") is not None:
                    bits.append(f"cloudiness index {r['turbidity']:.2f}")
                if r.get("chlorine") is not None:
                    bits.append(f"free chlorine {r['chlorine']:.2f} mg/L")
                reply = (f"Reading for {r['household']}: {', '.join(bits) or 'recorded'} — "
                         f"{(out['level'] or 'green').upper()}. {out['message']}")
                if not r.get("answer"):
                    reply += " Reply: fine, smell, dirty or ill."
            except IntakeError as e:
                if e.status == 422 and e.detail.get("issues"):
                    tips = "; ".join(i.split(": ", 1)[-1] for i in e.detail["issues"])
                    reply = f"Please take the photo again: {tips}."
                else:
                    reply = f"Sorry, we could not use that photo: {e}."
            except httpx.HTTPError:
                reply = "Sorry, we could not download that photo. Please send it again."
        elif kind in ("text", "interactive", "button"):
            if kind == "text":
                text = msg.get("text", {}).get("body", "")
            elif kind == "button":
                text = msg.get("button", {}).get("text", "")
            else:
                inter = msg.get("interactive", {})
                text = (inter.get("button_reply") or inter.get("list_reply") or {}).get("id", "")
            answer = parse_answer(text)
            rid = self.live.store.session(key)
            if answer and rid:
                self.live.set_answer(rid, answer)
                reply = f"Thanks — noted '{answer}' for your latest reading."
            else:
                reply = HELP
        else:
            reply = HELP
        self.send_text(phone_number_id, sender, reply)
        return reply

    def handle_payload(self, payload: dict) -> list[str]:
        replies = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                value = change.get("value", {})
                pnid = value.get("metadata", {}).get("phone_number_id")
                for msg in value.get("messages", []) or []:
                    if not pnid:
                        continue
                    try:
                        r = self.handle_message(pnid, msg)
                    except Exception:  # noqa: BLE001 — one bad message must not drop the rest
                        log.exception("WhatsApp message %s failed", msg.get("id"))
                        continue
                    if r:
                        replies.append(r)
        return replies


def make_router(bot: WhatsAppBot) -> APIRouter:
    router = APIRouter()

    @router.get("/whatsapp/webhook", response_class=PlainTextResponse)
    def verify(request: Request) -> str:
        q = request.query_params
        if not bot.cfg.configured:
            raise HTTPException(503, "WhatsApp is not configured")
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == bot.cfg.verify_token:
            return q.get("hub.challenge", "")
        raise HTTPException(403, "verification failed")

    @router.post("/whatsapp/webhook")
    async def receive(request: Request, background: BackgroundTasks) -> dict:
        body = await request.body()
        if bot.cfg.app_secret:
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(bot.cfg.app_secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise HTTPException(401, "bad signature")
        if not bot.cfg.configured:
            return {"status": "ignored", "reason": "WhatsApp is not configured"}
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON") from None
        background.add_task(bot.handle_payload, payload)
        return {"status": "accepted"}

    return router
