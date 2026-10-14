"""
wholesaler_photos.py
====================
Free, official hotel photos from the wholesalers' PUBLIC image sitemaps.

Transat publishes, for search engines, a sitemap listing every hotel page with
up to 10 official photos (aerial, beach, pool, rooms…) — robots.txt allows it.
We keep an in-memory index (hotel page + photo URLs, refreshed daily) and
display the photos live from Transat's servers with attribution and a link to
the Transat hotel page. Nothing is copied: only URLs are kept, in memory.

lookup("Royalton Splash Punta Cana") -> {"page": …, "images": [...], "host": "transat.com"}
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("duvoyageur.photos")

SOURCES = [
    ("transat.com", "https://www.transat.com/api/fr-CA/sitemap/hotels"
                    "?mainPage=true&subPages=false&alternates=true&imageCount=10"),
]
_REFRESH_S = 24 * 3600
_RETRY_S = 15 * 60
_UA = ("Mozilla/5.0 (compatible; DuVoyageurBot/1.0; +https://duvoyageur.netlify.app) "
       "AppleWebKit/537.36 (KHTML, like Gecko)")
_STOP = {"hotel", "hotels", "resort", "resorts", "spa", "and", "the", "by", "all", "inclusive",
         "de", "del", "la", "le", "les", "el", "y", "et", "a", "an", "at", "collection", "suites",
         "beach", "playa", "plage", "casino", "adults", "only", "adultes"}
_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
       "i": "http://www.google.com/schemas/sitemap-image/1.1"}

_IDX: dict = {"entries": [], "by_tok": {}, "at": 0.0, "tried": 0.0, "count": 0, "error": ""}
_LOCK = threading.Lock()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def _toks(s: str) -> set:
    return {t for t in _norm(s).split() if len(t) > 1 and t not in _STOP}


def parse_sitemap(xml_bytes: bytes, host: str) -> list:
    """[{slug, toks, page, images, host}] from a sitemap with image:image tags."""
    out = []
    root = ET.fromstring(xml_bytes)
    for u in root.findall("s:url", _NS):
        loc = (u.findtext("s:loc", default="", namespaces=_NS) or "").strip()
        imgs = [(i.findtext("i:loc", default="", namespaces=_NS) or "").strip()
                for i in u.findall("i:image", _NS)]
        imgs = [x for x in imgs if x.startswith("https://")]
        if not loc.startswith("https://") or not imgs:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        out.append({"slug": slug, "toks": _toks(slug), "page": loc, "images": imgs[:10], "host": host})
    return out


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/xml,text/xml"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read(40_000_000)


def refresh() -> int:
    entries = []
    for host, url in SOURCES:
        try:
            entries += parse_sitemap(_fetch(url), host)
        except Exception as exc:  # noqa: BLE001
            log.warning("photos: sitemap %s unavailable: %s", host, exc)
            _IDX["error"] = f"{host}: {exc}"
    if entries:
        by_tok: dict = {}
        for i, e in enumerate(entries):
            for t in e["toks"]:
                by_tok.setdefault(t, []).append(i)
        with _LOCK:
            _IDX.update(entries=entries, by_tok=by_tok, at=time.time(), count=len(entries), error="")
        log.info("photos: wholesaler index loaded (%s hotels)", len(entries))
    _IDX["tried"] = time.time()
    return len(entries)


def ensure_fresh(background: bool = True) -> None:
    now = time.time()
    stale = now - _IDX["at"] > _REFRESH_S
    if not stale or now - _IDX["tried"] < (_RETRY_S if not _IDX["entries"] else 300):
        return
    _IDX["tried"] = now
    if background:
        threading.Thread(target=refresh, daemon=True).start()
    else:
        refresh()


def lookup(hotel: str, area: str = ""):
    """Best index entry for a hotel name, or None (strict: no wrong hotel)."""
    ensure_fresh()
    q = _toks(hotel)
    if not q or not _IDX["entries"]:
        return None
    area_t = _toks(area)
    cands: dict = {}
    for t in q:
        for i in _IDX["by_tok"].get(t, ()):
            cands[i] = cands.get(i, 0) + 1
    best, best_score = None, 0.0
    for i, hits in cands.items():
        e = _IDX["entries"][i]
        name_t = q - area_t or q                     # the distinctive part of the name
        core = len(name_t & e["toks"]) / len(name_t)
        cover = hits / len(q)
        if core < 1.0 and (core < 0.75 or len(name_t) < 3):
            continue                                  # every distinctive word must match (or 3/4 of many)
        extra = len(e["toks"] - q)                    # prefer the closest slug
        score = cover + core - 0.02 * extra
        if score > best_score:
            best, best_score = e, score
    return best


def status() -> dict:
    return {"hotels": _IDX["count"], "loaded_at": _IDX["at"], "error": _IDX["error"]}
