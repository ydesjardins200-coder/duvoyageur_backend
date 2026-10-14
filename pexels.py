"""
pexels.py
=========
Generic « ambiance » photos from the Pexels API: on the assistant's DESTINATION
cards (a beach in Riviera Maya…), and as a LAST-RESORT fallback on a hotel card
that has no real photo (no Transat photo, no wholesaler photo, no Google photo).
Always labelled « Photo d'ambiance » so it is never mistaken for the hotel.

Displayed live from Pexels' CDN with the photographer credit and a Pexels link,
as the Pexels API guidelines require. Results are cached in memory (one call per
destination per week). Off unless PEXELS_API_KEY is set.
Free API: 200 requests/hour, 20 000/month.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import urllib.parse
import urllib.request

from config import settings

log = logging.getLogger("duvoyageur.pexels")
_API = "https://api.pexels.com/v1/search"
_CACHE: dict = {}              # query -> (photo dict | None, expiry)
_TTL = 7 * 24 * 3600


def enabled() -> bool:
    return bool(getattr(settings, "PEXELS_API_KEY", ""))


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def query_for(destination: str) -> str:
    """« Riviera Maya / Playa del Carmen » -> « Riviera Maya beach »."""
    name = re.split(r"[/(|,–-]", destination or "")[0].strip()
    return f"{_ascii(name)} beach".strip() if name else ""


def _search_all(q: str, n: int = 10) -> list:
    req = urllib.request.Request(
        _API + "?" + urllib.parse.urlencode({"query": q, "per_page": n, "orientation": "landscape"}),
        headers={"Authorization": settings.PEXELS_API_KEY, "User-Agent": "DuVoyageur/1.0"})
    with urllib.request.urlopen(req, timeout=4) as r:
        data = json.loads(r.read().decode("utf-8"))
    out = []
    for p in data.get("photos") or []:
        src = (p.get("src") or {}).get("landscape") or (p.get("src") or {}).get("large")
        if src and str(src).startswith("https://"):
            out.append({"src": src, "photographer": p.get("photographer") or "",
                        "photographer_url": p.get("photographer_url") or "",
                        "page": p.get("url") or "https://www.pexels.com"})
    return out


def resort_photos(destination: str) -> list:
    """Up to 10 different resort/beach ambiance photos for a destination
    (one call per destination per week), so several hotels of the same
    destination get different pictures."""
    if not enabled():
        return []
    name = re.split(r"[/(|,–-]", destination or "")[0].strip()
    name = _ascii(name)
    key = "resort|" + name.lower()
    hit = _CACHE.get(key)
    if hit and hit[1] > time.time():
        return hit[0] or []
    photos: list = []
    try:
        for q in ([f"{name} resort", f"{name} beach"] if name else []) + ["tropical beach resort"]:
            photos = _search_all(q)
            if len(photos) >= 3:
                break
    except Exception as exc:  # noqa: BLE001 — a card without photo is fine
        log.info("pexels: resort search failed for %s: %s", name, exc)
        return []
    if len(_CACHE) > 1000:
        _CACHE.clear()
    _CACHE[key] = (photos, time.time() + _TTL)
    return photos


def credit(photo: dict) -> str:
    return "Photo d'ambiance · " + (photo["photographer"] + " / " if photo.get("photographer") else "") + "Pexels"


def _search(q: str):
    req = urllib.request.Request(
        _API + "?" + urllib.parse.urlencode({"query": q, "per_page": 3, "orientation": "landscape"}),
        headers={"Authorization": settings.PEXELS_API_KEY, "User-Agent": "DuVoyageur/1.0"})
    with urllib.request.urlopen(req, timeout=4) as r:
        data = json.loads(r.read().decode("utf-8"))
    for p in data.get("photos") or []:
        src = (p.get("src") or {}).get("landscape") or (p.get("src") or {}).get("large")
        if src and str(src).startswith("https://"):
            return {"src": src, "photographer": p.get("photographer") or "",
                    "photographer_url": p.get("photographer_url") or "",
                    "page": p.get("url") or "https://www.pexels.com"}
    return None


def photo_for(destination: str):
    """Ambiance photo for a destination, or None (cached, fails quietly)."""
    if not enabled():
        return None
    q = query_for(destination)
    if not q:
        return None
    hit = _CACHE.get(q)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        photo = _search(q) or _search(q.removesuffix(" beach"))
    except Exception as exc:  # noqa: BLE001 — a card without photo is fine
        log.info("pexels: search failed for %s: %s", q, exc)
        return None
    if len(_CACHE) > 1000:
        _CACHE.clear()
    _CACHE[q] = (photo, time.time() + _TTL)
    return photo
