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
import threading
import time
from datetime import date
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
# "free" = Text Search Pro fields only (photos, address, map link): the
# larger monthly free quota. "full" adds Google rating, phone and website
# (Enterprise fields: smaller free quota).
_BASIC = ("id", "displayName", "formattedAddress", "location", "googleMapsUri", "photos")
_FULL = _BASIC + ("rating", "userRatingCount", "websiteUri", "nationalPhoneNumber",
                  "internationalPhoneNumber")


def _fields() -> str:
    full = (getattr(settings, "GOOGLE_PLACES_FIELDS", "free") or "free").lower() == "full"
    return ",".join("places." + f for f in (_FULL if full else _BASIC))


# HARD caps so the bill stays at 0 $: counted in the database (survives
# restarts/redeploys), per day AND per calendar month, below Google's monthly
# free usage. Beyond a cap the call is not made and the wholesaler photo is used.
_URI_CACHE: dict = {}          # photo name -> (photoUri, expiry) — Google URLs are short-lived
_LOCK = threading.Lock()


def _caps(kind: str) -> tuple:
    full = (getattr(settings, "GOOGLE_PLACES_FIELDS", "free") or "free").lower() == "full"
    if kind == "search":
        return (int(settings.GOOGLE_PLACES_DAILY_SEARCHES or 0),
                int(settings.GOOGLE_PLACES_MONTHLY_SEARCHES or (900 if full else 4500)))
    return int(settings.GOOGLE_PLACES_DAILY_PHOTOS or 0), int(settings.GOOGLE_PLACES_MONTHLY_PHOTOS or 900)


def _allow(kind: str) -> bool:
    """Reserve one call if under both caps (atomic per process, durable in DB)."""
    from db import ApiUsage, SessionLocal                # lazy: avoids import cycle
    day_cap, month_cap = _caps(kind)
    today = date.today().isoformat()
    try:
        with _LOCK, SessionLocal() as db:
            rows = db.query(ApiUsage).filter(ApiUsage.kind == kind,
                                             ApiUsage.day.like(today[:7] + "-%")).all()
            used_month = sum(r.count for r in rows)
            row = next((r for r in rows if r.day == today), None)
            used_day = row.count if row else 0
            if (day_cap and used_day >= day_cap) or (month_cap and used_month >= month_cap):
                return False
            if row:
                row.count += 1
            else:
                db.add(ApiUsage(day=today, kind=kind, count=1))
            db.commit()
            return True
    except Exception as exc:  # noqa: BLE001 — if we cannot count, we do not call
        log.warning("places: usage counter unavailable, call skipped: %s", exc)
        return False


def usage() -> dict:
    """This month's counts and caps (for the logs / admin)."""
    from db import ApiUsage, SessionLocal
    month = date.today().isoformat()[:7]
    with SessionLocal() as db:
        rows = db.query(ApiUsage).filter(ApiUsage.day.like(month + "-%")).all()
    return {k: {"month": sum(r.count for r in rows if r.kind == k), "caps": _caps(k)}
            for k in ("search", "photo")}


_PHOTO_RE = re.compile(r"^places/[A-Za-z0-9_-]+/photos/[A-Za-z0-9_-]+$")
MAX_PHOTOS = 4


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
    if not _allow("search"):
        log.info("places: daily search cap reached")
        return None
    try:
        data = _post("places:searchText",
                     {"textQuery": f"{name} {area}".strip(), "languageCode": "fr",
                      "regionCode": "CA", "maxResultCount": 1, "includedType": "lodging"},
                     _fields())
    except Exception as exc:  # noqa: BLE001 — a missing fiche is fine
        log.info("places: lookup failed for %s: %s", name, exc)
        return None
    places = data.get("places") or []
    return places[0] if places else None


def photo_uri(photo_name: str, width: int = 1200) -> str:
    """Short-lived Google-hosted URL of a place photo (one billed request)."""
    if not _PHOTO_RE.match(photo_name or ""):
        return ""
    hit = _URI_CACHE.get(photo_name)
    if hit and hit[1] > time.time():
        return hit[0]
    if not _allow("photo"):
        log.info("places: daily photo cap reached")
        return ""
    q = urllib.parse.urlencode({"maxWidthPx": width, "skipHttpRedirect": "true",
                                "key": settings.GOOGLE_PLACES_API_KEY})
    try:
        with urllib.request.urlopen(f"{_API}/{photo_name}/media?{q}", timeout=6) as r:
            uri = json.loads(r.read().decode("utf-8")).get("photoUri", "")
            if uri:
                if len(_URI_CACHE) > 2000:
                    _URI_CACHE.clear()
                _URI_CACHE[photo_name] = (uri, time.time() + 45 * 60)
            return uri
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
    fallback = next((g["src"] for g in card.get("gallery") or [] if g["src"].startswith("https://")), "")
    google = [{"src": "/portail/lieu-photo?" + urllib.parse.urlencode(
                   {"n": ph["name"], **({"f": fallback} if fallback else {})}),
               "host": _attribution(ph), "page": card["place"]["maps"]} for ph in photos]
    # every photo goes through our proxy: Google's photo URLs expire, ours don't.
    # A real wholesaler photo of the hotel stays first (free); Google photos come
    # first only when the wholesaler had none — fewer billed photos per answer.
    own = card.get("gallery") or []
    card["gallery"] = (own + google) if own else google
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
    if not getattr(settings, "GOOGLE_MAPS_STATIC", False):
        return ""                      # billed per view beyond the free tier: off unless asked
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
def place_photo(request: Request, n: str = "", f: str = ""):
    """Hotel photos: resolve the Google photo only when the member views it;
    over the daily cap (or on error) fall back to the wholesaler photo."""
    from portal import current_portal_client_id      # lazy: avoids import cycle
    if not current_portal_client_id(request) or not _PHOTO_RE.match(n):
        return Response(status_code=404)
    uri = photo_uri(n) if enabled() else ""
    if not uri and f.startswith("https://"):
        uri = f
    if not uri:
        return Response(status_code=404)
    return RedirectResponse(uri, status_code=302, headers={"Cache-Control": "private, max-age=2400"})


@router.get("/portail/google-usage")
def google_usage(request: Request):
    """Testers only: this month's Google Places usage against our hard caps."""
    from portal import current_portal_client_id
    from db import Client, SessionLocal
    from portal_assistant import is_tester
    from fastapi.responses import HTMLResponse
    cid = current_portal_client_id(request)
    with SessionLocal() as db:
        client = db.get(Client, cid) if cid else None
        if not is_tester(client):
            return Response(status_code=404)
    u = usage()
    today = date.today().isoformat()
    from db import ApiUsage
    with SessionLocal() as db:
        day = {r.kind: r.count for r in db.query(ApiUsage).filter(ApiUsage.day == today).all()}
    rows = "".join(
        f"<tr><td>{label}</td><td>{day.get(k, 0)} / {u[k]['caps'][0] or '∞'}</td>"
        f"<td>{u[k]['month']} / {u[k]['caps'][1]}</td><td>{free}</td></tr>"
        for k, label, free in (("search", "Recherches d'hôtels", "5 000 (Pro) / 1 000 (full)"),
                               ("photo", "Photos", "1 000")))
    html = ("<!doctype html><meta charset='utf-8'><title>Google Places — usage</title>"
            "<body style='font-family:system-ui;padding:24px;background:#03121b;color:#eafcff'>"
            "<h2>Google Places — consommation</h2>"
            f"<p>Clé configurée : <b>{'oui' if enabled() else 'non'}</b> · mode : "
            f"<b>{getattr(settings, 'GOOGLE_PLACES_FIELDS', 'free')}</b></p>"
            "<table cellpadding='8' style='border-collapse:collapse' border='1'>"
            "<tr><th>Usage</th><th>Aujourd'hui / plafond</th><th>Ce mois / plafond</th>"
            f"<th>Gratuit Google / mois</th></tr>{rows}</table>"
            "<p>Au-delà d'un plafond, aucun appel n'est fait à Google (photos des grossistes à la place).</p>")
    return HTMLResponse(html)
