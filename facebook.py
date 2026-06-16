"""
facebook.py
===========
Everything Messenger-specific, kept away from the app logic.

Three jobs:
1. verify_challenge  -> answer Meta's one-time GET verification handshake
2. valid_signature   -> confirm a POST really came from Meta (HMAC on the raw body)
3. extract_messages  -> pull (sender, text, image_urls) out of the webhook payload
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("duvoyageur.facebook")


def verify_challenge(mode: Optional[str], token: Optional[str], challenge: Optional[str],
                     expected_token: str) -> Optional[str]:
    """Meta calls GET /webhook once with hub.* params to confirm you own the URL."""
    if mode == "subscribe" and token == expected_token:
        return challenge
    return None


def valid_signature(raw_body: bytes, header: Optional[str], app_secret: str) -> bool:
    """
    Meta signs every POST with X-Hub-Signature-256: sha256=<hmac>.
    If no app secret is configured (local/dev), we skip the check but you should
    ALWAYS set FB_APP_SECRET in production.
    """
    if not app_secret:
        return True  # dev mode only
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


def extract_messages(payload: dict) -> list[tuple[Optional[str], str, list[str]]]:
    """Return one (sender_id, text, [image_urls]) tuple per inbound message."""
    results: list[tuple[Optional[str], str, list[str]]] = []
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo"):
                continue  # skip our own outgoing echoes
            sender = event.get("sender", {}).get("id")
            text = message.get("text", "") or ""
            image_urls = [
                a.get("payload", {}).get("url")
                for a in message.get("attachments", [])
                if a.get("type") == "image" and a.get("payload", {}).get("url")
            ]
            results.append((sender, text, image_urls))
    return results


def send_text(recipient_id: Optional[str], text: str, page_token: str,
              graph_version: str = "v21.0", timeout: int = 8) -> bool:
    """
    Send a plain-text reply to a user via the Send API.

    Built to NEVER raise: on any problem it logs and returns False, so a failed
    reply can never break webhook processing or cause Meta to retry the event.
    Only fires inside the 24-hour window (messaging_type RESPONSE).
    """
    if not (page_token and recipient_id and text):
        return False
    url = (
        f"https://graph.facebook.com/{graph_version}/me/messages"
        f"?access_token={urllib.parse.quote(page_token)}"
    )
    body = json.dumps({
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001
        log.warning("Send API reply failed: %s", e)
        return False
