"""
hotel_catalog.py
================
Du Voyageur's curated hotel catalogue (data/hotels.json, built by
tools/build_hotel_catalog.py from the site's hotel cards) and its QUALITY
RANKING.

The assistant recommends from this ranking first instead of whatever a web
search happens to surface:
    quality = TripAdvisor rating, weighted by how many reviews back it
            + bonus if it's a Du Voyageur « coup de cœur »
            + bonus per requested category it fits (famille, adultes, luxe,
              qualité-prix)
Web search is then used only to find each hotel's CURRENT public price.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import unicodedata
from functools import lru_cache

from config import settings

_PATH = pathlib.Path(__file__).resolve().parent / "data" / "hotels.json"

CATEGORY_LABELS = {"famille": "Famille", "adultes": "Adultes seulement",
                   "luxe": "Luxe", "qualite-prix": "Qualité-prix"}
_INTENTS = {
    "famille": ("famille", "enfant", "kids", "bebe", "ado", "mini club", "glissade"),
    "adultes": ("adults only", "adult only", "adultes seulement", "adulte seulement",
                "sans enfant", "lune de miel", "voyage de noces", "romanti", "en amoureux"),
    "luxe": ("luxe", "haut de gamme", "5 etoiles", "chic", "upscale"),
    "qualite-prix": ("pas cher", "budget", "qualite prix", "qualite-prix", "economique",
                     "meilleur prix", "abordable", "moins cher"),
}
_UNRATED = 4.0          # assumed rating for hotels we list but haven't rated yet


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


@lru_cache(maxsize=1)
def hotels() -> list:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8")).get("hotels", [])
    except (OSError, ValueError):
        return []
    base = settings.SITE_BASE_URL.rstrip("/")
    for h in data:
        for k in ("page",):
            if h.get(k):
                h[k] = base + "/" + h[k].rsplit("/", 1)[-1]
        h["photos"] = [base + "/assets/" + p.rsplit("/", 1)[-1] for p in h.get("photos", [])]
        h["_name"] = norm(h["name"])
        parts = []
        for field in (h.get("destination", ""), h.get("country", ""), h.get("location", "")):
            parts += re.split(r"[,·/&()]|\bet\b|\bpres d", field or "")
        h["_places"] = {p for p in (norm(x) for x in parts) if len(p) > 2}
    return data


def by_id(hid: str):
    return next((h for h in hotels() if h["id"] == hid), None)


def find(name: str):
    """Catalogue hotel matching a hotel name (as written by the model)."""
    n = norm(name)
    if len(n) < 5:
        return None
    best = None
    for h in hotels():
        if h["_name"] == n:
            return h
        if h["_name"] in n or n in h["_name"]:
            if not best or len(h["_name"]) > len(best["_name"]):
                best = h
    return best


def quality(h: dict) -> float:
    """Rating weighted by review volume: 4.5 on 30 000 reviews beats 4.8 on 200."""
    r = h.get("rating") or _UNRATED
    n = h.get("reviews")
    confidence = min(1.0, math.log10(n + 1) / 4) if n else 0.55   # 10 000 reviews = full weight
    return r * (0.75 + 0.25 * confidence) + (0.25 if h.get("coup_de_coeur") else 0.0)


def _has(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def intents(text: str) -> set:
    t = norm(text)
    return {cat for cat, words in _INTENTS.items() if any(_has(t, norm(w)) for w in words)}


def rank(text: str, limit: int = 8) -> list:
    """Best catalogue hotels for the conversation, best first.
    Destination named -> hotels there; otherwise -> the best overall."""
    t = norm(text)
    wanted = intents(text)
    named = [h for h in hotels() if _has(t, h["_name"])]
    local = [h for h in hotels() if any(_has(t, p) for p in h["_places"])]
    pool = local or hotels()
    scored = []
    for h in pool:
        s = quality(h) + 0.45 * len(wanted & set(h.get("categories", [])))
        if "qualite-prix" in wanted:
            if h.get("price_tier"):
                s += 0.15 * (3 - h["price_tier"])
            if "luxe" in h.get("categories", []) and "luxe" not in wanted:
                s -= 0.5                                   # budget ask: not the luxury picks
        if "adultes" in wanted and "famille" in h.get("categories", []) \
                and "adultes" not in h.get("categories", []):
            s -= 0.6                                   # don't push a kids resort to a honeymoon
        if "famille" in wanted and "adultes" in h.get("categories", []) \
                and "famille" not in h.get("categories", []):
            s -= 2.0                                   # adults-only can't take kids
        scored.append((s, h))
    scored.sort(key=lambda x: -x[0])
    out = [h for _s, h in scored]
    for h in reversed(named):                          # hotels named by the member go first
        if h in out:
            out.remove(h)
        out.insert(0, h)
    return out[:limit]


def _fmt_int(n) -> str:
    return f"{n:,}".replace(",", " ")


def prompt_lines(text: str, limit: int = 8) -> str:
    rows = []
    for i, h in enumerate(rank(text, limit), 1):
        bits = [f"{i}. {h['name']} — {h['destination']}"]
        if h.get("rating"):
            bits.append(f"TripAdvisor {str(h['rating']).replace('.', ',')}/5"
                        + (f" ({_fmt_int(h['reviews'])} avis)" if h.get("reviews") else ""))
        else:
            bits.append("pas encore noté")
        cats = [CATEGORY_LABELS[c] for c in h.get("categories", []) if c in CATEGORY_LABELS]
        if cats:
            bits.append(", ".join(cats))
        specs = ", ".join(x for x in (
            f"{h['chambres']} chambres" if h.get("chambres") else "",
            f"{h['restos']} restos" if h.get("restos") else "",
            f"{h['piscines']} piscines" if h.get("piscines") else "") if x)
        if specs:
            bits.append(specs)
        if h.get("price_tier"):
            bits.append("prix " + "$" * h["price_tier"])
        if h.get("coup_de_coeur"):
            bits.append("COUP DE CŒUR Du Voyageur")
        if h.get("description"):
            bits.append(h["description"])
        rows.append(" · ".join(bits))
    return "\n".join(rows)


def card_extras(name: str) -> dict:
    """What the catalogue adds to an assistant card for this hotel."""
    h = find(name)
    if not h:
        return {}
    return {"id": h["id"], "photo": (h.get("photos") or [""])[0], "rating": h.get("rating"),
            "reviews": h.get("reviews"), "page": h.get("page"),
            "coup_de_coeur": bool(h.get("coup_de_coeur")),
            "categories": h.get("categories", [])}
