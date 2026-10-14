"""
trip_brief.py
=============
Progressive profiling for the portal travel agent.

Before spending web searches and Google Places calls on hotel research, a
cheap model (Claude Haiku) reads the conversation and extracts the trip
criteria. If an essential is missing (destination, date, length, travellers,
children's ages, departure city, budget), the member gets a short clickable
form instead of generic hotel picks. General questions (luggage, documents,
seasons) are never gated.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from html import escape

import anthropic

from config import settings

log = logging.getLogger("duvoyageur.portal.brief")

_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]

_TOOL = {
    "name": "profil_voyage",
    "description": "Critères du voyage compris dans la conversation.",
    "input_schema": {
        "type": "object",
        "required": ["intention"],
        "properties": {
            "intention": {"type": "string", "enum": ["voyage", "question", "autre"],
                          "description": "voyage = il cherche un voyage/des hôtels/des prix ; "
                                         "question = question générale (bagages, documents, météo, "
                                         "pourboires, saison) ; autre = hors sujet"},
            "destination": {"type": ["string", "null"], "description": "pays, région ou ville ; null si aucune"},
            "destination_ouverte": {"type": "boolean", "description": "il veut des idées de destination"},
            "depart": {"type": ["string", "null"], "description": "ville ou aéroport de départ"},
            "date_depart": {"type": ["string", "null"], "description": "AAAA-MM-JJ si une date précise"},
            "mois": {"type": ["string", "null"], "description": "mois/période si pas de date précise (ex. « mars 2027 », « relâche »)"},
            "nuits": {"type": ["integer", "null"]},
            "adultes": {"type": ["integer", "null"]},
            "enfants": {"type": ["integer", "null"]},
            "ages_enfants": {"type": "array", "items": {"type": "integer"}},
            "budget": {"type": ["string", "null"], "description": "budget tel que dit (« 8 000 $ en tout »), "
                                                                  "ou « aucun » s'il dit ne pas en avoir"},
        },
    },
}

_PROMPT = ("Nous sommes le {today}. Lis la conversation d'un membre avec un agent de voyage et "
           "remplis profil_voyage avec ce que le MEMBRE a dit (pas l'agent). N'invente rien : "
           "null si ce n'est pas dit. « 2 adultes et 2 enfants de 5 et 9 ans » → adultes 2, "
           "enfants 2, ages_enfants [5, 9]. « une semaine » → nuits 7. « couple » → adultes 2.")


def enabled() -> bool:
    return bool(settings.PORTAL_ASSISTANT_TRIAGE and settings.ANTHROPIC_API_KEY)


def extract(history: list, q: str) -> dict | None:
    """Trip criteria from the member's messages (cheap model, forced tool)."""
    said = [m.get("text", "") for m in history[-10:] if m.get("role") == "user"] + [q]
    convo = "\n".join(f"- {t}" for t in said if t)
    d = date.today()
    try:
        api = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        r = api.messages.create(
            model=settings.PORTAL_ASSISTANT_TRIAGE_MODEL, max_tokens=600,
            system=_PROMPT.format(today=f"{d.day} {_MONTHS[d.month - 1]} {d.year}"),
            messages=[{"role": "user", "content": "Messages du membre :\n" + convo}],
            tools=[_TOOL], tool_choice={"type": "tool", "name": _TOOL["name"]})
        for b in r.content or []:
            if getattr(b, "type", "") == "tool_use" and isinstance(getattr(b, "input", None), dict):
                return b.input
    except Exception as exc:  # noqa: BLE001 — never block the agent on triage
        log.warning("brief: triage failed, answering directly: %s", exc)
    return None


def missing(p: dict) -> list:
    """Essentials still unknown for a hotel recommendation."""
    if not p or p.get("intention") != "voyage":
        return []
    out = []
    if not (p.get("destination") or "").strip():
        out.append("destination")
    if not (p.get("depart") or "").strip():
        out.append("depart")
    if not (p.get("date_depart") or p.get("mois")):
        out.append("date")
    if not p.get("nuits"):
        out.append("nuits")
    if not p.get("adultes"):
        out.append("voyageurs")
    elif (p.get("enfants") or 0) > 0 and len(p.get("ages_enfants") or []) < (p.get("enfants") or 0):
        out.append("ages")
    if not (p.get("budget") or "").strip():
        out.append("budget")
    return out


def known_chips(p: dict) -> str:
    bits = []
    if p.get("destination"):
        bits.append(f"📍 {p['destination']}")
    if p.get("depart"):
        bits.append(f"✈️ {p['depart']}")
    if p.get("date_depart"):
        try:
            y, m, d = p["date_depart"].split("-")
            bits.append(f"🗓️ {int(d)} {_MONTHS[int(m) - 1]} {y}")
        except (ValueError, IndexError):
            pass
    elif p.get("mois"):
        bits.append(f"🗓️ {p['mois']}")
    if p.get("nuits"):
        bits.append(f"🌙 {p['nuits']} nuits")
    if p.get("adultes"):
        e = p.get("enfants") or 0
        bits.append(f"👤 {p['adultes']} adulte{'s' if p['adultes'] > 1 else ''}"
                    + (f", {e} enfant{'s' if e > 1 else ''}" if e else ""))
    if p.get("budget"):
        bits.append(f"💰 {p['budget']}")
    return "".join(f"<span class='bf-k'>{escape(str(b))}</span>" for b in bits)


def _chips(name: str, options: list) -> str:
    return "<div class='bf-opts'>" + "".join(
        f"<button type='button' class='bf-o' data-f='{name}' data-v=\"{escape(v)}\">{escape(label)}</button>"
        for label, v in options) + "</div>"


def form_html(p: dict, miss: list) -> str:
    """The short « il me manque » form (only the missing fields)."""
    today = date.today().isoformat()
    f = []
    if "destination" in miss:
        f.append("<div class='bf-f'><label>Destination</label>"
                 + _chips("destination", [("Mexique", "Mexique"), ("Punta Cana", "Punta Cana"),
                                          ("Cuba", "Cuba"), ("Jamaïque", "Jamaïque"),
                                          ("Surprends-moi", "ouvert à vos suggestions")])
                 + "<input class='bf-in' data-f='destination' placeholder='ou écris une destination'></div>")
    if "depart" in miss:
        f.append("<div class='bf-f'><label>Départ de</label>"
                 + _chips("depart", [("Montréal (YUL)", "Montréal (YUL)"), ("Québec (YQB)", "Québec (YQB)"),
                                     ("Ottawa (YOW)", "Ottawa (YOW)"), ("Toronto (YYZ)", "Toronto (YYZ)")]) + "</div>")
    if "date" in miss:
        f.append("<div class='bf-f'><label>Date de départ</label>"
                 f"<input type='date' class='bf-in' data-f='date' min='{today}'>"
                 + _chips("date", [("Dates flexibles", "dates flexibles")]) + "</div>")
    if "nuits" in miss:
        f.append("<div class='bf-f'><label>Durée</label>"
                 + _chips("nuits", [("4 nuits", "4 nuits"), ("7 nuits", "7 nuits"),
                                    ("10 nuits", "10 nuits"), ("14 nuits", "14 nuits")]) + "</div>")
    if "voyageurs" in miss or "ages" in miss:
        a = p.get("adultes") or 2
        e = p.get("enfants") or 0
        f.append("<div class='bf-f'><label>Voyageurs</label><div class='bf-pax'>"
                 f"<span>Adultes <button type='button' class='bf-m' data-k='adultes'>−</button>"
                 f"<b data-n='adultes'>{a}</b><button type='button' class='bf-p' data-k='adultes'>+</button></span>"
                 f"<span>Enfants <button type='button' class='bf-m' data-k='enfants'>−</button>"
                 f"<b data-n='enfants'>{e}</b><button type='button' class='bf-p' data-k='enfants'>+</button></span>"
                 "</div><div class='bf-ages' data-ages='" + escape(",".join(str(x) for x in p.get("ages_enfants") or []))
                 + "'></div></div>")
    if "budget" in miss:
        f.append("<div class='bf-f'><label>Budget total</label>"
                 + _chips("budget", [("Moins de 3 000 $", "moins de 3 000 $ en tout"),
                                     ("3 000 – 5 000 $", "3 000 à 5 000 $ en tout"),
                                     ("5 000 – 8 000 $", "5 000 à 8 000 $ en tout"),
                                     ("8 000 $ et plus", "8 000 $ et plus en tout"),
                                     ("Pas de budget précis", "pas de budget précis")])
                 + "<input class='bf-in' data-f='budget' placeholder='ou un montant'></div>")
    known = known_chips(p)
    return ("<div class='brief' data-miss='" + ",".join(miss) + "'>"
            "<div class='bf-h'>Pour te trouver les bons hôtels, il me manque quelques infos</div>"
            + (f"<div class='bf-known'><span class='bf-l'>J'ai compris</span>{known}</div>" if known else "")
            + "".join(f)
            + "<button type='button' class='btn bf-go'>Voir mes recommandations</button>"
              "<button type='button' class='bf-skip'>Passer : suggère-moi quand même</button></div>")


def intro_text(p: dict, miss: list) -> str:
    dest = (p.get("destination") or "").strip()
    return (f"Super, {dest} ! 🌴 " if dest else "Avec plaisir ! 🌴 ") + \
        "Pour te proposer des hôtels qui te ressemblent vraiment (et les bons prix), " \
        "j'ai besoin de quelques précisions :"
