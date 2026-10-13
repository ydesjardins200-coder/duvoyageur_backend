#!/usr/bin/env python3
"""
build_hotel_catalog.py — extracts every hotel card from the public site into
data/hotels.json, the curated catalogue the portal assistant recommends from
and the comparator shows side by side.

USAGE (from the backend repo):
    python3 tools/build_hotel_catalog.py /path/to/duvoyageur_frontend

Re-run and commit whenever hotel cards change on the site.

Per hotel: name, destination, country, location, badge, description, rooms /
restaurants / pools / year, TripAdvisor rating + review count + link, official
link, our real hotel photos (generic "Photo d'ambiance" / Pexels images are
EXCLUDED), our page, and categories (famille, adultes, luxe, qualite-prix)
from the badge and from the "Nos préférés" lists of the top-hotels pages.
"""
import json
import pathlib
import re
import sys
import unicodedata
from html import unescape

SITE = "https://duvoyageur.netlify.app"
TOP_PAGES = {
    "top-hotels-famille.html": "famille",
    "top-hotels-adults-only.html": "adultes",
    "top-hotels-de-luxe.html": "luxe",
    "top-hotels-qualite-prix.html": "qualite-prix",
}
BADGE_CATS = [
    (("famille", "enfant"), "famille"),
    # "Couples" is NOT adults-only (many family resorts are great for couples):
    # only explicit adult wording maps to the adults-only category.
    (("adulte", "adults", "18 ans", "sans enfant"), "adultes"),
    (("luxe", "chic", "haut de gamme"), "luxe"),
    (("qualité-prix", "qualite-prix", "budget", "abordable"), "qualite-prix"),
]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def first(pattern: str, html: str, flags=re.S) -> str:
    m = re.search(pattern, html, flags)
    return m.group(1) if m else ""


def num(s: str):
    m = re.search(r"(\d[\d\s  ]*)", s or "")
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def parse_card(art: str, page: str, dest: str, country: str) -> dict:
    name = text(first(r'class="hotel__name"[^>]*>(.*?)</h[23]>', art))
    specs = {}
    for li in re.findall(r"<li>(.*?)</li>", first(r'<ul class="hotel__specs">(.*?)</ul>', art), re.S):
        t = text(li)
        low = t.lower()
        if "chambre" in low:
            specs["chambres"] = num(t)
        elif "resto" in low:
            specs["restos"] = num(t)
        elif "piscine" in low:
            specs["piscines"] = num(t)
        elif re.search(r"\b(19|20)\d\d\b", t):
            specs["annee"] = int(re.search(r"\b(19|20)\d\d\b", t).group(0))
    rating = first(r'--rate:([\d.]+)', art)
    reviews_txt = text(first(r'class="hotel__ta"[^>]*>(.*?)</a>', art))
    photos = []
    for src, alt, aria in re.findall(
            r'<button class="shot-open" data-src="([^"]+)"(?:[^>]*aria-label="([^"]*)")?[^>]*>'
            r'(?:<img[^>]*alt="([^"]*)")?', art):
        label = (alt + " " + aria).lower()
        if "pexels" in src.lower() or "ambiance" in label:
            continue                       # never generic stock photos
        photos.append(f"{SITE}/{src.lstrip('/')}")
    badge = text(first(r'class="hotel__badge"[^>]*>(.*?)</span>', art))
    return {
        "name": name,
        "destination": dest,
        "country": country,
        "location": text(first(r'class="hotel__loc"[^>]*>(.*?)</span>', art)),
        "badge": badge,
        "coup_de_coeur": "coup de c" in badge.lower(),
        "description": text(first(r'class="hotel__desc"[^>]*>(.*?)</p>', art)),
        **specs,
        "rating": float(rating) if rating else None,
        "reviews": num(reviews_txt.replace("TripAdvisor", "")) if "avis" in reviews_txt else None,
        "tripadvisor_url": first(r'class="hotel__ta" href="([^"]+)"', art) or None,
        "official_url": first(r'class="hotel__link" href="([^"]+)"', art) or None,
        "photos": photos[:6],
        "page": f"{SITE}/{page}",
        "categories": sorted({c for words, c in BADGE_CATS if any(w in badge.lower() for w in words)}
                             | ({"adultes"} if re.search(r"(?i)réservé aux (adultes|couples)|adultes seulement|adults[ -]only",
                                                         text(first(r'class="hotel__desc"[^>]*>(.*?)</p>', art)))
                                else set())),
    }


def main(front: str):
    root = pathlib.Path(front)
    hotels: dict = {}
    # Country = the eyebrow line above each destination page title, as indexed
    # by build_site_knowledge.py (run that first).
    eyebrows = {}
    kb = pathlib.Path(__file__).resolve().parent.parent / "data" / "site_knowledge.json"
    if kb.exists():
        for pg in json.loads(kb.read_text(encoding="utf-8"))["pages"]:
            line = pg["text"].split("\n", 1)[0].strip()
            if pg["kind"] == "destination" and not line.startswith("##") and len(line) < 40:
                eyebrows[pg["url"].rsplit("/", 1)[-1]] = line
    files = sorted(root.glob("*.html"), key=lambda f: (not f.name.startswith("voyage-"), f.name))
    for f in files:                          # destination pages first: they win on merge
        html = f.read_text(encoding="utf-8")
        arts = re.findall(r'<article class="hotel[ "][^>]*>(.*?)</article>', html, re.S)
        if not arts:
            continue
        title = re.sub(r"\s*—\s*Du Voyageur\s*$", "", unescape(first(r"<title>(.*?)</title>", html)))
        dest = re.sub(r"(?i)^voyage tout-inclus (à|a|au|aux|en)\s+", "", title)
        dest = re.sub(r"\s*\(.*?\)", "", re.split(r"[—|·:]", dest)[0]).strip()
        country = eyebrows.get(f.name, "")
        for art in arts:
            h = parse_card(art, f.name, dest, country)
            if not h["name"]:
                continue
            loc = h["location"]
            if re.fullmatch(r"\$+", loc or ""):           # "$$" = price tier, not a place
                h["price_tier"], h["location"] = len(loc), ""
            if not f.name.startswith("voyage-"):
                # Lists (top hotels, islands): the card's location is the place.
                if re.search(r"[&·…]", loc or ""):
                    continue                              # a hotel chain, not one hotel
                h["destination"] = h["location"] or ""
            if not h["country"] and "," in h["destination"]:
                h["country"] = h["destination"].rsplit(",", 1)[1].strip()
            key = norm(h["name"])
            if key in hotels:                      # keep the richest version
                old = hotels[key]
                for k, v in h.items():
                    if k == "categories":
                        old[k] = sorted(set(old[k]) | set(v))
                    elif not old.get(k) and v:
                        old[k] = v
            else:
                hotels[key] = h
    # categories from the "Nos préférés" lists of the top-hotels pages
    for page, cat in TOP_PAGES.items():
        p = root / page
        if not p.exists():
            continue
        for fave in re.findall(r'<span class="fave">(.*?)</span>', p.read_text(encoding="utf-8")):
            fn = norm(text(fave))
            for key, h in hotels.items():
                if fn and (fn == key or fn in key or key in fn):
                    h["categories"] = sorted(set(h["categories"]) | {cat})
    # Hotels listed only on a top page with a price tier: infer the destination
    # from a known destination named in the hotel's name or description.
    known = sorted({(h["destination"], h["country"]) for h in hotels.values()
                    if h["destination"]}, key=lambda x: -len(x[0]))
    for h in hotels.values():
        if h["destination"]:
            continue
        hay = norm(h["name"] + " " + h["description"])
        for dest, country in known:
            place = norm(dest.split(",")[0])
            if place and re.search(r"\b" + re.escape(place) + r"\b", hay):
                h["destination"], h["country"] = dest, country
                break
    out_list = sorted(hotels.values(), key=lambda h: (h["destination"], h["name"]))
    for h in out_list:
        h["id"] = re.sub(r"\s+", "-", norm(h["name"]))
    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "hotels.json"
    out.write_text(json.dumps({"site": SITE, "hotels": out_list}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    rated = sum(1 for h in out_list if h["rating"])
    with_photos = sum(1 for h in out_list if h["photos"])
    print(f"{len(out_list)} hotels -> {out} | rated: {rated} | with photos: {with_photos}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "../duvoyageur_frontend")
