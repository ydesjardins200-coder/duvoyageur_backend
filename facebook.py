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
import urllib.error
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
            if message.get("quick_reply"):
                continue  # quick-reply taps are routed via extract_quick_replies
            sender = event.get("sender", {}).get("id")
            text = message.get("text", "") or ""
            image_urls = [
                a.get("payload", {}).get("url")
                for a in message.get("attachments", [])
                if a.get("type") == "image" and a.get("payload", {}).get("url")
            ]
            results.append((sender, text, image_urls))
    return results


def extract_postbacks(payload: dict) -> list[tuple[Optional[str], str]]:
    """Return one (sender_id, payload_string) tuple per postback event.

    Postbacks fire when a user taps the "Get Started" button or a structured
    button — distinct from normal text/image messages.
    """
    results: list[tuple[Optional[str], str]] = []
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            pb = event.get("postback")
            if not pb:
                continue
            sender = event.get("sender", {}).get("id")
            results.append((sender, pb.get("payload", "") or ""))
    return results


def send_text_result(recipient_id: Optional[str], text: str, page_token: str,
                     graph_version: str = "v21.0", timeout: int = 8,
                     tag: Optional[str] = None) -> tuple[bool, str]:
    """Like send_text, but returns (ok, detail). On failure, detail is the actual
    Facebook Send API error message (read from the HTTP error body) so the reason
    — closed 24 h window, missing Human Agent permission, bad token… — is visible
    instead of a bare 'HTTP 400'. Never raises.
    """
    if not page_token:
        return False, "Token de page Messenger absent (FB_PAGE_TOKEN)"
    if not recipient_id:
        return False, "Pas de PSID Messenger sur ce dossier"
    if not text:
        return False, "Message vide"
    url = (
        f"https://graph.facebook.com/{graph_version}/me/messages"
        f"?access_token={urllib.parse.quote(page_token)}"
    )
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    if tag:
        payload["messaging_type"] = "MESSAGE_TAG"
        payload["tag"] = tag
    else:
        payload["messaging_type"] = "RESPONSE"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            return ok, "" if ok else f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}"
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
            msg = (err.get("message") or "").strip()
            code = err.get("code")
            sub = err.get("error_subcode")
            if msg:
                detail = msg + (f" (code {code}" + (f"/{sub}" if sub else "") + ")"
                                if code is not None else "")
        except Exception:  # noqa: BLE001
            pass
        log.warning("Send API reply failed: %s", detail)
        return False, detail
    except Exception as e:  # noqa: BLE001
        log.warning("Send API reply failed: %s", e)
        return False, str(e)


def send_text(recipient_id: Optional[str], text: str, page_token: str,
              graph_version: str = "v21.0", timeout: int = 8, tag: Optional[str] = None) -> bool:
    """
    Send a plain-text reply to a user via the Send API.

    Built to NEVER raise: on any problem it logs and returns False, so a failed
    reply can never break webhook processing or cause Meta to retry the event.

    Without a tag this is messaging_type RESPONSE (24-hour window only). Pass
    tag="HUMAN_AGENT" for a human-composed message up to 7 days after the user's
    last message (requires the Human Agent permission on the Meta app).
    """
    ok, _ = send_text_result(recipient_id, text, page_token, graph_version, timeout, tag)
    return ok


def get_user_name(psid: Optional[str], page_token: str,
                  graph_version: str = "v21.0", timeout: int = 8) -> Optional[str]:
    """
    Look up a customer's display name from their page-scoped ID via the
    User Profile API. Returns "First Last", or None if unavailable.

    Never raises. Note: full name access can require the pages_user_profile
    permission / app review for production; in dev/sandbox it works for testers.
    """
    if not (psid and page_token):
        return None
    url = (
        f"https://graph.facebook.com/{graph_version}/{urllib.parse.quote(psid)}"
        f"?fields=first_name,last_name&access_token={urllib.parse.quote(page_token)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("User Profile lookup failed: %s", e)
        return None
    name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p).strip()
    return name or None


def set_messenger_profile(page_token: str, greeting_text: str,
                          get_started_payload: str = "GET_STARTED",
                          graph_version: str = "v21.0", timeout: int = 8) -> tuple[bool, str]:
    """
    Configure the page's Messenger welcome screen, one time, via the Messenger
    Profile API: the static greeting text (shown before the user types) and the
    "Get Started" button (taps fire a GET_STARTED postback to our webhook).

    Returns (ok, detail) — detail carries Meta's response/error for diagnostics.
    The greeting may contain {{user_first_name}}, which Messenger auto-fills.
    """
    if not page_token:
        return False, "Aucun FB_PAGE_TOKEN configuré."
    url = (
        f"https://graph.facebook.com/{graph_version}/me/messenger_profile"
        f"?access_token={urllib.parse.quote(page_token)}"
    )
    body = json.dumps({
        "greeting": [{"locale": "default", "text": greeting_text}],
        "get_started": {"payload": get_started_payload},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            detail = resp.read().decode("utf-8")
            return (200 <= resp.status < 300), detail
    except Exception as e:  # noqa: BLE001 — surface Meta's error body when present
        detail = str(e)
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
        log.warning("set_messenger_profile failed: %s", detail)
        return False, detail


def set_ice_breakers(page_token: str, ice_breakers: list[dict],
                     graph_version: str = "v21.0", timeout: int = 8) -> tuple[bool, str]:
    """
    Configure the page's Ice Breakers — the tappable question bubbles shown when
    a user opens the conversation for the first time. Each entry is
    {"question": "...", "payload": "..."}; a tap fires that payload as a postback.

    Up to 4 are allowed; questions must be <= 80 characters. Never raises.
    Returns (ok, detail) with Meta's response/error body for diagnostics.
    """
    if not page_token:
        return False, "Aucun FB_PAGE_TOKEN configuré."
    url = (
        f"https://graph.facebook.com/{graph_version}/me/messenger_profile"
        f"?access_token={urllib.parse.quote(page_token)}"
    )
    body = json.dumps({
        "ice_breakers": [{"locale": "default", "call_to_actions": ice_breakers}],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (200 <= resp.status < 300), resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 — surface Meta's error body when present
        detail = str(e)
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
        log.warning("set_ice_breakers failed: %s", detail)
        return False, detail


def extract_quick_replies(payload: dict) -> list[tuple[Optional[str], str]]:
    """Return (sender_id, payload) for each quick-reply tap. Quick replies arrive
    as MESSAGE events carrying message.quick_reply.payload (not as postbacks), so
    we pull them out here and route them like postbacks."""
    results: list[tuple[Optional[str], str]] = []
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo"):
                continue
            qr = message.get("quick_reply")
            if not qr:
                continue
            sender = event.get("sender", {}).get("id")
            results.append((sender, qr.get("payload", "") or ""))
    return results


def send_quick_replies(recipient_id: Optional[str], text: str, quick_replies: list[dict],
                       page_token: str, graph_version: str = "v21.0", timeout: int = 8) -> bool:
    """Send a message with tappable quick-reply chips. Each chip is
    {"content_type":"text","title":"...","payload":"..."}; a tap sends the payload
    back as a message event. Never raises."""
    if not (page_token and recipient_id and text and quick_replies):
        return False
    url = (
        f"https://graph.facebook.com/{graph_version}/me/messages"
        f"?access_token={urllib.parse.quote(page_token)}"
    )
    body = json.dumps({
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": text, "quick_replies": quick_replies},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001
        log.warning("Send quick replies failed: %s", e)
        return False


def set_persistent_menu(page_token: str, menu_items: list[dict],
                        graph_version: str = "v21.0", timeout: int = 8) -> tuple[bool, str]:
    """Configure the always-available hamburger (☰) menu. Each item is
    {"type":"postback","title":"...","payload":"..."}; a tap fires that payload
    as a postback. Up to 3 items at the top level. Never raises."""
    if not page_token:
        return False, "Aucun FB_PAGE_TOKEN configuré."
    url = (
        f"https://graph.facebook.com/{graph_version}/me/messenger_profile"
        f"?access_token={urllib.parse.quote(page_token)}"
    )
    body = json.dumps({
        "persistent_menu": [{
            "locale": "default",
            "composer_input_disabled": False,
            "call_to_actions": menu_items,
        }],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (200 <= resp.status < 300), resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        detail = str(e)
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
        log.warning("set_persistent_menu failed: %s", detail)
        return False, detail
