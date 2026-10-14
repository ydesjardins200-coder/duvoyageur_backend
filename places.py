"""
places.py
=========
Google Places API (New) enrichment for the portal assistant's hotel cards:
licensed photos (with author attribution), Google rating, address, phone,
website, Google Maps link and coordinates. Everything is fetched live per
answer and shown with Google attribution; nothing is stored in our database
(Places terms only allow caching place IDs).

Off unless GOOGLE_PLACES_API_KEY is set. Optional GOOGLE_MAPS_EMBED_KEY (a
browser key restricted to our domains, Maps Embed + Maps Static APIs) adds the
map in the hotel panel and the « Où sont les hôtels ? » map.

Photos in the panel go through /portail/lieu-photo so the server key never
reaches the browser and a photo is only paid for when it is actually viewed.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from config import settings

log = logging.getLogger("duvoyageur.places")
router = APIRouter()

_API = "https://places.googleapis.com/v1"
_FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "location", "rating", "userRatingCount",
    "websiteUri", "nationalPhoneNumber", "internationalPhoneNumber", "googleMapsUri", "photos"))
_PHOTO_RE = re.compile(r"^places/[A-Za-z0-9_-]+/photos/[A-Za-z0-9_-]+$")
MAX_PHOTOS = 6


def enabled() -> bool:
    return bool(settings.GOOGLE_PLACES_API_KEY)


def _post(path: str, body: dict, fields: str) -> dict:
    req = urllib.request.Request(
        f"{_API}/{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
                 "X-Goog-FieldMask": fields})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


def find_hotel(name: str, area: str = "") -> dict | None:
    """Best Places match for a hotel name (+ destination), or None."""
    try:
        data = _post("places:searchText",
                     {"textQuery": f"{name} {area}".strip(), "languageCode": "fr",
                      "regionCode": "CA", "maxResultCount": 1, "includedType": "lodging"},
                     _FIELDS)
    except Exception as exc:  # noqa: BLE001 — a missing fiche is fine
        log.info("places: lookup failed for %s: %s", name, exc)
        return None
    places = data.get("places") or []
    return places[0] if places else None


def photo_uri(photo_name: str, width: int = 1200) -> str:
    """Short-lived Google-hosted URL of a place photo (one billed request)."""
    if not _PHOTO_RE.match(photo_name or ""):
        return ""
    q = urllib.parse.urlencode({"maxWidthPx": width, "skipHttpRedirect": "true",
                                "key": settings.GOOGLE_PLACES_API_KEY})
    try:
        with urllib.request.urlopen(f"{_API}/{photo_name}/media?{q}", timeout=6) as r:
            return json.loads(r.read().decode("utf-8")).get("photoUri", "")
    except Exception as exc:  # noqa: BLE001
        log.info("places: photo failed: %s", exc)
        return ""


def _attribution(photo: dict) -> str:
    names = [a.get("displayName") for a in photo.get("authorAttributions") or [] if a.get("displayName")]
    return (names[0] + " · Google") if names else "Google"


def _apply(card: dict, p: dict) -> dict:
    loc = p.get("location") or {}
    photos = [ph for ph in p.get("photos") or [] if _PHOTO_RE.match(ph.get("name", ""))][:MAX_PHOTOS]
    card["place"] = {
        "id": p.get("id", ""),
        "name": (p.get("displayName") or {}).get("text", ""),
        "address": p.get("formattedAddress", ""),
        "phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or "",
        "website": p.get("websiteUri", "") if str(p.get("websiteUri", "")).startswith("https://") else "",
        "maps": p.get("googleMapsUri", ""),
        "rating": p.get("rating"), "count": p.get("userRatingCount"),
        "lat": loc.get("latitude"), "lng": loc.get("longitude"),
    }
    google = [{"src": "/portail/lieu-photo?n=" + urllib.parse.quote(ph["name"], safe=""),
               "host": _attribution(ph), "page": card["place"]["maps"]} for ph in photos]
    # every photo goes through our proxy: Google's photo URLs expire, ours don't
    card["gallery"] = google + (card.get("gallery") or [])
    if card["gallery"]:
        card["image"] = card["gallery"][0]["src"]
        card["image_from"] = card["gallery"][0]["host"]
    return card


def enrich(cards: list) -> list:
    """Add Google Places data to each hotel card (no-op without a key)."""
    if not enabled() or not cards:
        return cards
    with ThreadPoolExecutor(max_workers=5) as ex:
        found = list(ex.map(lambda c: find_hotel(c["hotel"], c.get("destination", "")), cards))
    for c, p in zip(cards, found):
        if p:
            _apply(c, p)
    return cards


def static_map(cards: list) -> str:
    """URL of a static map with numbered markers, or "" (needs the browser key)."""
    key = getattr(settings, "GOOGLE_MAPS_EMBED_KEY", "")
    pts = [(i, c["place"]["lat"], c["place"]["lng"]) for i, c in enumerate(cards, 1)
           if (c.get("place") or {}).get("lat") is not None]
    if not key or not pts:
        return ""
    q = [("size", "640x300"), ("scale", "2"), ("maptype", "roadmap"), ("language", "fr"), ("key", key)]
    q += [("markers", f"color:0x19d3e6|label:{i}|{lat},{lng}") for i, lat, lng in pts]
    return "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(q)


def embed_url(card: dict) -> str:
    key = getattr(settings, "GOOGLE_MAPS_EMBED_KEY", "")
    pid = (card.get("place") or {}).get("id")
    if not key or not pid:
        return ""
    return ("https://www.google.com/maps/embed/v1/place?"
            + urllib.parse.urlencode({"key": key, "q": f"place_id:{pid}", "language": "fr"}))


@router.get("/portail/lieu-photo")
def place_photo(request: Request, n: str = ""):
    """Panel photos: resolve the Google photo only when the member views it."""
    from portal import current_portal_client_id      # lazy: avoids import cycle
    if not current_portal_client_id(request) or not enabled() or not _PHOTO_RE.match(n):
        return Response(status_code=404)
    uri = photo_uri(n)
    if not uri:
        return Response(status_code=404)
    return RedirectResponse(uri, status_code=302, headers={"Cache-Control": "private, max-age=600"})
