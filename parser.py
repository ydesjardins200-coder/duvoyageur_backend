"""
parser.py
=========
Wraps Claude with forced tool-use to turn a message (text and/or a screenshot)
into a validated TripRequest. Same approach as the standalone prototype, adapted
to accept raw image bytes (so the webhook can pass a downloaded attachment).
"""
from __future__ import annotations

import base64
from typing import Optional

import anthropic

from config import settings
from prompts import PARSE_SYSTEM_PROMPT
from trip_schema import TripRequest

# Tool schema is generated from the Pydantic model so validator + model can't drift.
TRIP_TOOL = {
    "name": "record_trip_request",
    "description": "Enregistre le forfait du client sous forme structurée.",
    "input_schema": TripRequest.model_json_schema(),
}


def parse_trip(
    message_text: str,
    image_bytes: Optional[bytes] = None,
    image_media_type: str = "image/png",
    client: Optional[anthropic.Anthropic] = None,
) -> TripRequest:
    client = client or anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    content: list = []
    if image_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type,
                "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": message_text or "(capture d'écran jointe)"})

    resp = client.messages.create(
        model=settings.PARSE_MODEL,
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
