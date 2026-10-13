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
from urllib.parse import quote_plus, urljoin
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
      - **Les hôtels que je regarderais** : UNE phrase d'intro, puis
        [[HOTELS]] seul sur sa ligne. Les fiches d'hôtels (photo, angle,
        résumé, prix, note) s'affichent là depuis le bloc <pistes> : ne
        décris PAS les hôtels un par un dans le texte. Tu peux ajouter une
        phrase après pour dire lequel tu choisirais et pourquoi ;
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

4. HÔTELS : recommande selon leur RÉPUTATION RÉELLE, vérifiée en ligne.
   - Choisis 3 à 5 hôtels reconnus qui collent aux critères (destination,
     voyageurs, envies, budget) et qui sont DIFFÉRENTS les uns des autres :
     chacun a son angle (« Meilleur pour la gastronomie », « Meilleur choix
     luxe », « Meilleur rapport qualité-prix », « Plus calme »…). Pas deux
     fois le même profil. Appuie ton choix sur TripAdvisor (note et nombre
     d'avis) trouvé par ta recherche ; n'invente jamais une note.
   - Pour CHAQUE hôtel recommandé, trouve sa fiche chez un ou plusieurs
     grossistes canadiens (Transat, Vacances Air Canada, Vacances Sunwing,
     Vacances WestJet) : fiche_url = la meilleure, fiches = les autres. Ce
     sont elles qui affichent les vraies photos de l'hôtel. Aucune : null.
   - Pour chaque destination proposée, page_url = une page de cette
     destination chez un de ces grossistes, si tu l'as trouvée.

5. PRIX : {search_rule}
   Ne cherche les prix que lorsque la destination est assez précise ET que tu
   as les dates et les voyageurs ; sinon, conseille sans prix. Les prix
   publics changent vite : « vu en ligne », jamais garantis. Quand tu donnes
   des prix, termine par : « Ces prix sont des prix publics. Cherche le même
   forfait dans notre moteur : c'est souvent moins cher, avec ton Rabais
   Récompense déjà appliqué. »

6. Le CONTENU DE DU VOYAGEUR ci-dessous (saisons, quoi faire) est une aide
   pour l'information générale seulement. Pour les hôtels, fie-toi à tes
   recherches et aux fiches des grossistes. Ne renvoie pas à nos pages.

Règles strictes :
- Ne promets jamais que notre prix sera plus bas ni un montant de rabais
  précis : « souvent moins cher », « jusqu'à 5 % avant taxes ».
- Tu ne peux pas réserver ni confirmer une disponibilité.
- N'invente jamais un hôtel, un prix, une note, un horaire ou un détail :
  ils viennent de tes résultats de recherche.
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
"bemol": "plus animé que reposant", "moment": "baleines et Nouvel An festif",
"page_url": "https://…"}}]</destinations>
   (seulement avec [[DESTINATIONS]] ; 3 ou 4 destinations ; textes courts)
<pistes>[{{"hotel": "Nom exact", "destination": "Marina Vallarta, Puerto Vallarta",
"titre": "Meilleur pour le calme et la gastronomie",
"type": "Tout-inclus boutique · adultes seulement",
"resume": "Petit hôtel de 80 suites avec jardin tropical et club de plage. Cuisine réputée, service très attentionné.",
"points_forts": ["Restaurants à la carte réputés", "Très calme"],
"bemol": "Plage de l'hôtel petite ; navette vers le club de plage",
"depart": "31 décembre 2026", "nuits": 7, "prix_personne": null,
"total": null, "voyageurs": "2 adultes", "taxes": null, "prix_note": null,
"fiche_url": "https://…", "fiches": ["https://…"], "source_url": null,
"note": 4.8, "avis": 3100, "avis_url": "https://www.tripadvisor.ca/…",
"adresse": null, "a_confirmer": "vol direct"}}]</pistes>
   (seulement avec [[HOTELS]] ou des prix ; 3 à 5 hôtels ; titre = l'angle
   qui le distingue des autres, moins de 50 caractères ; resume = 2 phrases
   concrètes et utiles, pas de formules creuses ; points_forts = 2 ou 3
   courts ; bemol = honnête ; prix, source_url et taxes à null tant que tu
   n'as pas cherché le prix ; source_url = l'adresse exacte de la page où
   tu as vu le prix ; prix_note = une courte précision sur ce prix (« à
   partir de », date différente…) ou null ; note, avis et avis_url = note
   TripAdvisor, nombre d'avis et page où tu les as vus, sinon null ;
   adresse seulement si tu l'as vue dans tes sources ; taxes "incluses" ou
   "en sus" ; montants en dollars sans symbole)
<questions>[{{"q": "Budget total pour 2, vols compris ?",
"options": ["3 500 – 5 000 $", "5 000 – 7 000 $", "7 000 – 10 000 $", "10 000 $ et plus"]}},
{{"q": "Quel type de séjour ?", "options": ["Tout inclus avec excellente bouffe",
"Hôtel de luxe et restos à découvrir", "Ouvert aux deux"]}}]</questions>
   (1 à 3 questions qui changent vraiment ta recommandation ; 2 à 4
   options courtes chacune ; jamais une question déjà répondue)
<suivis>["Moins cher", "Seulement avec vol direct"]</suivis>
   (2 ou 3 raffinements rédigés comme SA demande, moins de 40 caractères)

Style : français québécois chaleureux, tu tutoies, riche mais lisible :
phrases courtes, sections, listes avec « - » quand ça aide. Varie ton
écriture : pas la même structure de phrase d'une section à l'autre. **Gras** pour
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

_SEARCH_ON = ("cherche sur le web (sites des grossistes canadiens et d'information) : "
              "la fiche et la note des hôtels que tu recommandes, leur prix public actuel "
              "quand c'est le moment, les vols directs, les événements à ses dates.")
_SEARCH_OFF = ("pas de recherche web pour cette réponse : conseille sans prix ni note "
               "précise, et envoie-le vers notre moteur pour les prix et les photos.")

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


_CITE_RE = re.compile(r"\s?\[\[c:([\d,]+)\]\]")


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").removeprefix("www.")


def cite_html(idxs: list, sources: list) -> str:
    """Inline source pill (chatgpt.com style): host label, hover/tap preview card."""
    items = [sources[i] for i in idxs if 0 <= i < len(sources)]
    if not items:
        return ""
    first = items[0]
    more = f"<span class='cite-n'>+{len(items) - 1}</span>" if len(items) > 1 else ""
    pop = "".join(
        f"<a class='cp-item' href=\"{escape(x['url'])}\" target='_blank' rel='noopener nofollow'>"
        + (f"<img class='cp-img' src=\"{escape(x['image'])}\" alt='' loading='lazy' "
           "referrerpolicy='no-referrer' onerror=\"this.remove()\">" if x.get("image") else "")
        + f"<span class='cp-host'>{escape(x.get('host') or _host(x['url']))}</span>"
        + f"<b class='cp-title'>{escape(x.get('title') or x['url'])}</b>"
        + (f"<span class='cp-snip'>{escape(x['snippet'])}</span>" if x.get("snippet") else "")
        + "</a>" for x in items)
    return (f"<span class='cite' tabindex='0'><a class='cite-a' href=\"{escape(first['url'])}\" "
            f"target='_blank' rel='noopener nofollow'>{escape(first.get('host') or _host(first['url']))}"
            f"{more}</a><span class='cpop' role='tooltip'>{pop}</span></span>")


def _clean_src_url(url: str) -> str:
    """Drop tracking params the search tool adds (utm_source=openai…)."""
    u = urlparse(url)
    q = "&".join(p for p in u.query.split("&") if p and not p.startswith("utm_"))
    return u._replace(query=q).geturl()


def mark_citations(visible: str, cites: list) -> tuple:
    """Insert [[c:i,j]] markers where the model cited a page (only inside the
    visible answer) and return (marked_text, sources). Sources carry
    title, url, host, snippet (quoted text) and a live preview image."""
    cites = [dict(c, url=_clean_src_url(c["url"])) for c in cites
             if str(c.get("url", "")).startswith("https://") and 0 < c.get("at", 0) <= len(visible)]
    urls = list(dict.fromkeys(c["url"] for c in cites))[:8]
    previews = {}
    if urls:
        with ThreadPoolExecutor(max_workers=6) as ex:
            previews = dict(zip(urls, ex.map(page_preview, urls)))
    urls = [u for u in urls if not previews.get(u, ("", False))[1]]      # dead pages out
    sources = []
    for u in urls:
        cs = [c for c in cites if c["url"] == u]
        snip = next((c["snippet"] for c in cs if c.get("snippet")), "")
        snip = re.sub(r"\s+", " ", snip).strip()
        sources.append({"url": u, "title": next((c["title"] for c in cs if c.get("title")), ""),
                        "host": _host(u), "image": previews.get(u, ("", False))[0],
                        "snippet": (snip[:217] + "…") if len(snip) > 220 else snip})
    index = {u: i for i, u in enumerate(urls)}
    # final position of each citation: after trailing punctuation / closing bold
    edits: dict = {}
    for c in cites:
        if c["url"] not in index:
            continue
        start, at = c.get("start"), c["at"]
        if start is None:
            while at < len(visible):
                nxt = visible[at:at + 2]
                if nxt == "**":
                    at += 2
                elif visible[at] in ".;:)!?»":
                    at += 1
                else:
                    break
            start = at
        key = (start, at)
        edits.setdefault(key, [])
        if index[c["url"]] not in edits[key]:
            edits[key].append(index[c["url"]])
    out, prev_end, last_in_para = [], 0, None
    for (start, at), idxs in sorted(edits.items()):
        if start < prev_end:
            continue                                   # overlapping spans: keep the first
        chunk = visible[prev_end:start]
        if "\n" in chunk:
            last_in_para = None
        if idxs != last_in_para:                       # same source again in the paragraph: skip
            out.append(chunk.rstrip(" ") + f" [[c:{','.join(map(str, idxs))}]]")
        else:
            out.append(chunk)
            last_in_para = idxs
        prev_end = at
    out.append(visible[prev_end:])
    return "".join(out), sources


def render_md(text: str, sources=None) -> str:
    """Tiny, safe markdown: escape everything, then **bold** and '- ' bullets.
    Mirrors the JS renderer in the panel so stored and live replies match.
    [[c:i]] citation markers become source pills (or vanish without sources)."""
    out = escape(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^\s*[-*]\s+", "• ", out)
    out = re.sub(r"(?m)^#{1,6}\s*", "", out)
    out = re.sub(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"(?m)^\s*\[\[(DESTINATIONS|HOTELS)\]\]\s*$",
                 lambda m: f"<div class='slot' data-slot='{m.group(1).lower()}'></div>", out)
    out = _CITE_RE.sub(lambda m: (" " + cite_html([int(i) for i in m.group(1).split(",") if i],
                                                  sources or [])) if sources else "", out)
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
        out.append({
            "nom": nom,
            "accroche": str(d.get("accroche") or "")[:90],
            "ideal_pour": str(d.get("ideal_pour") or "")[:120],
            "bemol": str(d.get("bemol") or "")[:120],
            "moment": str(d.get("moment") or "")[:120],
            "guide": guide["url"] if guide and guide.get("kind") in ("destination", "pays") else "",
            "page_url": str(d.get("page_url") or "").strip(),
        })
        if len(out) == 4:
            break
    return out


def add_destination_images(dests: list, known: set) -> list:
    """Destination photo = share image of the destination's page at a
    Canadian wholesaler, shown live (never copied)."""
    for d in dests:
        d["page_url"] = _clean_url(d.get("page_url"), known, WHOLESALERS)
    urls = list({d["page_url"] for d in dests if d.get("page_url")})
    previews = {}
    if urls:
        with ThreadPoolExecutor(max_workers=4) as ex:
            previews = dict(zip(urls, ex.map(page_preview, urls)))
    for d in dests:
        img, dead = previews.get(d.get("page_url"), ("", False))
        if dead:
            d["page_url"] = ""
        d["photo"] = img
        d["photo_from"] = (urlparse(d["page_url"]).hostname or "").removeprefix("www.") if img else ""
    return dests


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
               f"referrerpolicy='no-referrer' onerror=\"this.parentNode.remove()\">"
               f"<figcaption>{escape(d.get('photo_from') or '')}</figcaption></figure>"
               ) if d.get("photo") else "<div class='dc-pic dc-nopic'>🌴</div>"
        rows = "".join(f"<div class='dc-row'><span>{k}</span>{escape(v)}</div>" for k, v in (
            ("Idéal pour", d.get("ideal_pour")), ("À tes dates", d.get("moment")),
            ("Bémol", d.get("bemol"))) if v)
        guide = ""
        if d.get("page_url"):
            guide += (f"<a class='dc-guide' href=\"{escape(d['page_url'])}\" target='_blank' "
                      f"rel='noopener nofollow'>Voir sur {escape(d.get('photo_from') or 'le site')} ↗</a>")
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
    """Share image + the page's own photo list (JSON-LD "image"), the way a
    link preview reads a page. Nothing is stored beyond the URLs."""
    def __init__(self):
        super().__init__()
        self.image = ""
        self.ld: list = []
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld = True
            self.ld.append("")
            return
        if tag != "meta" or self.image:
            return
        key = (a.get("property") or a.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "twitter:image") and a.get("content"):
            self.image = a["content"].strip()

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld and self.ld:
            self.ld[-1] += data


def _ld_images(chunks: list) -> list:
    out = []

    def walk(x):
        if isinstance(x, dict):
            img = x.get("image") or x.get("photo")
            for v in (img if isinstance(img, list) else [img]):
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, dict) and isinstance(v.get("url") or v.get("contentUrl"), str):
                    out.append(v.get("url") or v.get("contentUrl"))
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for c in chunks:
        try:
            walk(json.loads(c))
        except ValueError:
            continue
    return out


_GALLERY: dict = {}             # page url -> [image urls] (process-wide)


def _allowed_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    domains = [d.strip().lower() for d in settings.PORTAL_ASSISTANT_SEARCH_DOMAINS.split(",") if d.strip()]
    return any(host == d or host.endswith("." + d) for d in domains)


WHOLESALERS = ("transat.com", "vacancesaircanada.com", "aircanadavacations.com",
               "sunwing.ca", "westjet.com")


def _on(url: str, domains) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _clean_url(url, known, domains=None) -> str:
    """Keep a model-given URL only if the search cited it or it sits on an
    allowed site; prefer the cited page on the same site when unambiguous."""
    url = str(url or "").strip()
    if not url.startswith("https://"):
        return ""
    if url not in known and not _allowed_domain(url):
        same = [k for k in known if urlparse(k).hostname == urlparse(url).hostname]
        if len(same) == 1:
            url = same[0]
    ok = url in known or _allowed_domain(url)
    if domains is not None:
        ok = ok and _on(url, domains)
    return url if ok else ""


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
                p.feed(r.read(900_000).decode("utf-8", "ignore"))
                cand = urljoin(url, p.image) if p.image else ""
                if cand.startswith("https://"):
                    img = cand
                pics = [img] if img else []
                for u in _ld_images(p.ld):
                    u = urljoin(url, u.strip())
                    if u.startswith("https://") and u not in pics:
                        pics.append(u)
                _GALLERY[url] = pics[:8]
    except urllib.error.HTTPError as exc:
        dead = exc.code in (404, 410)
        if not dead:            # 403/5xx: site blocks bots or is down — link may still work
            log.info("preview unavailable (%s) for %s", exc.code, url)
    except Exception as exc:  # noqa: BLE001 — a missing photo is fine
        log.info("preview unavailable for %s: %s", url, exc)
    if len(_OG_CACHE) > 500:
        _OG_CACHE.clear()
        _GALLERY.clear()
    _OG_CACHE[url] = (img, dead)
    return img, dead


def page_gallery(url: str) -> list:
    """Photos a page shows of itself (share image + JSON-LD images), live URLs."""
    if url not in _OG_CACHE:
        page_preview(url)
    if url in _GALLERY:
        return _GALLERY[url]
    img = _OG_CACHE.get(url, ("", False))[0]
    return [img] if img else []


def _money(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n:,.0f}".replace(",", "\u202f") + "\u00a0$"


def _txt(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def parse_pistes(raw: str, sources: list) -> list:
    """Validate the model's <pistes> JSON into clean hotel card dicts (max 5)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    known = {s["url"] for s in sources}
    cards = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not str(d.get("hotel") or "").strip():
            continue
        url = _clean_url(d.get("source_url"), known)
        fiche = _clean_url(d.get("fiche_url"), known, WHOLESALERS)
        fiches = []
        for f in (d.get("fiches") or []) if isinstance(d.get("fiches"), list) else []:
            f = _clean_url(f, known, WHOLESALERS)
            if f and f != fiche and f not in fiches:
                fiches.append(f)
        avis_url = _clean_url(d.get("avis_url"), known, ("tripadvisor.ca", "tripadvisor.com", "tripadvisor.fr"))
        ta_seen = bool(avis_url) or any("tripadvisor" in k for k in known)
        try:                       # a rating counts only if TripAdvisor was actually consulted
            note = float(d.get("note")) if d.get("note") not in (None, "") else None
            note = note if note and 0 < note <= 5 and ta_seen else None
        except (TypeError, ValueError):
            note = None
        try:
            avis = int(d.get("avis")) if note and d.get("avis") not in (None, "") else None
        except (TypeError, ValueError):
            avis = None
        try:
            nuits = int(d.get("nuits")) if d.get("nuits") not in (None, "") else None
        except (TypeError, ValueError):
            nuits = None
        pts = d.get("points_forts") if isinstance(d.get("points_forts"), list) else []
        cards.append({
            "hotel": _txt(d.get("hotel"), 90),
            "destination": _txt(d.get("destination"), 80),
            "titre": _txt(d.get("titre"), 60),
            "type": _txt(d.get("type"), 70),
            "resume": _txt(d.get("resume"), 360),
            "points_forts": [_txt(p, 70) for p in pts if _txt(p, 70)][:3],
            "bemol": _txt(d.get("bemol"), 140),
            "depart": _txt(d.get("depart"), 40),
            "nuits": nuits,
            "prix_personne": d.get("prix_personne"),
            "total": d.get("total"),
            "voyageurs": _txt(d.get("voyageurs"), 40),
            "taxes": d.get("taxes") if d.get("taxes") in ("incluses", "en sus") else None,
            "prix_note": _txt(d.get("prix_note"), 140),
            "source_url": url,
            "fiche_url": fiche,
            "fiches": fiches[:3],
            "avis_url": avis_url,
            "note": note, "avis": avis,
            "adresse": _txt(d.get("adresse"), 120),
            "a_confirmer": _txt(d.get("a_confirmer"), 120),
        })
        if len(cards) == 5:
            break
    return cards


def _card_urls(c: dict) -> list:
    return [u for u in dict.fromkeys([c.get("fiche_url"), *c.get("fiches", []),
                                      c.get("source_url"), c.get("avis_url")]) if u]


def add_images(cards: list, titles: dict | None = None) -> list:
    """Real hotel photos, never copied: what the hotel's pages at Canadian
    wholesalers show (share image + their own photo list), displayed live
    from their sites with attribution. Dead links (404/410) are dropped.
    Also builds each card's source list (for the source pill)."""
    titles = titles or {}
    urls = list({u for c in cards for u in _card_urls(c)})
    previews = {}
    if urls:
        with ThreadPoolExecutor(max_workers=8) as ex:
            previews = dict(zip(urls, ex.map(page_preview, urls)))
    for c in cards:
        for key in ("fiche_url", "source_url", "avis_url"):
            if c.get(key) and previews.get(c[key], ("", False))[1]:
                log.info("assistant: dead link removed from card: %s", c[key])
                c[key] = ""
        c["fiches"] = [f for f in c.get("fiches", []) if not previews.get(f, ("", False))[1]]
        gallery, seen = [], set()
        for u in _card_urls(c):
            if not _on(u, WHOLESALERS):
                continue
            pics = page_gallery(u) or ([previews[u][0]] if previews.get(u, ("", False))[0] else [])
            for p in pics:
                if p not in seen:
                    seen.add(p)
                    gallery.append({"src": p, "host": _host(u), "page": u})
        c["gallery"] = gallery[:10]
        c["image"] = gallery[0]["src"] if gallery else ""
        c["image_from"] = gallery[0]["host"] if gallery else ""
        c["sources"] = [{"url": u, "host": _host(u), "title": titles.get(u, ""),
                         "image": previews.get(u, ("", False))[0], "snippet": ""}
                        for u in _card_urls(c)]
    return cards


def _price_line(c: dict) -> str:
    pp, tot = _money(c.get("prix_personne")), _money(c.get("total"))
    if not (pp or tot):
        return ""
    bits = []
    if pp:
        bits.append(f"{pp}/personne")
    if tot:
        bits.append(f"soit {tot}" + (f" pour {escape(c['voyageurs'])}" if c.get("voyageurs") else ""))
    when = ", ".join(x for x in (escape(c.get("depart") or ""),
                                 f"{c['nuits']} nuits" if c.get("nuits") else "") if x)
    tax = {"incluses": "taxes comprises", "en sus": "taxes en sus"}.get(c.get("taxes"), "")
    tail = " · ".join(x for x in (when, tax) if x)
    note = f" {escape(c['prix_note'])}" if c.get("prix_note") else ""
    return (f"<div class='hc-price'><b>Tarif repéré :</b> {', '.join(bits)}"
            f"{(' (' + tail + ')') if tail else ''}.{note}</div>")


def _rating(c: dict) -> str:
    if not c.get("note"):
        return ""
    rv = c.get("avis")
    return (f"★ {str(c['note']).replace('.', ',')}"
            + (f" · {rv:,} avis".replace(",", " ") if rv else "") + " TripAdvisor")


def hotel_detail_html(c: dict) -> str:
    """Side panel (desktop) / sheet (mobile) for one hotel, chatgpt.com style."""
    pics = "".join(
        f"<figure class='hd-shot'><img src=\"{escape(g['src'])}\" alt='' loading='lazy' "
        "referrerpolicy='no-referrer' onerror=\"this.parentNode.remove()\">"
        f"<figcaption>Photo : {escape(g['host'])}</figcaption></figure>"
        for g in c.get("gallery") or [])
    gal = (f"<div class='hd-gal'>{pics}</div>" if pics
           else "<div class='hd-gal hd-nogal'>Photos sur la fiche du grossiste</div>")
    meta = " · ".join(escape(x) for x in (_rating(c), c.get("type")) if x)
    maps = ("https://www.google.com/maps/search/?api=1&query="
            + quote_plus(f"{c['hotel']} {c.get('destination') or ''}"))
    fiche = c.get("fiche_url") or (c.get("fiches") or [""])[0]
    btns = (f"<a class='hd-b hd-main' href='{SEARCH_URL}'>Voir les prix avec mon rabais</a>"
            f"<a class='hd-b' href=\"{escape(maps)}\" target='_blank' rel='noopener'>Itinéraire</a>"
            + (f"<a class='hd-b' href=\"{escape(fiche)}\" target='_blank' rel='noopener nofollow'>"
               f"Fiche {escape(_host(fiche))} ↗</a>" if fiche else ""))
    rows = ""
    if c.get("adresse"):
        rows += f"<div class='hd-row'><span>📍</span>{escape(c['adresse'])}</div>"
    elif c.get("destination"):
        rows += f"<div class='hd-row'><span>📍</span>{escape(c['destination'])}</div>"
    if c.get("depart") or c.get("nuits"):
        rows += ("<div class='hd-row'><span>🗓️</span>" + escape(", ".join(x for x in (
            c.get("depart") or "", f"{c['nuits']} nuits" if c.get("nuits") else "") if x)) + "</div>")
    desc = f"<p>{escape(c['resume'])}</p>" if c.get("resume") else ""
    if c.get("points_forts"):
        desc += "<ul>" + "".join(f"<li>{escape(p)}</li>" for p in c["points_forts"]) + "</ul>"
    if c.get("bemol"):
        desc += f"<p class='hd-bemol'><b>Bémol :</b> {escape(c['bemol'])}</p>"
    price = _price_line(c)
    conf = (f"<p class='hd-conf'>À confirmer : {escape(c['a_confirmer'])}</p>"
            if c.get("a_confirmer") else "")
    srcs = "".join(f"<a href=\"{escape(x['url'])}\" target='_blank' rel='noopener nofollow'>"
                   f"{escape(x['host'])}</a>" for x in c.get("sources") or [])
    return (f"<div class='hd'>{gal}<div class='hd-body'>"
            f"<h3 class='hd-name'>{escape(c['hotel'])}</h3>"
            + (f"<div class='hd-meta'>{meta}</div>" if meta else "")
            + f"<div class='hd-btns'>{btns}</div>{rows}"
            + f"<div class='hd-desc'>{desc}{price}{conf}</div>"
            + (f"<div class='hd-srcs'><span>Sources</span>{srcs}</div>" if srcs else "")
            + "<p class='hd-note'>Photos et infos affichées en direct depuis les sites cités. "
              "Prix publics à titre indicatif : ton Rabais Récompense s’applique dans notre moteur.</p>"
            + "</div></div>")


def cards_html(cards) -> str:
    """Hotel recommendations as a numbered, structured list (chatgpt.com
    style): photo, name, angle, summary, price seen, source pill. Clicking a
    hotel opens its detail panel."""
    if not cards:
        return ""
    out = []
    for i, c in enumerate(cards, 1):
        img = c.get("image") or ""
        pic = (f"<button type='button' class='hc-pic hc-open' aria-label='Voir {escape(c['hotel'])}'>"
               f"<img src=\"{escape(img)}\" alt='' loading='lazy' referrerpolicy='no-referrer' "
               "onerror=\"this.parentNode.classList.add('hc-nopic');this.remove()\">"
               + (f"<span class='hc-n'>+{len(c['gallery']) - 1}</span>" if len(c.get("gallery") or []) > 1 else "")
               + "</button>") if img else \
              "<button type='button' class='hc-pic hc-nopic hc-open' aria-label='Voir'>🏝️</button>"
        sub = escape(c.get("titre") or c.get("type") or c.get("destination") or "")
        loc = " · ".join(escape(x) for x in (c.get("destination"), _rating(c)) if x)
        pill = cite_html(list(range(len(c.get("sources") or []))), c.get("sources") or [])
        conf = (f"<div class='hc-conf'>À confirmer : {escape(c['a_confirmer'])}</div>"
                if c.get("a_confirmer") else "")
        out.append(
            f"<div class='hcard'>{pic}<div class='hc-body'>"
            f"<button type='button' class='hc-name hc-open'>{i}. {escape(c['hotel'])}</button>"
            + (f"<div class='hc-sub'>{sub}</div>" if sub else "")
            + (f"<div class='hc-loc'>{loc}</div>" if loc else "")
            + (f"<div class='hc-desc'>{escape(c['resume'])}</div>" if c.get("resume") else "")
            + _price_line(c) + conf
            + f"<div class='hc-foot'>{pill}<a class='hc-go' href='{SEARCH_URL}'>Voir les prix →</a></div>"
            + f"</div><template class='hc-detail'>{hotel_detail_html(c)}</template></div>")
    cmp_btn = cmp = ""
    if len(cards) >= 2:
        def cell(v):
            return f"<td>{v or '—'}</td>"
        rows = [
            ("Pour qui", [escape(c.get("titre") or "") for c in cards]),
            ("Type", [escape(c.get("type") or "") for c in cards]),
            ("TripAdvisor", [f"{str(c['note']).replace('.', ',')}/5" if c.get("note") else ""
                             for c in cards]),
            ("Prix / pers.", [_money(c.get("prix_personne")) for c in cards]),
            ("Total", [_money(c.get("total")) for c in cards]),
            ("Bémol", [escape(c.get("bemol") or "") for c in cards]),
        ]
        thead = "".join(f"<th>{escape(c['hotel'])}</th>" for c in cards)
        tbody = "".join(f"<tr><th>{k}</th>{''.join(cell(v) for v in vals)}</tr>"
                        for k, vals in rows if any(vals))
        cmp_btn = "<button type='button' class='pc-cmpbtn'>Comparer</button>"
        cmp = (f"<div class='pc-cmp' hidden><table><thead><tr><th></th>{thead}</tr></thead>"
               f"<tbody>{tbody}</tbody></table></div>")
    return (f"<div class='pcards hlist'><div class='pc-head'><span>Hôtels à regarder</span>{cmp_btn}</div>"
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
    body, tail = _fill_slots(render_md(m.get("marked") or m.get("text", ""), m.get("sources")),
                             destinations_html(m.get("destinations")),
                             cards_html(m.get("cards")))
    return (f"<div class='msg ai'><div>{body}</div>{tail}"
            f"{_sources_html(m.get('sources'))}{rate_html(m.get('id', ''), m.get('rating', 0))}"
            f"{questions_html(m.get('questions')) if last else ''}</div>")


def assistant_panel(client) -> str:
    """The portal home: a full-screen chat (chatgpt.com layout) — centered
    empty state with suggestions, then the thread with a sticky composer."""
    thread = list(client.assistant_chat or [])
    hi = escape(_first_name(client))
    msgs = "".join(_bubble(m, last=(i == len(thread) - 1)) for i, m in enumerate(thread))
    chip_list = _SUGGESTIONS if not thread else (
        thread[-1].get("suivis") or [] if thread[-1].get("role") == "assistant" else [])
    chips = ("<div class='chips' id='aChips'>" + "".join(
        f"<button type='button' class='chip'>{escape(s)}</button>" for s in chip_list)
        + "</div>") if chip_list else ""
    reset = ("<button type='button' class='areset' id='aReset'>Nouvelle conversation</button>"
             if thread else "")
    human = "<button type='button' class='areset' id='aHuman'>💬 Conseiller</button>"
    if is_tester(client):
        human = "<a class='areset' href='/portail/assistant/comparer'>🧪 Test A/B</a>" + human
    messenger = json.dumps(settings.MESSENGER_URL)
    return f"""
<div class='assist chat{'' if thread else ' empty'}' id='aBox'>
  <div class='c-bar'><span class='c-title'>Ton assistant voyage</span>{human}{reset}
    <a class='c-search' href='{SEARCH_URL}'>🔎 Rechercher un forfait</a></div>
  <div class='a-toast' id='aToast' hidden></div>
  <div class='c-empty'>
    <h2>Où veux-tu partir{(', ' + hi) if hi else ''}?</h2>
    <p>Décris ton voyage (budget, dates, voyageurs, ce qui compte pour toi) : je te propose
      des destinations et des hôtels. Ensuite, réserve avec ton rabais dans notre moteur.</p>
  </div>
  <div class='a-log' id='aLog' aria-live='polite'>{msgs}</div>
  <div class='c-dock'>
    {chips}
    <form class='a-form' id='aForm' autocomplete='off'>
      <textarea id='aIn' rows='1' maxlength='{_MAX_LEN}' placeholder='Décris ton voyage…'
        aria-label='Ta question'></textarea>
      <button class='btn a-send' type='submit' id='aSend' aria-label='Envoyer'>↑</button>
    </form>
    <div class='c-note'>Assistant IA : il peut se tromper. Prix publics à titre indicatif ;
      ton Rabais Récompense s’applique dans notre moteur.</div>
  </div>
</div>
<div class='hp-ov' id='hpOv' hidden></div>
<aside class='hpanel' id='hPanel' hidden aria-label='Fiche de l’hôtel'>
  <div class='hp-top'><button type='button' class='hp-x' id='hpX' aria-label='Fermer'>✕</button></div>
  <div id='hpBody'></div>
</aside>
<script>
(function(){{
 var box=document.getElementById('aBox'),log=document.getElementById('aLog'),f=document.getElementById('aForm'),
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
 function bottom(){{window.scrollTo(0,document.documentElement.scrollHeight);}}
 function add(cls,t){{var d=document.createElement('div');d.className='msg '+cls;
   d.textContent=t;log.appendChild(d);return d;}}
 function grow(){{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,140)+'px';}}
 inp.addEventListener('input',grow);
 inp.addEventListener('keydown',function(e){{
   if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();f.requestSubmit();}}}});
 if(!box.classList.contains('empty'))bottom();
 function ask(q){{
   q=(q||'').trim(); if(!q||send.disabled)return;
   if(chips){{chips.remove();chips=null;}}
   box.classList.remove('empty');
   log.querySelectorAll('.refine,.qset').forEach(function(x){{x.remove();}});
   var me=add('me',q); inp.value=''; grow();
   me.style.scrollMarginTop='96px';me.scrollIntoView({{block:'start',behavior:'smooth'}});
   send.disabled=true; inp.disabled=true;
   var out=add('ai typing',''),body=document.createElement('div'),st=document.createElement('div');
   st.className='astatus';out.appendChild(st);out.appendChild(body);
   var raw='',buf='';
   function handle(ev){{
     if(ev.t==='status'){{st.textContent=ev.v;out.classList.remove('typing');}}
     else if(ev.t==='text'){{out.classList.remove('typing');st.textContent='';raw+=ev.v;body.innerHTML=md(raw);}}
     else if(ev.t==='marked'&&ev.v){{body.innerHTML=ev.v;}}
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
 var touch=window.matchMedia&&matchMedia('(hover: none)').matches;
 function place(c){{var r=c.getBoundingClientRect();
   c.classList.toggle('down',r.top<380);c.classList.toggle('right',r.left+300>window.innerWidth);}}
 log.addEventListener('mouseover',function(e){{var c=e.target.closest('.cite');if(c)place(c);}});
 log.addEventListener('focusin',function(e){{var c=e.target.closest('.cite');if(c)place(c);}});
 document.addEventListener('click',function(e){{
   document.querySelectorAll('.cite.open').forEach(function(x){{if(!x.contains(e.target))x.classList.remove('open');}});}});
 var hp=document.getElementById('hPanel'),hpb=document.getElementById('hpBody'),hov=document.getElementById('hpOv');
 function closeHotel(){{hp.hidden=true;hov.hidden=true;document.body.classList.remove('has-panel');
   log.querySelectorAll('.hcard.on').forEach(function(x){{x.classList.remove('on');}});}}
 function openHotel(card){{var t=card.querySelector('template.hc-detail');if(!t)return;
   log.querySelectorAll('.hcard.on').forEach(function(x){{x.classList.remove('on');}});card.classList.add('on');
   hpb.innerHTML=t.innerHTML;hp.hidden=false;hov.hidden=false;document.body.classList.add('has-panel');
   hp.scrollTop=0;var g=hpb.querySelector('.hd-gal');
   if(g&&g.querySelectorAll('.hd-shot').length>1){{
     ['‹','›'].forEach(function(ch,i){{var b=document.createElement('button');b.type='button';
       b.className='hd-arr '+(i?'nx':'pv');b.textContent=ch;b.setAttribute('aria-label',i?'Photo suivante':'Photo précédente');
       b.onclick=function(){{g.scrollBy({{left:(i?1:-1)*g.clientWidth,behavior:'smooth'}});}};
       g.parentNode.insertBefore(b,g.nextSibling);}});}}
   document.getElementById('hpX').focus();}}
 document.getElementById('hpX').addEventListener('click',closeHotel);
 hov.addEventListener('click',closeHotel);
 document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&!hp.hidden)closeHotel();}});
 log.addEventListener('click',function(e){{
   var ho=e.target.closest('.hc-open');if(ho){{openHotel(ho.closest('.hcard'));return;}}
   var ca=e.target.closest('.cite-a');
   if(ca&&touch&&!ca.parentNode.classList.contains('open')){{e.preventDefault();place(ca.parentNode);
     ca.parentNode.classList.add('open');return;}}
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
   var pc=[].slice.call(log.querySelectorAll('.pc-name,.hc-name')).slice(-3).map(function(x){{return x.textContent;}});
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
    sources, searches, wrote, cites, base = [], 0, False, [], 0
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
        for block in final.content or []:            # text blocks == streamed text, in order
            if getattr(block, "type", "") != "text":
                continue
            base += len(block.text or "")
            for c in getattr(block, "citations", None) or []:
                if getattr(c, "url", None):
                    cites.append({"start": None, "at": base, "url": c.url,
                                  "title": getattr(c, "title", None) or "",
                                  "snippet": getattr(c, "cited_text", None) or ""})
        if final.stop_reason == "pause_turn":
            convo = convo + [{"role": "assistant", "content": final.content}]
            continue
        break
    yield ("meta", {"sources": sources, "searches": searches, "cites": cites, "chars": base})


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
    sources, searches, cites, base = [], 0, [], 0
    for item in getattr(final, "output", None) or []:
        if getattr(item, "type", "") == "web_search_call":
            searches += 1
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") != "output_text":
                continue
            for a in getattr(part, "annotations", None) or []:
                url = getattr(a, "url", None)
                if getattr(a, "type", "") != "url_citation" or not url:
                    continue
                if url not in {s["url"] for s in sources}:
                    sources.append({"url": url, "title": getattr(a, "title", None) or ""})
                si, ei = getattr(a, "start_index", None), getattr(a, "end_index", None)
                if isinstance(si, int) and isinstance(ei, int) and ei >= si:
                    cites.append({"start": base + si, "at": base + ei, "url": url,
                                  "title": getattr(a, "title", None) or "", "snippet": ""})
            base += len(part.text or "")
    yield ("meta", {"sources": sources[:6], "searches": searches, "cites": cites, "chars": base})


_RUNNERS = {"anthropic": _run_anthropic, "openai": _run_openai}


def _history_msgs(history: list, q: str) -> list:
    def ctx(m):
        extra = ""
        for key, tag in (("destinations", "destinations"), ("cards", "pistes"), ("questions", "questions")):
            if m.get(key):
                data = [{k: v for k, v in x.items() if k in ("nom", "hotel", "destination", "prix_personne", "titre",
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


    def system_for(search: bool) -> list:
        """[static, dynamic]: the static block (rules + catalogue) is identical
        across members and questions, so it is prompt-cached."""
        static = SYSTEM_PROMPT.format(
            today=_today(), search_rule=_SEARCH_ON if search else _SEARCH_OFF,
            catalogue=catalogue, excerpts="")
        if not _full():
            static += STYLE_CONCIS
        dynamic = ""
        if excerpts:
            dynamic += "Extraits de nos pages liés à la conversation :\n" + excerpts + "\n\n"
        if name:
            dynamic += f"Le membre s'appelle {name}."
        return [static, dynamic.strip()]

    parts: list[str] = []          # everything the model wrote
    shown = [0]                    # chars of `parts` already sent
    meta = {"sources": [], "searches": 0, "cites": []}

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
    marked = ""
    if meta.get("cites") and meta.get("chars") != len(full):
        log.info("assistant: citation offsets skipped (%s chars vs %s streamed)", meta.get("chars"), len(full))
    elif meta.get("cites"):
        marked, cited = mark_citations(full[:cut], meta["cites"])
        if cited:
            got = {c["url"] for c in cited}
            rest = [x for x in sources if _clean_src_url(x["url"]) not in got]
            sources = cited + rest[:max(0, 8 - len(cited))]
            marked = marked.strip()
            yield _ev("marked", render_md(marked, sources))
        else:
            marked = ""
    dm = _DEST_RE.search(full)
    dests = parse_destinations(dm.group(1).strip()) if dm else []
    if dests:
        dests = add_destination_images(dests, {x["url"] for x in sources})
        yield _ev("dests", destinations_html(dests))
    m = _PISTES_RE.search(full)
    cards = parse_pistes(m.group(1).strip(), sources) if m else []
    if cards:
        yield _ev("status", "📸 Je prépare les fiches…")
        cards = add_images(cards, {x["url"]: x.get("title", "") for x in sources})
        yield _ev("cards", cards_html(cards))
    guides = []                       # our own pages are not shown in answers anymore
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
            {"role": "assistant", "id": mid, "text": answer, "marked": marked, "cards": cards,
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
