"""
parser.py
=========
Wraps Claude with forced tool-use to turn a message (text and/or a screenshot)
into a validated TripRequest.

Cost shape (mid-2026): the primary pass runs on Haiku 4.5 (~5x cheaper than
Sonnet) with screenshots downscaled to save vision tokens. If Haiku reports low
confidence, we retry once on a stronger model with a higher-res image — so the
common case is cheap and fast, and only the hard cases pay for Sonnet.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

import anthropic

from config import settings
from prompts import PARSE_SYSTEM_PROMPT
from trip_schema import TripRequest

log = logging.getLogger("duvoyageur.parser")

# Tool schema is generated from the Pydantic model so validator + model can't drift.
TRIP_TOOL = {
    "name": "record_trip_request",
    "description": "Enregistre le forfait du client sous forme structurée.",
    "input_schema": TripRequest.model_json_schema(),
}


def _economical_image(img_bytes: bytes, media_type: str, max_edge: int):
    """Downscale a screenshot so its long edge ≤ max_edge, re-encoding to JPEG.
    Vision tokens scale with pixel area, so this is where the saving is. Falls
    back to the original bytes on any error — never blocks a parse."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(img_bytes))
        im = im.convert("RGB")
        w, h = im.size
        long_edge = max(w, h)
        if long_edge > max_edge:
            scale = max_edge / float(long_edge)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception as e:  # noqa: BLE001 — degrade gracefully to the original
        log.warning("Image downscale failed (%s); sending original", e)
        return img_bytes, (media_type or "image/png")


def _run_parse(client, message_text, images, model, max_edge) -> TripRequest:
    content: list = []
    for img_bytes, media_type in (images or []):
        data, mt = _economical_image(img_bytes, media_type, max_edge)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mt,
                "data": base64.standard_b64encode(data).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": message_text or "(capture d'écran jointe)"})

    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=PARSE_SYSTEM_PROMPT,
        tools=[TRIP_TOOL],
        tool_choice={"type": "tool", "name": "record_trip_request"},
        messages=[{"role": "user", "content": content}],
    )
    tool_use = next(b for b in resp.content if b.type == "tool_use")
    trip = TripRequest.model_validate(tool_use.input)
    if not trip.raw_message:
        trip.raw_message = message_text
    return trip


def parse_trip(
    message_text: str,
    images: Optional[list] = None,
    client: Optional[anthropic.Anthropic] = None,
) -> TripRequest:
    """Parse a message + optional screenshots into a TripRequest.

    `images` is a list of (bytes, media_type) tuples — a deal is sometimes split
    across several screenshots (flight + hotel), so we pass them all to the model.

    Primary pass: Haiku 4.5 on downscaled images. If confidence is below
    PARSE_FALLBACK_CONF, retry once on PARSE_FALLBACK_MODEL with larger images.
    """
    client = client or anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    trip = _run_parse(client, message_text, images,
                      settings.PARSE_MODEL, settings.PARSE_IMG_MAX_EDGE)

    fb = settings.PARSE_FALLBACK_MODEL
    if (fb and fb != settings.PARSE_MODEL
            and (trip.parse_confidence or 0.0) < settings.PARSE_FALLBACK_CONF):
        try:
            log.info("Low confidence (%.2f) on %s — retrying on %s",
                     trip.parse_confidence or 0.0, settings.PARSE_MODEL, fb)
            trip = _run_parse(client, message_text, images,
                              fb, settings.PARSE_FALLBACK_EDGE)
        except Exception as e:  # noqa: BLE001 — keep the primary result on failure
            log.warning("Fallback parse on %s failed (%s); keeping primary", fb, e)

    return trip
