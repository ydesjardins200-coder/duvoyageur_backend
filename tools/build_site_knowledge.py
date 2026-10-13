#!/usr/bin/env python3
"""
build_site_knowledge.py — turns the public site's editorial pages into
data/site_knowledge.json, the verified knowledge the portal assistant reads.

USAGE (from the backend repo):
    python3 tools/build_site_knowledge.py /path/to/duvoyageur_frontend

Re-run and commit whenever destination / top-hotels / guide pages change.
Only editorial content is kept: calls-to-action, "why us" blocks and the
old "send us your package" copy are stripped.
"""
import json
import pathlib
import re
import sys
import unicodedata
from html import unescape

SITE = "https://duvoyageur.netlify.app"
PREFIXES = ("voyage-", "pays-", "top-hotels-", "type-", "guide-", "les-iles")
MAX_CHARS = 7000


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def clean_main(html: str) -> str:
    m = re.search(r"<main[^>]*>(.*?)</main>", html, re.S)
    body = m.group(1) if m else ""
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S)
    body = re.sub(r'<section class="cta-band.*?</section>', " ", body, flags=re.S)
    body = re.sub(r'<a [^>]*class="[^"]*\bbtn\b[^"]*"[^>]*>.*?</a>', " ", body, flags=re.S)
    body = re.sub(r"<(h[1-4])[^>]*>", "\n## ", body)
    body = re.sub(r"</(p|li|h[1-4]|div|summary)>", "\n", body)
    text = unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t ]+", " ", text)
    lines = [l.strip() for l in text.splitlines()]
    out, skip = [], False
    for l in lines:
        if not l or l in ("##",):
            continue
        low = norm(l)
        # drop the marketing "why us" block (until the next practical heading)
        if low.startswith("## pourquoi nous") or low.startswith("pourquoi nous"):
            skip = True
            continue
        if skip and l.startswith("##") and not low.startswith("## le meme voyage"):
            skip = False
        if skip:
            continue
        if any(k in low for k in ("tu nous l'envoies", "envoie-nous", "on s'occupe du prix",
                                  "obtenir mon rabais", "ecris-nous sur messenger",
                                  "demande ton prix", "aucun paiement et aucune obligation")):
            continue
        out.append(l)
    txt = "\n".join(out)
    return re.sub(r"\n{2,}", "\n", txt)[:MAX_CHARS]


def keywords(name: str, title: str, hotels, eyebrow: str = "") -> list:
    slug = name.removesuffix(".html")
    for p in ("voyage-", "pays-", "top-hotels-", "type-", "guide-"):
        slug = slug.removeprefix(p)
    kw = {norm(slug.replace("-", " "))}
    base = re.sub(r"\([^)]*\)", "", re.split(r"[—|·:]", title)[0])
    base = re.sub(r"(?i)voyage tout-inclus (à|a|au|aux|en)\s+", "", base)
    kw.add(norm(base.strip()))
    for part in re.split(r"[,&/()]| et ", base):
        if len(part.strip()) > 3:
            kw.add(norm(part.strip()))
    for h in hotels:
        kw.add(norm(h))
    if eyebrow and len(eyebrow) < 40:
        kw.add(norm(eyebrow))
    return sorted(k for k in kw if len(k) > 2 and not k.isdigit())


def main(front: str):
    root = pathlib.Path(front)
    pages = []
    for f in sorted(root.glob("*.html")):
        if not f.name.startswith(PREFIXES):
            continue
        html = f.read_text(encoding="utf-8")
        title = unescape((re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1]).strip()
        title = re.sub(r"\s*—\s*Du Voyageur\s*$", "", title)
        desc = re.search(r'<meta name="description" content="([^"]*)"', html)
        hotels = [unescape(re.sub(r"<[^>]+>", "", h)).strip()
                  for h in re.findall(r'class="hotel__name"[^>]*>(.*?)</h[23]>', html, re.S)]
        kind = ("destination" if f.name.startswith("voyage-") else
                "pays" if f.name.startswith("pays-") else
                "top-hotels" if f.name.startswith("top-hotels-") else
                "guide" if f.name.startswith("guide-") else "type")
        pages.append({
            "url": f"{SITE}/{f.name}", "title": title, "kind": kind,
            "description": unescape(desc.group(1)) if desc else "",
            "hotels": hotels, "text": clean_main(html),
        })
    for pg in pages:
        first = pg["text"].split("\n", 1)[0].strip()
        eyebrow = first if not first.startswith("##") else ""
        name = pg["url"].rsplit("/", 1)[-1]
        pg["keywords"] = keywords(name, pg["title"], pg["hotels"],
                                  eyebrow if pg["kind"] == "destination" else "")
    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "site_knowledge.json"
    out.write_text(json.dumps({"site": SITE, "pages": pages}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"{len(pages)} pages -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "../duvoyageur_frontend")
