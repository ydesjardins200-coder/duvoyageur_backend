"""
portal_assistant.py
===================
The portal home's AI travel assistant ("Ton assistant voyage") + the package
search page it sends people to.

What it does
- Answers travel questions and, when the member describes a trip (budget,
  dates, travellers, priorities), searches the web (Claude's server-side
  web_search tool) and suggests 2-3 concrete options with the PUBLIC price seen
  and its source.
- Always closes by steering to our engine (/portail/recherche), where the
  Rabais Récompense is already applied — "souvent moins cher", never a promise.
- Search is restricted to tour-operator / information sites (no competing
  agencies), at most 3 searches per answer and a per-member daily search cap;
  past the cap, or if the model/tool is unavailable, it answers without search.

Wire format of /portail/assistant: NDJSON lines
  {"t":"status","v":"…"}        transient status ("Je cherche…")
  {"t":"text","v":"…"}          text delta
  {"t":"sources","v":[{"url","title"}]}
Built so an API failure never breaks the page.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin
from datetime import datetime
from html import escape
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import secrets

import anthropic
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from config import settings
from db import AssistantFeedback, Client, SessionLocal
import site_knowledge as kb
import hotel_catalog as hc
from portal import (_bell_panel, _logged_out, _nav, _set_session_cookie, _shell,
                    current_portal_client_id)

router = APIRouter()
log = logging.getLogger("duvoyageur.portal.assistant")

_KEEP = 60                  # messages stored per client
_CONTEXT = 16               # messages sent to the model (≈ 8 exchanges)
_MAX_LEN = 1000             # characters per client message
_DAY = 86400
_HITS: dict = {}            # client id -> [monotonic ts] of questions (single replica)
_SEARCHES: dict = {}        # client id -> [monotonic ts] of web searches

SEARCH_URL = "/portail/recherche"
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]

SYSTEM_PROMPT = """\
Tu es l'agent de voyage de Du Voyageur, une agence québécoise (permis du
Québec 700495). Tu parles à un membre connecté à son espace client.
Nous sommes le {today}.

Comment fonctionne Du Voyageur : le membre cherche et réserve LUI-MÊME son
forfait dans notre moteur de réservation (bouton « Rechercher un forfait »).
Son Rabais Récompense (jusqu'à 5 % sur le prix avant taxes) est déjà appliqué
aux prix affichés dans ce moteur.

TA MISSION : conseiller comme le meilleur agent de voyage que le membre ait
connu — quelqu'un qui comprend ce qu'il veut vivre, connaît les destinations
et les hôtels, et l'aide à choisir. Pas un moteur de prix.

1. DONNE DE LA VALEUR D'ABORD, POSE TES QUESTIONS À LA FIN.
   Même si la demande est incomplète, réponds tout de suite avec de vraies
   recommandations, puis termine par 1 à 3 questions à choix (bloc
   <questions>) pour affiner. Ne te contente JAMAIS de poser des questions.
   Seule exception : une demande vraiment vide (« je veux partir ») — alors
   propose 3 grandes idées variées ET pose tes questions.

2. LIS ENTRE LES LIGNES. Traduis les envies en critères : « bonne bouffe » =
   restos à la carte réputés, gastronomie locale ; « plein d'activités » =
   excursions, nature, sports nautiques, animation ; « tranquille » = petit
   complexe, adultes seulement ; « avec les enfants » = mini-club, plage en
   pente douce, glissades. Regarde les DATES : fête (Noël, Nouvel An, relâche)
   → ambiance et réservations ; saison (baleines dans le Pacifique mexicain de
   décembre à mars, ouragans de juin à novembre, sargasses côté Caraïbes
   mexicaines au printemps et à l'été, variables d'une année à l'autre). Dis
   ce que ses dates lui apportent de spécial. Ne présente comme certain que ce
   qui est bien établi ou confirmé par tes sources ou notre contenu.

3. STRUCTURE TA RÉPONSE selon la demande, avec des sections courtes
   (une ligne en **gras** comme titre de section) :
   a) Demande large (une région, « le Sud », « le Pacifique mexicain »,
      aucune destination précise) :
      - une phrase qui montre que tu as compris ce qu'il veut vivre ;
      - **Mes destinations** : écris [[DESTINATIONS]] seul sur sa ligne
        (les cartes s'affichent là) puis, pour chacune, 1-2 phrases de
        conseil (pourquoi elle colle, son bémol honnête) ;
      - **Les hôtels que je regarderais** : écris [[HOTELS]] seul sur sa
        ligne, puis une ligne par hôtel (pour qui, ce qui le distingue) ;
      - **Ton séjour** : ce que tu ferais pendant ses dates, selon ses envies
        (activités, sorties, moments forts comme le 31 décembre) ;
      - **Vols** : ce que tu sais du vol direct depuis sa ville, honnêtement ;
      - puis le bloc <questions>.
   b) Destination choisie : saute « Mes destinations » ; va aux hôtels
      (avec prix si tu as ses dates et le nombre de voyageurs), au séjour,
      aux vols et aux questions qui restent.
   c) Question générale (saison, bagages, pourboires, documents) : réponse
      claire et structurée, sans cartes, sans recherche de prix.
   d) Suivi (« moins cher », « et Cuba? », réponse à tes questions) : ne
      répète pas tout ; ajuste ce qui change.

4. HÔTELS : recommande selon la QUALITÉ. La section « HÔTELS RECOMMANDÉS PAR
   DU VOYAGEUR » (plus bas, quand elle est présente) les classe du meilleur
   au moins bon pour cette demande. Prends-les d'abord, dans cet ordre, s'ils
   collent aux critères, et écris leur nom EXACT. Un hôtel hors de cette liste
   seulement s'il manque d'options, et dis-le. Mentionne la note TripAdvisor
   quand elle aide à choisir.

5. PRIX : {search_rule}
   Ne cherche les prix que lorsque la destination est assez précise ET que tu
   as les dates et les voyageurs ; sinon, conseille sans prix. Les prix
   publics changent vite : « vu en ligne », jamais garantis. Quand tu donnes
   des prix, termine par : « Ces prix sont des prix publics. Cherche le même
   forfait dans notre moteur : c'est souvent moins cher, avec ton Rabais
   Récompense déjà appliqué. »

6. Utilise le CONTENU VÉRIFIÉ DE DU VOYAGEUR (destinations, extraits de nos
   pages « Quoi faire », guides) en priorité. Ne le contredis jamais. Les
   liens vers nos pages s'affichent automatiquement : ne les écris pas.

Règles strictes :
- Ne promets jamais que notre prix sera plus bas ni un montant de rabais
  précis : « souvent moins cher », « jusqu'à 5 % avant taxes ».
- Tu ne peux pas réserver ni confirmer une disponibilité.
- N'invente jamais un hôtel, un prix, un horaire ou un détail. Un prix vient
  de tes résultats de recherche ; un détail d'hôtel, de tes sources ou de
  notre contenu.
- Ne recommande pas d'autres agences de voyages.
- Passeports, visas, santé, avis aux voyageurs (Cuba surtout) : idée
  générale et renvoi à voyage.gc.ca.
- Réservation existante, changement, annulation, envie de parler à un
  humain : bouton « Parler à un conseiller » ou Messenger (onglet « Aide »).
- Hors voyage : ramène gentiment la conversation au voyage.

BLOCS DE DONNÉES — à la TOUTE FIN de ta réponse, dans cet ordre, sans rien
après, jamais mentionnés dans ton texte :
<destinations>[{{"nom": "Puerto Vallarta", "accroche": "Gastronomie, excursions et vie nocturne",
"ideal_pour": "un couple qui veut bouger et bien manger",
"bemol": "plus animé que reposant", "moment": "baleines et Nouvel An festif"}}]</destinations>
   (seulement avec [[DESTINATIONS]] ; 3 ou 4 destinations ; textes courts)
<pistes>[{{"hotel": "Nom exact", "destination": "Puerto Vallarta",
"depart": "31 décembre 2026", "nuits": 7, "prix_personne": null,
"total": null, "voyageurs": "2 adultes", "taxes": null,
"source_url": null, "a_confirmer": "vol direct"}}]</pistes>
   (seulement avec [[HOTELS]] ou des prix ; 3 ou 4 hôtels ; prix,
   source_url et taxes à null tant que tu n'as pas cherché le prix ;
   source_url = l'adresse exacte de la page où tu as vu le prix ;
   taxes "incluses" ou "en sus" ; montants en dollars sans symbole)
<questions>[{{"q": "Budget total pour 2, vols compris ?",
"options": ["3 500 – 5 000 $", "5 000 – 7 000 $", "7 000 – 10 000 $", "10 000 $ et plus"]}},
{{"q": "Quel type de séjour ?", "options": ["Tout inclus avec excellente bouffe",
"Hôtel de luxe et restos à découvrir", "Ouvert aux deux"]}}]</questions>
   (1 à 3 questions qui changent vraiment ta recommandation ; 2 à 4
   options courtes chacune ; jamais une question déjà répondue)
<suivis>["Moins cher", "Seulement avec vol direct"]</suivis>
   (2 ou 3 raffinements rédigés comme SA demande, moins de 40 caractères)

Style : français québécois chaleureux, tu tutoies, riche mais lisible :
phrases courtes, sections, listes avec « - » quand ça aide. **Gras** pour
les titres de section et les noms. Pas de titres avec #, pas de tableaux,
pas de séparateurs ---, pas de liens dans le texte. Deux ou trois emojis
au maximum.

CONTENU VÉRIFIÉ DE DU VOYAGEUR
Destinations que nous couvrons :
{catalogue}
{excerpts}
"""

# Appended when PORTAL_ASSISTANT_STYLE=concis.
STYLE_CONCIS = """
LONGUEUR : CONCISE (remplace les consignes de longueur ci-dessus) : sections
très courtes, 2 ou 3 destinations, 2 ou 3 hôtels, une phrase chacune.
"""

_SEARCH_ON = ("tu peux chercher sur le web (limité aux sites des voyagistes et "
              "d'information) : le prix public actuel des hôtels que tu recommandes, "
              "les vols directs, les événements à ses dates.")
_SEARCH_OFF = ("pas de recherche web pour cette réponse : conseille avec nos hôtels et "
               "notre contenu, SANS prix, et envoie-le vers notre moteur pour les prix.")

_SUGGESTIONS = [
    "2 adultes, 4 000 $ max, priorité plage, départ de Montréal en novembre",
    "Quand partir au Mexique?",
    "Quelle destination pour une famille avec des petits?",
    "Quoi mettre dans ma valise pour le Sud?",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _first_name(client) -> str:
    return ((client.kyc or {}).get("legal_first_name")
            or (client.display_name or "").split(" ")[0] or "")


def _recent(store: dict, cid: int) -> list:
    now = time.monotonic()
    hits = [t for t in store.get(cid, []) if now - t < _DAY]
    store[cid] = hits
    return hits


def _rate_ok(cid: int) -> bool:
    return len(_recent(_HITS, cid)) < settings.PORTAL_ASSISTANT_DAILY_MAX


def _searches_left(cid: int) -> int:
    return max(0, settings.PORTAL_ASSISTANT_DAILY_SEARCHES - len(_recent(_SEARCHES, cid)))


def _today() -> str:
    d = datetime.now(ZoneInfo("America/Toronto"))
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def render_md(text: str) -> str:
    """Tiny, safe markdown: escape everything, then **bold** and '- ' bullets.
    Mirrors the JS renderer in the panel so stored and live replies match."""
    out = escape(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^\s*[-*]\s+", "• ", out)
    out = re.sub(r"(?m)^#{1,6}\s*", "", out)
    out = re.sub(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"(?m)^\s*\[\[(DESTINATIONS|HOTELS)\]\]\s*$",
                 lambda m: f"<div class='slot' data-slot='{m.group(1).lower()}'></div>", out)
    return out


def _sources_html(sources) -> str:
    if not sources:
        return ""
    links = "".join(
        f"<a href=\"{escape(s['url'])}\" target='_blank' rel='noopener nofollow'>"
        f"{escape(s.get('title') or urlparse(s['url']).netloc)}</a>"
        for s in sources if str(s.get("url", "")).startswith("https://"))
    return f"<div class='srcs'><span>Sources</span>{links}</div>" if links else ""


_PISTES_RE = re.compile(r"<pistes>(.*?)(?:</pistes>|$)", re.S)
_SUIVIS_RE = re.compile(r"<suivis>(.*?)(?:</suivis>|$)", re.S)
_DEST_RE = re.compile(r"<destinations>(.*?)(?:</destinations>|$)", re.S)
_QUEST_RE = re.compile(r"<questions>(.*?)(?:</questions>|$)", re.S)
_TAGS = ("<destinations>", "<pistes>", "<questions>", "<suivis>")
_TAG_MAX = max(len(t) for t in _TAGS)


def _json_list(raw: str) -> list:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def parse_destinations(raw: str) -> list:
    out = []
    for d in _json_list(raw):
        if not isinstance(d, dict) or not str(d.get("nom") or "").strip():
            continue
        nom = str(d["nom"]).strip()[:60]
        guide = next(iter(kb.match(nom, 1)), None)
        photo = next(((h["photos"][0], h["name"]) for h in hc.rank(nom, 12) if h.get("photos")
                      and kb.norm(nom.split(",")[0]) in kb.norm(h.get("destination", "") + " " + h.get("country", ""))),
                     ("", ""))
        out.append({
            "nom": nom,
            "accroche": str(d.get("accroche") or "")[:90],
            "ideal_pour": str(d.get("ideal_pour") or "")[:120],
            "bemol": str(d.get("bemol") or "")[:120],
            "moment": str(d.get("moment") or "")[:120],
            "guide": guide["url"] if guide and guide.get("kind") in ("destination", "pays") else "",
            "photo": photo[0], "photo_hotel": photo[1],
        })
        if len(out) == 4:
            break
    return out


def parse_questions(raw: str) -> list:
    out = []
    for d in _json_list(raw):
        if not isinstance(d, dict):
            continue
        q = str(d.get("q") or "").strip()[:120]
        opts = [str(o).strip()[:60] for o in (d.get("options") or []) if str(o).strip()][:5]
        if q and len(opts) >= 2:
            out.append({"q": q, "options": opts})
        if len(out) == 3:
            break
    return out


def destinations_html(dests) -> str:
    if not dests:
        return ""
    cards = []
    for i, d in enumerate(dests, 1):
        pic = (f"<figure class='dc-pic'><img src=\"{escape(d['photo'])}\" alt='' loading='lazy' "
               f"onerror=\"this.parentNode.remove()\"><figcaption>Photo : {escape(d['photo_hotel'])}"
               "</figcaption></figure>") if d.get("photo") else "<div class='dc-pic dc-nopic'>🌴</div>"
        rows = "".join(f"<div class='dc-row'><span>{k}</span>{escape(v)}</div>" for k, v in (
            ("Idéal pour", d.get("ideal_pour")), ("À tes dates", d.get("moment")),
            ("Bémol", d.get("bemol"))) if v)
        guide = (f"<a class='dc-guide' href=\"{escape(d['guide'])}\" target='_blank' rel='noopener'>"
                 "Notre guide →</a>") if d.get("guide") else ""
        more = (f"<button type='button' class='chip rchip dc-more'>Les meilleurs hôtels à "
                f"{escape(d['nom'])}</button>")
        cards.append(f"<div class='dcard'>{pic}<div class='dc-body'><div class='dc-rank'>{i}</div>"
                     f"<div class='dc-name'>{escape(d['nom'])}</div>"
                     f"<div class='dc-hook'>{escape(d.get('accroche') or '')}</div>{rows}"
                     f"<div class='dc-foot'>{guide}{more}</div></div></div>")
    return f"<div class='dcards'>{''.join(cards)}</div>"


def questions_html(qs) -> str:
    if not qs:
        return ""
    groups = "".join(
        f"<div class='qgrp' data-q=\"{escape(q['q'])}\"><div class='qq'>{escape(q['q'])}</div>"
        f"<div class='qopts'>{''.join(f'<button type=\'button\' class=\'qopt\'>{escape(o)}</button>' for o in q['options'])}</div></div>"
        for q in qs)
    return (f"<div class='qset'><div class='qhead'>Pour affiner</div>{groups}"
            "<button type='button' class='btn qsend' disabled>Envoyer mes réponses</button></div>")


def strip_blocks(text: str) -> str:
    """The answer as the member sees it: everything before the first data block."""
    cuts = [i for i in (text.find(t) for t in _TAGS) if i != -1]
    return (text[:min(cuts)] if cuts else text).strip()


def parse_suivis(raw: str) -> list:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x).strip()[:60] for x in data if isinstance(x, str) and x.strip()][:4] \
        if isinstance(data, list) else []
_OG_CACHE: dict = {}            # page url -> image url or "" (process-wide)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class _OgParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = ""

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.image:
            return
        a = dict(attrs)
        key = (a.get("property") or a.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "twitter:image") and a.get("content"):
            self.image = a["content"].strip()


def _allowed_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    domains = [d.strip().lower() for d in settings.PORTAL_ASSISTANT_SEARCH_DOMAINS.split(",") if d.strip()]
    return any(host == d or host.endswith("." + d) for d in domains)


def page_preview(url: str):
    """(share image, page_is_dead) for a source page — like a link preview.
    og:image / twitter:image; 3 s timeout, 600 KB read cap, cached.
    page_is_dead is True only on 404/410: the link itself is broken."""
    if url in _OG_CACHE:
        return _OG_CACHE[url]
    img, dead = "", False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept-Language": "fr-CA,fr;q=0.9"})
        with urllib.request.urlopen(req, timeout=3) as r:
            if "html" in (r.headers.get("Content-Type") or ""):
                p = _OgParser()
                p.feed(r.read(600_000).decode("utf-8", "ignore"))
                cand = urljoin(url, p.image) if p.image else ""
                if cand.startswith("https://"):
                    img = cand
    except urllib.error.HTTPError as exc:
        dead = exc.code in (404, 410)
        if not dead:            # 403/5xx: site blocks bots or is down — link may still work
            log.info("preview unavailable (%s) for %s", exc.code, url)
    except Exception as exc:  # noqa: BLE001 — a missing photo is fine
        log.info("preview unavailable for %s: %s", url, exc)
    if len(_OG_CACHE) > 500:
        _OG_CACHE.clear()
    _OG_CACHE[url] = (img, dead)
    return img, dead


def _money(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n:,.0f}".replace(",", "\u202f") + "\u00a0$"


def parse_pistes(raw: str, sources: list) -> list:
    """Validate the model's <pistes> JSON into clean card dicts (max 3)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    known = {s["url"] for s in sources}
    cards = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not str(d.get("hotel") or "").strip():
            continue
        url = str(d.get("source_url") or "").strip()
        if url not in known and url.startswith("https://"):
            # Prefer a page the search actually cited on the same site over a
            # URL the model may have rebuilt from memory.
            host = urlparse(url).hostname
            same = [k for k in known if urlparse(k).hostname == host]
            if len(same) == 1:
                url = same[0]
        if not (url.startswith("https://") and (url in known or _allowed_domain(url))):
            url = ""
        try:
            nuits = int(d.get("nuits")) if d.get("nuits") not in (None, "") else None
        except (TypeError, ValueError):
            nuits = None
        cards.append({
            "hotel": str(d.get("hotel"))[:90],
            "destination": str(d.get("destination") or "")[:80],
            "depart": str(d.get("depart") or "")[:40],
            "nuits": nuits,
            "prix_personne": d.get("prix_personne"),
            "total": d.get("total"),
            "voyageurs": str(d.get("voyageurs") or "")[:40],
            "taxes": d.get("taxes") if d.get("taxes") in ("incluses", "en sus") else None,
            "source_url": url,
            "a_confirmer": str(d.get("a_confirmer") or "")[:120],
        })
        if len(cards) == 4:
            break
    return cards


def add_images(cards: list) -> list:
    """Photos, in order of trust: our own photo of the hotel (catalogue) →
    the source page's share image (if PORTAL_ASSISTANT_WEB_IMAGES) → none.
    Also drops source links that are dead (404) so a member never clicks
    through to an error page, and adds catalogue facts (rating, reviews…)."""
    for c in cards:
        extra = hc.card_extras(c["hotel"])
        if extra:
            c.update({"cat_" + k: v for k, v in extra.items()})
    urls = list({c["source_url"] for c in cards if c.get("source_url")})
    if urls:
        with ThreadPoolExecutor(max_workers=4) as ex:
            previews = dict(zip(urls, ex.map(page_preview, urls)))
        for c in cards:
            img, dead = previews.get(c.get("source_url"), ("", False))
            if dead:
                log.info("assistant: dead source link removed from card: %s", c["source_url"])
                c["source_url"] = ""
            c["image"] = c.get("cat_photo") or (img if settings.PORTAL_ASSISTANT_WEB_IMAGES else "")
    for c in cards:
        if "image" not in c:
            c["image"] = c.get("cat_photo") or ""
    return cards


def cards_html(cards) -> str:
    if not cards:
        return ""
    out = []
    for c in cards:
        url = c.get("source_url") or ""
        host = (urlparse(url).hostname or "").removeprefix("www.") if url else ""
        name = escape(c["hotel"])
        title = (f"<a class='pc-name' href=\"{escape(url)}\" target='_blank' "
                 f"rel='noopener nofollow'>{name}</a>") if url else f"<span class='pc-name'>{name}</span>"
        sub = " — ".join(x for x in (
            escape(c.get("destination") or ""),
            escape(", ".join(x for x in (
                f"départ {c['depart']}" if c.get("depart") else "",
                f"{c['nuits']} nuits" if c.get("nuits") else "") if x))) if x)
        pp, tot = _money(c.get("prix_personne")), _money(c.get("total"))
        price = f"<div class='pc-price'>{pp}<span>/personne</span></div>" if pp else ""
        tail = ", ".join(x for x in (
            f"taxes {'comprises' if c['taxes'] == 'incluses' else 'en sus'}" if c.get("taxes") else "",
            "prix affiché") if x)
        total = (f"<div class='pc-total'><b>{tot}{(' pour ' + escape(c['voyageurs'])) if c.get('voyageurs') else ''}</b>"
                 f", {tail}.</div>") if tot else ""
        chip = (f"<a class='pc-src' href=\"{escape(url)}\" target='_blank' rel='noopener nofollow'>"
                f"{escape(host)}</a>") if host else ""
        ours = c.get("cat_page") or kb.hotel_page(c["hotel"])
        if ours:
            label = "⭐ Notre coup de cœur" if c.get("cat_coup_de_coeur") else "Notre fiche"
            chip += (f"<a class='pc-ours' href=\"{escape(ours)}\" target='_blank' rel='noopener'>"
                     f"{label}</a>")
        rate = ""
        if c.get("cat_rating"):
            rv = c.get("cat_reviews")
            rate = (f"<div class='pc-rate'>★ {str(c['cat_rating']).replace('.', ',')}/5 TripAdvisor"
                    + (f" · {rv:,} avis".replace(",", "\u202f") if rv else "") + "</div>")
        conf = (f"<div class='pc-conf'>À confirmer : {escape(c['a_confirmer'])}</div>"
                if c.get("a_confirmer") else "")
        img = c.get("image") or ""
        pic = (f"<img class='pc-img' src=\"{escape(img)}\" alt='' loading='lazy' "
               "referrerpolicy='no-referrer' onerror=\"this.remove()\">") if img else ""
        out.append(f"<div class='pcard{'' if pic else ' noimg'}'>{pic}<div class='pc-body'>"
                   f"{title}<div class='pc-sub'>{sub}</div>{rate}{price}{total}"
                   f"<div class='pc-foot'>{chip}<a class='pc-go' href='{SEARCH_URL}'>"
                   f"Voir dans notre moteur →</a></div>{conf}</div></div>")
    priced = any(_money(c.get("prix_personne")) or _money(c.get("total")) for c in cards)
    head = (("Meilleure piste trouvée" if len(cards) == 1 else "Pistes trouvées") if priced
            else "Hôtels à regarder")
    cmp_btn = cmp = ""
    if len(cards) >= 2:
        def cell(v):
            return f"<td>{v or '—'}</td>"
        rows = [
            ("Destination", [escape(c.get("destination") or "") for c in cards]),
            ("TripAdvisor", [f"{str(c['cat_rating']).replace('.', ',')}/5" if c.get("cat_rating") else ""
                             for c in cards]),
            ("Départ", [escape(c.get("depart") or "") for c in cards]),
            ("Nuits", [str(c["nuits"]) if c.get("nuits") else "" for c in cards]),
            ("Prix / pers.", [_money(c.get("prix_personne")) for c in cards]),
            ("Total", [_money(c.get("total")) for c in cards]),
            ("Taxes", [{"incluses": "comprises", "en sus": "en sus"}.get(c.get("taxes"), "") for c in cards]),
            ("À confirmer", [escape(c.get("a_confirmer") or "") for c in cards]),
        ]
        thead = "".join(f"<th>{escape(c['hotel'])}</th>" for c in cards)
        tbody = "".join(f"<tr><th>{k}</th>{''.join(cell(v) for v in vals)}</tr>"
                        for k, vals in rows if any(vals))
        cmp_btn = "<button type='button' class='pc-cmpbtn'>Comparer</button>"
        ids = [c["cat_id"] for c in cards if c.get("cat_id")]
        if len(ids) >= 2:
            cmp_btn += (f"<a class='pc-cmpbtn' href='/portail/comparer?h={','.join(ids)}'>"
                        "Fiches côte à côte →</a>")
        cmp = (f"<div class='pc-cmp' hidden><table><thead><tr><th></th>{thead}</tr></thead>"
               f"<tbody>{tbody}</tbody></table></div>")
    return (f"<div class='pcards'><div class='pc-head'><span>{head}</span>{cmp_btn}</div>"
            f"{cmp}{''.join(out)}</div>")


def guides_html(links) -> str:
    if not links:
        return ""
    a = "".join(f"<a href=\"{escape(l['url'])}\" target='_blank' rel='noopener'>{escape(l['title'])}</a>"
                for l in links)
    return f"<div class='guides'><span>Nos guides</span>{a}</div>"


def suivis_html(items) -> str:
    if not items:
        return ""
    return "<div class='refine'>" + "".join(
        f"<button type='button' class='chip rchip'>{escape(x)}</button>" for x in items) + "</div>"


def rate_html(mid: str, rating: int = 0) -> str:
    if not mid:
        return ""
    up = " on" if rating == 1 else ""
    down = " on" if rating == -1 else ""
    return (f"<div class='rate' data-mid=\"{escape(mid)}\">"
            f"<button type='button' class='rbtn{up}' data-r='1' aria-label='Réponse utile'>👍</button>"
            f"<button type='button' class='rbtn{down}' data-r='-1' aria-label='Réponse pas utile'>👎</button></div>")


def _fill_slots(body: str, dests: str, cards: str) -> tuple:
    """Put card blocks where the agent placed [[DESTINATIONS]] / [[HOTELS]];
    returns (body, leftover blocks to append after the text)."""
    tail = ""
    for slot, html in (("destinations", dests), ("hotels", cards)):
        marker = f"<div class='slot' data-slot='{slot}'></div>"
        if marker in body:
            body = body.replace(marker, f"<div class='slot' data-slot='{slot}'>{html}</div>", 1)
        else:
            tail += html
    return body, tail


def _bubble(m: dict, last: bool = False) -> str:
    if m.get("role") == "user":
        return f"<div class='msg me'>{escape(m.get('text', ''))}</div>"
    body, tail = _fill_slots(render_md(m.get("text", "")), destinations_html(m.get("destinations")),
                             cards_html(m.get("cards")))
    return (f"<div class='msg ai'><div>{body}</div>{tail}{guides_html(m.get('guides'))}"
            f"{_sources_html(m.get('sources'))}{rate_html(m.get('id', ''), m.get('rating', 0))}"
            f"{questions_html(m.get('questions')) if last else ''}</div>")


def assistant_panel(client) -> str:
    """The home-page block: search CTA + chat thread + composer."""
    thread = list(client.assistant_chat or [])
    hi = escape(_first_name(client))
    intro = (f"Salut{(' ' + hi) if hi else ''}! Je suis ton assistant voyage. "
             "Décris-moi ton voyage (budget, dates, voyageurs, ce qui compte pour toi) "
             "et je te propose des pistes. Ensuite, trouve le même forfait avec ton "
             "rabais dans notre moteur de recherche.")
    msgs = (f"<div class='msg ai'>{intro}</div>"
            + "".join(_bubble(m, last=(i == len(thread) - 1)) for i, m in enumerate(thread)))
    chips = "" if thread else (
        "<div class='chips' id='aChips'>" + "".join(
            f"<button type='button' class='chip'>{escape(s)}</button>" for s in _SUGGESTIONS)
        + "</div>")
    if thread and thread[-1].get("role") == "assistant" and thread[-1].get("suivis"):
        chips = ("<div class='chips' id='aChips'>" + "".join(
            f"<button type='button' class='chip'>{escape(s)}</button>"
            for s in thread[-1]["suivis"]) + "</div>")
    reset = ("<button type='button' class='areset' id='aReset'>Nouvelle conversation</button>"
             if thread else "")
    human = ("<button type='button' class='areset' id='aHuman'>💬 Parler à un conseiller</button>")
    if is_tester(client):
        human = "<a class='areset' href='/portail/assistant/comparer'>🧪 Test A/B</a>" + human
    messenger = json.dumps(settings.MESSENGER_URL)
    return f"""
<a class='searchcta' href='{SEARCH_URL}'>
  <div><b>🔎 Rechercher un forfait</b>
  <span>Ton Rabais Récompense est déjà appliqué aux prix affichés.</span></div>
  <span class='sc-go'>→</span></a>
<div class='assist'>
  <div class='a-head'><div><h3>Ton assistant voyage</h3>
    <span class='a-sub'>Propulsé par l'IA · prix publics à titre indicatif</span></div>
    <div class='a-acts'>{human}{reset}</div></div>
  <div class='a-toast' id='aToast' hidden></div>
  <div class='a-log' id='aLog' aria-live='polite'>{msgs}</div>
  {chips}
  <form class='a-form' id='aForm' autocomplete='off'>
    <textarea id='aIn' rows='1' maxlength='{_MAX_LEN}' placeholder='Décris ton voyage…'
      aria-label='Ta question'></textarea>
    <button class='btn a-send' type='submit' id='aSend' aria-label='Envoyer'>Envoyer</button>
  </form>
</div>
<script>
(function(){{
 var log=document.getElementById('aLog'),f=document.getElementById('aForm'),
     inp=document.getElementById('aIn'),send=document.getElementById('aSend'),
     chips=document.getElementById('aChips'),rst=document.getElementById('aReset'),
     hum=document.getElementById('aHuman'),toast=document.getElementById('aToast');
 var MSGR={messenger};
 function say(t){{toast.textContent=t;toast.hidden=false;clearTimeout(say.t);
   say.t=setTimeout(function(){{toast.hidden=true;}},5000);}}
 function esc(s){{return s.replace(/[&<>"']/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#x27;'}}[c];}});}}
 function md(s){{return esc(s).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')
   .replace(/^\\s*(?:-{{3,}}|\\*{{3,}}|_{{3,}})\\s*$/gm,'').replace(/^\\s*[-*]\\s+/gm,'• ')
   .replace(/^#{{1,6}}\\s*/gm,'').replace(/\\n{{3,}}/g,'\\n\\n')
   .replace(/^\\s*\\[\\[(DESTINATIONS|HOTELS)\\]\\]\\s*$/gm,function(_,k){{
     return "<div class='slot' data-slot='"+k.toLowerCase()+"'></div>";}});}}
 function bottom(){{log.scrollTop=log.scrollHeight;}}
 function add(cls,t){{var d=document.createElement('div');d.className='msg '+cls;
   d.textContent=t;log.appendChild(d);bottom();return d;}}
 function grow(){{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,140)+'px';}}
 inp.addEventListener('input',grow);
 inp.addEventListener('keydown',function(e){{
   if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();f.requestSubmit();}}}});
 bottom();
 function ask(q){{
   q=(q||'').trim(); if(!q||send.disabled)return;
   if(chips){{chips.remove();chips=null;}}
   log.querySelectorAll('.refine,.qset').forEach(function(x){{x.remove();}});
   add('me',q); inp.value=''; grow();
   send.disabled=true; inp.disabled=true;
   var out=add('ai typing',''),body=document.createElement('div'),st=document.createElement('div');
   st.className='astatus';out.appendChild(st);out.appendChild(body);
   var raw='',buf='';
   function handle(ev){{
     if(ev.t==='status'){{st.textContent=ev.v;out.classList.remove('typing');}}
     else if(ev.t==='text'){{out.classList.remove('typing');st.textContent='';raw+=ev.v;body.innerHTML=md(raw);}}
     else if((ev.t==='dests'||ev.t==='cards')&&ev.v){{
       st.textContent='';var slot=body.querySelector(".slot[data-slot='"+(ev.t==='dests'?'destinations':'hotels')+"']");
       var cd=document.createElement('div');cd.innerHTML=ev.v;
       while(cd.firstChild)(slot||out).appendChild(cd.firstChild);}}
     else if((ev.t==='guides'||ev.t==='refine'||ev.t==='rate'||ev.t==='questions')&&ev.v){{
       st.textContent='';var cd2=document.createElement('div');cd2.innerHTML=ev.v;
       while(cd2.firstChild)out.appendChild(cd2.firstChild);}}
     else if(ev.t==='sources'&&ev.v&&ev.v.length&&!out.querySelector('.srcs')){{
       var s=document.createElement('div');s.className='srcs';s.innerHTML='<span>Sources</span>';
       ev.v.forEach(function(x){{if(!/^https:\\/\\//.test(x.url))return;var a=document.createElement('a');
         a.href=x.url;a.target='_blank';a.rel='noopener nofollow';a.textContent=x.title||x.url;s.appendChild(a);}});
       out.appendChild(s);}}
     bottom();
   }}
   fetch('/portail/assistant',{{method:'POST',headers:{{'Content-Type':'application/json'}},
     body:JSON.stringify({{message:q,fmt:'ndjson'}})}}).then(function(r){{
     if(!r.ok||!r.body){{return r.text().then(function(t){{throw new Error(t);}});}}
     var rd=r.body.getReader(),dec=new TextDecoder();
     function pump(){{return rd.read().then(function(x){{
       if(x.done){{if(buf.trim())try{{handle(JSON.parse(buf));}}catch(e){{}}return;}}
       buf+=dec.decode(x.value,{{stream:true}});var lines=buf.split('\\n');buf=lines.pop();
       lines.forEach(function(l){{if(l.trim())try{{handle(JSON.parse(l));}}catch(e){{}}}});
       return pump();}});}}
     return pump();
   }}).catch(function(e){{
     out.classList.remove('typing');st.textContent='';
     body.textContent=(e&&e.message&&e.message.length<300)?e.message:
       "Oups, je n'ai pas pu répondre. Réessaie dans un instant.";
   }}).finally(function(){{st.textContent='';send.disabled=false;inp.disabled=false;inp.focus();}});
 }}
 f.addEventListener('submit',function(e){{e.preventDefault();ask(inp.value);}});
 if(chips)chips.addEventListener('click',function(e){{
   var b=e.target.closest('.chip');if(b)ask(b.textContent);}});
 log.addEventListener('click',function(e){{
   var r=e.target.closest('.rchip');if(r){{ask(r.textContent);return;}}
   var o=e.target.closest('.qopt');
   if(o){{var g=o.closest('.qgrp'),set=o.closest('.qset');
     g.querySelectorAll('.qopt').forEach(function(x){{x.classList.toggle('on',x===o);}});
     var gs=set.querySelectorAll('.qgrp'),done=set.querySelectorAll('.qgrp .qopt.on').length;
     set.querySelector('.qsend').disabled=!done;
     if(done===gs.length)set.querySelector('.qsend').click();
     return;}}
   var qs=e.target.closest('.qsend');
   if(qs){{var parts=[];qs.closest('.qset').querySelectorAll('.qgrp').forEach(function(g){{
       var on=g.querySelector('.qopt.on');if(on)parts.push(g.getAttribute('data-q').replace(/\\s*\\?\\s*$/,'')+' : '+on.textContent);}});
     if(parts.length)ask(parts.join('\\n'));return;}}
   var c=e.target.closest('.pc-cmpbtn');
   if(c){{var t=c.closest('.pcards').querySelector('.pc-cmp');t.hidden=!t.hidden;
     c.textContent=t.hidden?'Comparer':'Masquer la comparaison';return;}}
   var b=e.target.closest('.rbtn');
   if(b){{var w=b.closest('.rate'),v=+b.getAttribute('data-r');
     w.querySelectorAll('.rbtn').forEach(function(x){{x.classList.toggle('on',x===b);}});
     fetch('/portail/assistant/feedback',{{method:'POST',headers:{{'Content-Type':'application/json'}},
       body:JSON.stringify({{id:w.getAttribute('data-mid'),rating:v}})}});
     if(v<0)say("Merci! Dis-moi ce qui cloche et je vais ajuster.");else say("Merci pour ton retour!");}}
 }});
 if(hum)hum.addEventListener('click',function(){{
   var me=[].slice.call(log.querySelectorAll('.msg.me')).slice(-3).map(function(x){{return '- '+x.textContent;}});
   var pc=[].slice.call(log.querySelectorAll('.pc-name')).slice(-3).map(function(x){{return x.textContent;}});
   var txt='Bonjour! Je viens de l’assistant voyage de mon espace client.'+
     (me.length?'\\nMa demande :\\n'+me.join('\\n'):'')+(pc.length?'\\nPistes vues : '+pc.join(', '):'');
   var w=window.open(MSGR,'_blank');if(w)try{{w.opener=null;}}catch(e){{}}
   if(navigator.clipboard)navigator.clipboard.writeText(txt).then(function(){{
     say('Résumé copié 📋 — colle-le dans Messenger pour que le conseiller parte de là.');}},function(){{}});
   if(!w)location.href=MSGR;
 }});
 if(rst)rst.addEventListener('click',function(){{
   fetch('/portail/assistant/reset',{{method:'POST'}}).then(function(){{location.reload();}});}});
}})();
</script>"""


# --------------------------------------------------------------------------- #
# Chat endpoint (streamed NDJSON)
# --------------------------------------------------------------------------- #
def _ev(t: str, v) -> str:
    return json.dumps({"t": t, "v": v}, ensure_ascii=False) + "\n"


def _search_tool() -> dict:
    domains = [d.strip() for d in settings.PORTAL_ASSISTANT_SEARCH_DOMAINS.split(",") if d.strip()]
    tool = {"type": "web_search_20250305", "name": "web_search",
            "max_uses": settings.PORTAL_ASSISTANT_SEARCHES_PER_ANSWER,
            "user_location": {"type": "approximate", "city": "Montreal",
                              "region": "Quebec", "country": "CA",
                              "timezone": "America/Toronto"}}
    if domains:
        tool["allowed_domains"] = domains
    return tool


def _collect_sources(message) -> list:
    seen, out = set(), []
    for block in getattr(message, "content", None) or []:
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen.add(url)
                out.append({"url": url, "title": getattr(c, "title", None) or ""})
    return out[:6]


# --------------------------------------------------------------------------- #
# Model providers — Claude (Anthropic) or ChatGPT (OpenAI), same event stream
# --------------------------------------------------------------------------- #
PROVIDERS = ("anthropic", "openai")


def _full() -> bool:
    return settings.PORTAL_ASSISTANT_STYLE.strip().lower() != "concis"


def _max_tokens() -> int:
    return 5000 if _full() else 2000
_LABELS = {"anthropic": "Claude", "openai": "ChatGPT"}


class SearchRefused(Exception):
    """The model rejected the web-search tool: retry the same turn without it."""


def provider_ready(provider: str) -> bool:
    return bool(settings.ANTHROPIC_API_KEY if provider == "anthropic" else settings.OPENAI_API_KEY)


def model_name(provider: str) -> str:
    return settings.PORTAL_ASSISTANT_MODEL if provider == "anthropic" else settings.OPENAI_ASSISTANT_MODEL


def _run_anthropic(search: bool, system: list, convo: list):
    """Yields ("status"|"text", str) then ("meta", {"sources", "searches"})."""
    api = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    sources, searches, wrote = [], 0, False
    for _attempt in range(3):                     # pause_turn continuations
        blocks = [{"type": "text", "text": system[0], "cache_control": {"type": "ephemeral"}}]
        if system[1]:
            blocks.append({"type": "text", "text": system[1]})
        kw = dict(model=settings.PORTAL_ASSISTANT_MODEL, max_tokens=_max_tokens(),
                  system=blocks, messages=convo)
        if search:
            kw["tools"] = [_search_tool()]
        try:
            with api.messages.stream(**kw) as stream:
                for event in stream:
                    et = getattr(event, "type", "")
                    if et == "content_block_start":
                        cb = getattr(event, "content_block", None)
                        if getattr(cb, "type", "") == "server_tool_use":
                            yield ("status", "🔎 Je cherche des options en ligne…")
                    elif et == "content_block_delta":
                        d = getattr(event, "delta", None)
                        if getattr(d, "type", "") == "text_delta" and d.text:
                            wrote = True
                            yield ("text", d.text)
                final = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            if search and not wrote:
                raise SearchRefused(str(exc)) from exc
            raise
        searches += getattr(getattr(final.usage, "server_tool_use", None),
                            "web_search_requests", 0) or 0
        sources += [s for s in _collect_sources(final)
                    if s["url"] not in {x["url"] for x in sources}]
        if final.stop_reason == "pause_turn":
            convo = convo + [{"role": "assistant", "content": final.content}]
            continue
        break
    yield ("meta", {"sources": sources, "searches": searches})


def _openai_tool() -> dict:
    domains = [d.strip() for d in settings.PORTAL_ASSISTANT_SEARCH_DOMAINS.split(",") if d.strip()]
    tool = {"type": "web_search",
            "user_location": {"type": "approximate", "country": "CA", "city": "Montreal",
                              "region": "Quebec", "timezone": "America/Toronto"}}
    if domains:
        tool["filters"] = {"allowed_domains": domains}
    return tool


def _run_openai(search: bool, system: list, convo: list):
    """Same contract as _run_anthropic, on OpenAI's Responses API."""
    import openai                                  # optional dependency
    api = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    kw = dict(model=settings.OPENAI_ASSISTANT_MODEL, instructions="\n\n".join(x for x in system if x),
              input=[{"role": m["role"], "content": m["content"]} for m in convo],
              max_output_tokens=_max_tokens() + 6000, stream=True)   # + reasoning
    if settings.OPENAI_REASONING_EFFORT:
        kw["reasoning"] = {"effort": settings.OPENAI_REASONING_EFFORT}
    if search:
        kw["tools"] = [_openai_tool()]
    wrote, said, final = False, False, None
    try:
        for event in api.responses.create(**kw):
            et = getattr(event, "type", "")
            if et == "response.output_text.delta" and event.delta:
                wrote = True
                yield ("text", event.delta)
            elif et.startswith("response.web_search_call") and not said:
                said = True
                yield ("status", "🔎 Je cherche des options en ligne…")
            elif et == "response.completed":
                final = event.response
            elif et in ("error", "response.failed"):
                raise RuntimeError(getattr(event, "message", None) or et)
    except openai.BadRequestError as exc:
        if search and not wrote:
            raise SearchRefused(str(exc)) from exc
        raise
    sources, searches = [], 0
    for item in getattr(final, "output", None) or []:
        if getattr(item, "type", "") == "web_search_call":
            searches += 1
        for part in getattr(item, "content", None) or []:
            for a in getattr(part, "annotations", None) or []:
                url = getattr(a, "url", None)
                if getattr(a, "type", "") == "url_citation" and url \
                        and url not in {s["url"] for s in sources}:
                    sources.append({"url": url, "title": getattr(a, "title", None) or ""})
    yield ("meta", {"sources": sources[:6], "searches": searches})


_RUNNERS = {"anthropic": _run_anthropic, "openai": _run_openai}


def _history_msgs(history: list, q: str) -> list:
    def ctx(m):
        extra = ""
        for key, tag in (("destinations", "destinations"), ("cards", "pistes"), ("questions", "questions")):
            if m.get(key):
                data = [{k: v for k, v in x.items() if k in ("nom", "hotel", "destination", "prix_personne",
                                                                "total", "q", "options", "depart", "nuits")}
                        for x in m[key]]
                extra += f"\n<{tag}>" + json.dumps(data, ensure_ascii=False) + f"</{tag}>"
        return m["text"] + extra
    msgs = [{"role": m["role"], "content": ctx(m)}
            for m in history[-_CONTEXT:] if m.get("text")]
    while msgs and msgs[0]["role"] != "user":    # start with user, alternate
        msgs.pop(0)
    msgs.append({"role": "user", "content": q})
    return msgs


def answer_events(provider: str, cid: int, q: str, history: list, name: str,
                  can_search: bool, persist, count_searches: bool = True,
                  debug: bool = False):
    """The whole answer pipeline as NDJSON lines, for either provider.
    `persist(user_entry, assistant_entry)` stores the exchange at the end."""
    convo_text = " ".join([m["text"] for m in history[-6:] if m.get("role") == "user"] + [q])
    excerpts = kb.context_for(convo_text)
    catalogue = kb.catalogue()

    ranked = hc.prompt_lines(convo_text)

    def system_for(search: bool) -> list:
        """[static, dynamic]: the static block (rules + catalogue) is identical
        across members and questions, so it is prompt-cached."""
        static = SYSTEM_PROMPT.format(
            today=_today(), search_rule=_SEARCH_ON if search else _SEARCH_OFF,
            catalogue=catalogue, excerpts="")
        if not _full():
            static += STYLE_CONCIS
        dynamic = ""
        if ranked:
            dynamic += ("HÔTELS RECOMMANDÉS PAR DU VOYAGEUR pour cette demande "
                        "(du meilleur au moins bon) :\n" + ranked + "\n\n")
        if excerpts:
            dynamic += "Extraits de nos pages liés à la conversation :\n" + excerpts + "\n\n"
        if name:
            dynamic += f"Le membre s'appelle {name}."
        return [static, dynamic.strip()]

    parts: list[str] = []          # everything the model wrote
    shown = [0]                    # chars of `parts` already sent
    meta = {"sources": [], "searches": 0}

    def visible_cut(full: str) -> int:
        cuts = [i for i in (full.find(t) for t in _TAGS) if i != -1]
        if cuts:
            return min(cuts)
        for k in range(_TAG_MAX - 1, 0, -1):            # possible partial tag
            if any(full.endswith(t[:k]) for t in _TAGS):
                return len(full) - k
        return len(full)

    msgs = _history_msgs(history, q)
    search = can_search
    try:
        for _try in range(2):
            try:
                for kind, val in _RUNNERS[provider](search, system_for(search), msgs):
                    if kind == "text":
                        parts.append(val)
                        full = "".join(parts)
                        cut = visible_cut(full)
                        if cut > shown[0]:
                            yield _ev("text", full[shown[0]:cut])
                            shown[0] = cut
                    elif kind == "meta":
                        meta = val
                    else:
                        yield _ev(kind, val)
                break
            except SearchRefused as exc:
                log.warning("assistant[%s]: web search refused, retrying without: %s", provider, exc)
                search = False
    except Exception as exc:  # noqa: BLE001 — never break the page
        log.warning("assistant[%s] stream failed: %s", provider, exc)
        if not parts:
            yield _ev("text", "Oups, je n'ai pas pu répondre. Réessaie dans un instant.")
        if debug:                      # test page only: show the real cause
            yield _ev("text", f"\n\n[{_LABELS[provider]} · {model_name(provider)}] "
                              f"{type(exc).__name__}: {str(exc)[:600]}")
        return
    if count_searches and meta["searches"]:
        _SEARCHES.setdefault(cid, []).extend([time.monotonic()] * meta["searches"])
    sources = meta["sources"]
    full = "".join(parts)
    answer = strip_blocks(full)
    cut = visible_cut(full)
    if shown[0] < cut:                 # text held back while checking for a tag
        yield _ev("text", full[shown[0]:cut])
    dm = _DEST_RE.search(full)
    dests = parse_destinations(dm.group(1).strip()) if dm else []
    if dests:
        yield _ev("dests", destinations_html(dests))
    m = _PISTES_RE.search(full)
    cards = parse_pistes(m.group(1).strip(), sources) if m else []
    if cards:
        yield _ev("status", "📸 Je prépare les fiches…")
        cards = add_images(cards)
        yield _ev("cards", cards_html(cards))
    guides = kb.related_links(" ".join([q, answer] + [c["hotel"] + " " + c.get("destination", "")
                                                      for c in cards]))
    if guides:
        yield _ev("guides", guides_html(guides))
    if sources:
        yield _ev("sources", sources)
    sm = _SUIVIS_RE.search(full)
    suivis = parse_suivis(sm.group(1).strip()) if sm else []
    if not answer:
        return
    mid = secrets.token_hex(8)
    yield _ev("rate", rate_html(mid))
    qm = _QUEST_RE.search(full)
    questions = parse_questions(qm.group(1).strip()) if qm else []
    if questions:
        yield _ev("questions", questions_html(questions))
    if suivis:
        yield _ev("refine", suivis_html(suivis))
    now = datetime.utcnow().isoformat(timespec="seconds")
    persist({"role": "user", "text": q, "at": now},
            {"role": "assistant", "id": mid, "text": answer, "cards": cards,
             "destinations": dests, "questions": questions,
             "guides": guides, "suivis": suivis, "sources": sources,
             "provider": provider, "model": model_name(provider), "at": now})


def _stream(lines, ndjson: bool = True):
    def plain():
        for line in lines:
            ev = json.loads(line)
            if ev["t"] == "text":
                yield ev["v"]
            elif ev["t"] == "sources":
                yield "\n\nSources : " + " · ".join(x["url"] for x in ev["v"])
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    if not ndjson:
        return StreamingResponse(plain(), media_type="text/plain; charset=utf-8", headers=headers)
    return StreamingResponse(lines, media_type="application/x-ndjson; charset=utf-8",
                             headers=headers)


@router.post("/portail/assistant")
async def assistant_chat(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    q = str(payload.get("message") or "").strip()[:_MAX_LEN]
    # Pages loaded before the NDJSON format shipped read a plain-text stream;
    # keep serving them that so an open tab never shows raw JSON.
    ndjson = payload.get("fmt") == "ndjson"
    provider = settings.PORTAL_ASSISTANT_PROVIDER if \
        settings.PORTAL_ASSISTANT_PROVIDER in PROVIDERS else "anthropic"
    if not q:
        return HTMLResponse("Écris ta question 🙂", status_code=400)
    if not provider_ready(provider):
        return HTMLResponse("L'assistant n'est pas disponible pour le moment.", status_code=503)
    if not _rate_ok(cid):
        return HTMLResponse("Tu as atteint la limite de questions pour aujourd'hui. "
                            "Reviens demain, ou trouve ton forfait dans le moteur de "
                            "recherche!", status_code=429)

    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return JSONResponse({"error": "auth"}, status_code=401)
        history = list(client.assistant_chat or [])
        name = _first_name(client)
    _HITS.setdefault(cid, []).append(time.monotonic())

    def persist(user_entry, ai_entry):
        with SessionLocal() as db2:
            c = db2.get(Client, cid)
            if c:
                c.assistant_chat = (list(c.assistant_chat or []) + [user_entry, ai_entry])[-_KEEP:]
                db2.commit()

    return _stream(answer_events(provider, cid, q, history, name,
                                 _searches_left(cid) > 0, persist), ndjson)


# --------------------------------------------------------------------------- #
# Blind A/B test page: Claude vs ChatGPT, for the emails in ASSISTANT_TESTERS
# --------------------------------------------------------------------------- #
_CMP: dict = {}         # test id -> {"cid", "slots": {"A","B"}, "hist": {"A": [], "B": []}}
_PENDING: dict = {}     # message id -> answer awaiting a rating (test page only)


def is_tester(client) -> bool:
    emails = {e.strip().lower() for e in settings.ASSISTANT_TESTERS.split(",") if e.strip()}
    return bool(client and client.primary_email and client.primary_email.lower() in emails)


def _new_test(cid: int) -> str:
    order = list(PROVIDERS)
    secrets.SystemRandom().shuffle(order)
    tid = secrets.token_hex(8)
    if len(_CMP) > 200:
        _CMP.clear()
    _CMP[tid] = {"cid": cid, "slots": {"A": order[0], "B": order[1]}, "hist": {"A": [], "B": []}}
    return tid


_CMP_JS = r"""
(function(){
 var tid=document.getElementById('cmp').getAttribute('data-tid'),f=document.getElementById('cForm'),
     inp=document.getElementById('cIn'),send=document.getElementById('cSend'),busy=0;
 function esc(s){return s.replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#x27;'}[c];});}
 function md(s){return esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/gm,'')
   .replace(/^\s*[-*]\s+/gm,'• ').replace(/^#{1,6}\s*/gm,'').replace(/\n{3,}/g,'\n\n');}
 function run(slot,q){
   var log=document.getElementById('log'+slot);
   var me=document.createElement('div');me.className='msg me';me.textContent=q;log.appendChild(me);
   var out=document.createElement('div');out.className='msg ai typing';log.appendChild(out);
   var st=document.createElement('div');st.className='astatus';out.appendChild(st);
   var body=document.createElement('div');out.appendChild(body);
   var raw='',buf='',t0=Date.now(),first=0;busy++;
   function handle(ev){
     if(ev.t==='status'){st.textContent=ev.v;out.classList.remove('typing');}
     else if(ev.t==='text'){if(!first)first=Date.now()-t0;out.classList.remove('typing');st.textContent='';raw+=ev.v;body.innerHTML=md(raw);}
     else if(['cards','guides','rate','refine'].indexOf(ev.t)>=0&&ev.v){st.textContent='';var d=document.createElement('div');
       d.innerHTML=ev.v;while(d.firstChild)out.appendChild(d.firstChild);}
     else if(ev.t==='sources'&&ev.v&&ev.v.length){var s=document.createElement('div');s.className='srcs';s.innerHTML='<span>Sources</span>';
       ev.v.forEach(function(x){if(!/^https:\/\//.test(x.url))return;var a=document.createElement('a');a.href=x.url;a.target='_blank';
         a.rel='noopener nofollow';a.textContent=x.title||x.url;s.appendChild(a);});out.appendChild(s);}
   }
   fetch('/portail/assistant/comparer/ask',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({test:tid,slot:slot,message:q})}).then(function(r){
     if(!r.ok||!r.body)return r.text().then(function(t){throw new Error(t);});
     var rd=r.body.getReader(),dec=new TextDecoder();
     function pump(){return rd.read().then(function(x){
       if(x.done){if(buf.trim())try{handle(JSON.parse(buf));}catch(e){}return;}
       buf+=dec.decode(x.value,{stream:true});var l=buf.split('\n');buf=l.pop();
       l.forEach(function(z){if(z.trim())try{handle(JSON.parse(z));}catch(e){}});return pump();});}
     return pump();
   }).catch(function(e){out.classList.remove('typing');body.textContent=(e&&e.message)||'Erreur';})
   .finally(function(){st.textContent='';var tm=document.createElement('div');tm.className='ctime';
     tm.textContent='1er mot : '+(first/1000).toFixed(1)+' s · total : '+((Date.now()-t0)/1000).toFixed(1)+' s';
     out.appendChild(tm);if(--busy===0){send.disabled=false;inp.disabled=false;}});
 }
 function ask(q){q=(q||'').trim();if(!q||busy)return;inp.value='';send.disabled=true;inp.disabled=true;
   document.querySelectorAll('#cmp .refine').forEach(function(x){x.remove();});run('A',q);run('B',q);}
 f.addEventListener('submit',function(e){e.preventDefault();ask(inp.value);});
 document.getElementById('cmp').addEventListener('click',function(e){
   var r=e.target.closest('.rchip');if(r){ask(r.textContent);return;}
   var c=e.target.closest('.pc-cmpbtn');if(c){var t=c.closest('.pcards').querySelector('.pc-cmp');t.hidden=!t.hidden;return;}
   var b=e.target.closest('.rbtn');if(b){var w=b.closest('.rate');
     w.querySelectorAll('.rbtn').forEach(function(x){x.classList.toggle('on',x===b);});
     fetch('/portail/assistant/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({id:w.getAttribute('data-mid'),rating:+b.getAttribute('data-r')})});}
 });
 document.getElementById('cReveal').addEventListener('click',function(){
   fetch('/portail/assistant/comparer/reveal?test='+tid).then(function(r){return r.json();}).then(function(j){
     document.getElementById('nameA').textContent='A = '+j.A;document.getElementById('nameB').textContent='B = '+j.B;});
 });
})();
"""


@router.get("/portail/assistant/comparer", response_class=HTMLResponse)
def compare_page(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not is_tester(client):
            return RedirectResponse("/portail", status_code=303)
    missing = [_LABELS[p] for p in PROVIDERS if not provider_ready(p)]
    warn = (f"<div class='note err'>Clé API manquante pour : {', '.join(missing)}.</div>"
            if missing else "")
    tid = _new_test(cid)
    col = lambda s: (f"<div class='assist cmpcol'><div class='a-head'><h3 id='name{s}'>Assistant {s}</h3></div>"
                     f"<div class='a-log' id='log{s}' aria-live='polite'></div></div>")
    body = (warn + "<div class='hello'><h2>Test à l'aveugle 🧪</h2>"
            "<p class='lede'>Même question, même contenu et mêmes règles pour les deux. "
            "L'ordre A/B est tiré au hasard à chaque test. Note chaque réponse avec 👍/👎 "
            "<b>avant</b> de révéler qui est qui.</p></div>"
            f"<div id='cmp' data-tid='{tid}'><div class='cmpgrid'>{col('A')}{col('B')}</div></div>"
            "<form class='a-form' id='cForm' autocomplete='off'>"
            f"<textarea id='cIn' rows='1' maxlength='{_MAX_LEN}' placeholder='Décris un voyage…'></textarea>"
            "<button class='btn a-send' id='cSend' type='submit'>Envoyer aux deux</button></form>"
            "<div class='actions' style='margin-top:14px'>"
            "<button class='btn ghost' id='cReveal' type='button'>Révéler A / B</button>"
            "<a class='btn ghost' href='/portail/assistant/comparer'>Nouveau test</a></div>"
            "<style>.cmpgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}"
            ".cmpcol .a-log{max-height:none;min-height:180px}.ctime{font-size:11.5px;color:var(--mist);margin-top:8px}"
            "@media(max-width:900px){.cmpgrid{grid-template-columns:1fr}}</style>"
            f"<script>{_CMP_JS}</script>")
    resp = HTMLResponse(_shell("Test A/B", body, logged_in=True, nav=_nav("accueil")))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/assistant/comparer/ask")
async def compare_ask(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    test = _CMP.get(str(payload.get("test") or ""))
    slot = payload.get("slot")
    q = str(payload.get("message") or "").strip()[:_MAX_LEN]
    with SessionLocal() as db:
        client = db.get(Client, cid)
        allowed = is_tester(client)
        name = _first_name(client) if client else ""
    if not allowed or not test or test["cid"] != cid or slot not in ("A", "B") or not q:
        return HTMLResponse("Test introuvable — recharge la page.", status_code=400)
    provider = test["slots"][slot]
    if not provider_ready(provider):
        return HTMLResponse(f"Clé API manquante pour l'assistant {slot}.", status_code=503)
    hist = test["hist"][slot]

    def persist(user_entry, ai_entry):
        hist.extend([user_entry, ai_entry])
        _PENDING[ai_entry["id"]] = {"cid": cid, "question": q, **ai_entry}
        if len(_PENDING) > 2000:
            _PENDING.pop(next(iter(_PENDING)))

    return _stream(answer_events(provider, cid, q, list(hist), name, True, persist,
                                 count_searches=False, debug=True))


@router.get("/portail/assistant/comparer/reveal")
def compare_reveal(request: Request, test: str = ""):
    cid = current_portal_client_id(request)
    t = _CMP.get(test)
    if not cid or not t or t["cid"] != cid:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({s: f"{_LABELS[p]} ({model_name(p)})" for s, p in t["slots"].items()})


@router.post("/portail/assistant/feedback")
async def assistant_feedback(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    mid = str(payload.get("id") or "")[:40]
    rating = 1 if payload.get("rating") == 1 else -1 if payload.get("rating") == -1 else 0
    if not mid or not rating:
        return JSONResponse({"error": "bad request"}, status_code=400)
    with SessionLocal() as db:
        c = db.get(Client, cid)
        chat = list((c.assistant_chat if c else None) or [])
        idx = next((i for i, m in enumerate(chat) if m.get("id") == mid), None)
        pending = _PENDING.get(mid)
        if idx is not None:
            chat[idx] = {**chat[idx], "rating": rating}
            c.assistant_chat = chat
            entry = chat[idx]
            q = next((chat[j]["text"] for j in range(idx - 1, -1, -1)
                      if chat[j].get("role") == "user"), None)
        elif pending and pending["cid"] == cid:          # blind A/B test page
            entry, q = pending, pending["question"]
        else:                                            # only the member's own answers
            return JSONResponse({"error": "not found"}, status_code=404)
        row = db.query(AssistantFeedback).filter_by(message_id=mid).first()
        if not row:
            row = AssistantFeedback(message_id=mid, client_id=cid)
            db.add(row)
        row.rating, row.question, row.answer = rating, q, entry.get("text")
        row.cards = entry.get("cards") or []
        row.model = f"{entry.get('provider', 'anthropic')}:{entry.get('model')}"
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/portail/assistant/reset")
def assistant_reset(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"error": "auth"}, status_code=401)
    with SessionLocal() as db:
        c = db.get(Client, cid)
        if c:
            c.assistant_chat = []
            db.commit()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# Search page (Softvoyage DMO engine)
# --------------------------------------------------------------------------- #
def _engine_url() -> str:
    q = urlencode({"code_ag": settings.SIRRVA_CODE_AG, "alias": settings.SIRRVA_ALIAS,
                   "language": "fr"})
    return f"{settings.SIRRVA_HOST.rstrip('/')}/cgi-bin/querypackage.cgi?{q}"


@router.get("/portail/recherche", response_class=HTMLResponse)
def portal_search(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return _logged_out()
        bell = _bell_panel(client)
    if settings.SIRRVA_ALIAS:
        body = ("<div class='hello'><h2>Rechercher un forfait</h2>"
                "<p class='lede'>Ton Rabais Récompense est déjà appliqué aux prix "
                "affichés.</p></div>"
                f"<div class='engine'><iframe src=\"{escape(_engine_url())}\" "
                "title='Moteur de recherche de forfaits' loading='eager'></iframe></div>")
    else:
        body = ("<div class='infobox'><h2>Bientôt disponible 🌴</h2>"
                "<p>Notre moteur de recherche avec ton Rabais Récompense arrive très "
                "bientôt. En attendant, pose tes questions à ton assistant voyage!</p>"
                "<p style='margin-top:16px'><a class='btn' href='/portail'>"
                "Retour à l'accueil</a></p></div>")
    resp = HTMLResponse(_shell("Rechercher", body, logged_in=True,
                               nav=_nav("recherche"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp
