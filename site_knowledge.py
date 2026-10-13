"""
site_knowledge.py
=================
Du Voyageur's own editorial content (destination pages, top-hotels lists,
guides), loaded from data/site_knowledge.json (built by
tools/build_site_knowledge.py from the frontend repo).

Used by the portal assistant to:
- ground recommendations in verified content (catalogue + relevant excerpts),
- link members to OUR pages ("Nos guides") instead of sending traffic away,
- recognise when a suggested hotel has its own page on our site.
"""
from __future__ import annotations

import json
import pathlib
import re
import unicodedata
from functools import lru_cache

from config import settings

_PATH = pathlib.Path(__file__).resolve().parent / "data" / "site_knowledge.json"

# Intent words -> page slug fragments (top-hotels / guides / types)
_INTENTS = [
    (("famille", "enfant", "kids", "bebe", "ado"), "top-hotels-famille"),
    (("adults only", "adult only", "adultes seulement", "adulte seulement", "sans enfant",
      "lune de miel", "romanti"), "top-hotels-adults-only"),
    (("luxe", "haut de gamme", "5 etoiles", "chic"), "top-hotels-de-luxe"),
    (("pas cher", "budget", "qualite-prix", "qualite prix", "economique", "meilleur prix"), "top-hotels-qualite-prix"),
    (("quand partir", "meilleure periode", "saison", "meteo", "ouragan", "sargasse", "pluie"), "guide-meilleure-periode"),
    (("valise", "apporter", "bagage"), "guide-quoi-apporter"),
    (("passeport", "visa", "document", "e-ticket", "carte touristique"), "guide-documents-passeport"),
    (("pourboire",), "guide-pourboires"),
    (("douane", "rapporter", "alcool"), "guide-limites-douane"),
    (("devise", "argent", "pesos", "taux de change"), "guide-convertisseur-devises"),
    (("plongee", "snorkel", "tuba"), "type-plongee-snorkeling"),
    (("culture", "histoire", "ruines"), "type-culture"),
    (("aventure", "decouverte", "excursion"), "type-aventure-decouverte"),
]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def _data() -> dict:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pages": []}
    base = settings.SITE_BASE_URL.rstrip("/")
    for p in d.get("pages", []):
        p["url"] = base + "/" + p["url"].rsplit("/", 1)[-1]
        p["_kw"] = [k for k in p.get("keywords", []) if len(k) > 3]
        p["_hotels"] = [(h, norm(h)) for h in p.get("hotels", [])]
    # A country named on a destination page (e.g. "cuba") should favour the
    # country page, not every resort town in that country.
    countries = {k for p in d.get("pages", []) if p["kind"] == "pays" for k in p["_kw"]}
    for p in d.get("pages", []):
        p["_weak"] = set(p["_kw"]) & countries if p["kind"] == "destination" else set()
    return d


def pages() -> list:
    return _data().get("pages", [])


def _has(text: str, kw: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None


def match(text: str, limit: int = 3) -> list:
    """Pages relevant to `text`, best first (destinations/hotels, then intents)."""
    t = norm(text)
    try:                                   # "côte Pacifique" -> add its destinations
        import hotel_catalog as _hc
        for region, words in _hc._REGION_WORDS.items():
            if any(_has(t, _hc.norm(w)) for w in words):
                t += " " + " ".join(_hc.REGIONS[region])
    except ImportError:
        pass
    scored = []
    for p in pages():
        score = 0
        for k in p["_kw"]:
            if _has(t, k):
                score += (1 if k in p["_weak"] else
                          3 if p["kind"] in ("destination", "pays") else 2)
        for slug_words, slug in _INTENTS:
            if slug in p["url"] and any(w in t for w in slug_words):
                score += 2
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _s, p in scored[:limit]]


def catalogue() -> str:
    """One line per destination we cover — tells the model our coverage."""
    rows = []
    for p in pages():
        if p["kind"] == "destination":
            name = re.sub(r"(?i)^voyage tout-inclus (à|a|au|aux|en)\s+", "", p["title"])
            name = re.sub(r"\s*\(.*?\)", "", re.split(r"[—|·:]", name)[0]).strip()
            rows.append(f"- {name}")
    return "\n".join(rows)


def _strip_picks(p: dict) -> str:
    """Page text without our hotel picks (« Nos coups de cœur » + hotel sections)."""
    t = p["text"]
    i = re.search(r"(?i)nos coups de c.ur\n", t)
    if not i:
        return t
    hotels = {norm(h) for h in p.get("hotels", [])}
    heads = list(re.finditer(r"\n## ([^\n]*)", t))
    end = len(t)
    for m in heads:
        if m.start() <= i.start():
            continue
        h = norm(m.group(1))
        if h.startswith("meilleurs") or h in hotels or any(x and (x in h or h in x) for x in hotels):
            continue
        end = m.start()
        break
    return t[:i.start()] + t[end:]


def _strip_all_picks(p: dict) -> str:
    """No hotel picks at all: picks block, any hotel section, FAQ « coups de cœur » lines."""
    t = _strip_picks(p)
    hotels = {norm(h) for h in p.get("hotels", [])}
    out, skip = [], False
    for line in t.split("\n"):
        if line.startswith("## "):
            skip = norm(line[3:]) in hotels
        if skip or re.search(r"(?i)coups? de c.ur", line):
            continue
        out.append(line)
    return "\n".join(out)


def context_for(text: str, max_chars: int = 3200) -> str:
    """Verified excerpts for the pages that match the conversation."""
    parts = []
    for p in match(text):
        if p["kind"] == "top-hotels":
            continue                       # our hotel picks are not a source anymore
        body = _strip_all_picks(p)
        parts.append(f"### {p['title']} ({p['url']})\n{body[:max_chars]}")
    return "\n\n".join(parts)


def related_links(text: str, limit: int = 3) -> list:
    """Our pages to show under an answer as « Nos guides »."""
    return [{"url": p["url"], "title": re.sub(r"\s*\(.*?\)", "", p["title"]).strip()}
            for p in match(text, limit)]


def hotel_page(hotel: str):
    """URL of our page featuring this hotel, or None."""
    h = norm(hotel)
    if len(h) < 5:
        return None
    for p in pages():
        for _name, n in p["_hotels"]:
            if n and (n in h or h in n):
                return p["url"]
    return None
