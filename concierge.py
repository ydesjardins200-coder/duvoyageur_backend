"""
concierge.py
============
A short, friendly reply for when a customer asks a GENERAL question (weather,
season, "is it all-inclusive", safety…) rather than giving trip details.

Hybrid policy: answer light general travel questions, but NEVER quote prices,
promise a rebate amount, confirm availability, book, or guarantee anything — for
those, defer to a human agent. Built to never raise.
"""
from __future__ import annotations

from typing import Optional

import anthropic

from config import settings
from trip_schema import TripRequest

CONCIERGE_SYSTEM_PROMPT = """\
Tu es l'assistant virtuel de Du Voyageur, une agence de voyages québécoise qui
retrouve à ses clients le MÊME forfait tout inclus qu'ils ont repéré, mais avec
un rabais. Un client vient de poser une question générale dans la conversation.

Ton rôle :
- Réponds brièvement et chaleureusement, en français québécois (1 à 3 phrases).
- Tu PEUX répondre aux questions générales de voyage : météo et saison, infos
  générales sur une destination, ce que veut dire « tout inclus », bagages,
  meilleur moment pour partir, etc.
- Tu NE DOIS PAS donner de prix, promettre un montant de rabais, confirmer une
  disponibilité, faire une réservation, ni garantir quoi que ce soit. Pour tout
  ça, dis simplement qu'un conseiller va revenir avec son offre personnalisée.
- N'invente jamais de détails précis dont tu n'es pas sûr ; reste général et
  honnête.
- Pas de listes ni de markdown, pas de formules d'ouverture répétitives. Parle
  comme un agent sympathique.
"""


def concierge_reply(message_text: str, trip: Optional[TripRequest] = None,
                    client: Optional[anthropic.Anthropic] = None) -> Optional[str]:
    """Return a short helpful reply to a general question, or None on failure."""
    if not (message_text and settings.ANTHROPIC_API_KEY):
        return None
    client = client or anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    context = ""
    if trip:
        bits = []
        dest = trip.destination or trip.hotel_name_raw
        when = trip.dates_raw or (trip.departure_date.isoformat() if trip.departure_date else None)
        if dest:
            bits.append(f"destination {dest}")
        if when:
            bits.append(f"dates {when}")
        if bits:
            context = "Contexte du dossier : " + ", ".join(bits) + ".\n"

    try:
        resp = client.messages.create(
            model=settings.PARSE_MODEL,
            max_tokens=300,
            system=CONCIERGE_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": context + "Question du client : " + message_text}],
        )
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
    except Exception:  # noqa: BLE001 — never break webhook processing
        return None
