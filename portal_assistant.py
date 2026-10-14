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
import unicodedata
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
from db import AssistantChat, AssistantFeedback, Client, SessionLocal
import pexels
import places
import site_knowledge as kb
import trip_brief
import wholesaler_photos
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

TA MISSION : être le meilleur conseiller voyage que le membre ait connu. Tu
fais une VRAIE recherche, tu RECOMMANDES des hôtels précis avec des infos
concrètes et vérifiées, tu tranches (« Mon choix pour toi ») et tu poses
ensuite les bonnes questions. Pas de réponse générique.

1. DONNE DE LA VALEUR D'ABORD, QUESTIONS À LA FIN. Même si la demande est
   incomplète, réponds tout de suite avec de vraies recommandations, puis
   1 à 3 questions à choix. Ne te contente JAMAIS de poser des questions.
   Seule exception : une demande vide (« je veux partir ») : 3 grandes idées
   variées + tes questions. S'il manque la durée du séjour ou le budget,
   demande-les dans tes questions à choix (la durée est nécessaire aux
   prix des forfaits). Si l'année d'une date est ambiguë, prends la
   prochaine occurrence et dis-le.

2. LIS ENTRE LES LIGNES. « bonne bouffe » = restos à la carte réputés,
   gastronomie locale (et souvent : sortir manger en ville plutôt qu'un tout
   inclus) ; « plein d'activités » = excursions, nature, sports nautiques ;
   « tranquille » = petit complexe, adultes seulement ; « avec les enfants »
   = mini-club, plage en pente douce, glissades. Regarde les DATES : Fêtes
   (prix au sommet, réservations, ambiance du 31), saisons (baleines dans le
   Pacifique mexicain de décembre à mars, ouragans de juin à novembre,
   sargasses côté Caraïbes au printemps et à l'été). Dis franchement si le
   budget est serré pour ses dates, et ce qui l'aiderait (partir le 29 ou le
   30, chambre seulement, autre destination).

3. RECHERCHE (outil web_search) comme un vrai conseiller, en plusieurs
   étapes : {search_rule}
   a) les meilleurs hôtels pour SES critères (guides, magazines voyage,
      TripAdvisor, sites des hôtels) : réputation, restos, plage, ambiance,
      adultes ou familles, rénovations ;
   b) la fiche de chaque hôtel chez les grossistes canadiens (Transat,
      Vacances Air Canada, Vacances Sunwing, Vacances WestJet) ;
   c) quand tu as dates + voyageurs : les prix publics de ces grossistes
      au départ de sa ville ;
   d) les vols directs et ce qui se passe à ses dates.
   N'invente jamais un hôtel, un prix, une note ou un détail : ils viennent
   de tes recherches. Cite tes sources dans le texte.

4. HÔTELS : dès qu'une destination ou une région est connue, recommande
   TOUJOURS 3 à 5 hôtels précis, même sans dates ni budget. Chacun a son
   angle (« Mon préféré pour ton style », « Meilleur choix luxe », « Meilleur
   pour calme et gastronomie », « Meilleur rapport qualité-prix »…), jamais
   deux fois le même profil. Ils respectent ses contraintes (adultes,
   enfants, budget) ou tu dis clairement l'exception. Ne suppose pas
   « adultes seulement » sans qu'il le demande (au plus une option). Pour
   chaque hôtel : clientèle, formule si vérifiée, restos et activités
   marquants, 3 forces, 1 ou 2 faiblesses réelles.

5. PRIX : seulement ceux vus chez les grossistes canadiens ci-dessus, avec
   leurs vraies dates et leur ville de départ, même s'ils ne correspondent
   pas exactement à ses dates (dis-le). Distingue toujours prix vu et
   estimation. Jamais garantis. Quand tu donnes des prix, termine par :
   « Ces prix sont des prix publics. Cherche le même forfait dans notre
   moteur : c'est souvent moins cher, avec ton Rabais Récompense déjà
   appliqué. »

6. STRUCTURE EN ONGLETS. Ta réponse s'affiche en onglets. Commence chaque
   onglet par son marqueur, seul sur sa ligne, dans cet ordre :
   [[TAB:info]] Info générale : ce que tu as compris, ton constat franc
     (saison, budget, dates) ; pour une demande large, [[DESTINATIONS]]
     seul sur sa ligne puis 1 puce par destination ;
   [[TAB:hotels]] Hôtels retenus : 1 puce d'intro, [[HOTELS]] seul sur sa
     ligne (OBLIGATOIRE : les fiches s'y affichent ; ne décris PAS les hôtels
     dans le texte), puis **Mon choix pour toi** seul sur sa ligne et 2 ou 3
     puces (lequel, pourquoi, quelle formule) ;
   [[TAB:prix]] Comparaison des prix : [[PRIX]] seul sur sa ligne si tu as
     trouvé des prix, 2 ou 3 puces de lecture (dates, ville de départ,
     estimation ou prix vu), [[BUDGET]] seul sur sa ligne si le budget est
     connu, puis la phrase obligatoire sur les prix publics ;
   [[TAB:sejour]] Ton séjour : 4 à 6 puces (restos, excursions, moments
     forts à ses dates) ;
   [[TAB:vols]] Vols suggérés : 3 ou 4 puces (vols directs depuis sa ville,
     durée, compagnies, conseil d'horaire).
   Omet un onglet s'il n'a rien d'utile. Question générale (saison,
   bagages, documents) ou court suivi : PAS d'onglets, juste des puces.
   CONCIS : des puces courtes (une idée, 20 mots max), pas de paragraphes,
   pas de répétition entre onglets. Chaque puce commence par un mot clé en
   gras suivi de « : » (ex. « - **Saison** : haute saison, prix au sommet »).
   Les prix vont dans [[PRIX]] (tableau), jamais en phrases. Chaque fait important vient de tes
   recherches et est cité (la source s'affiche en lien).

7. À LA FIN, appelle l'outil afficher_resultats UNE fois avec les données
   structurées (recherche, destinations, hotels, prix_vus, budget,
   questions, suivis) qui correspondent à ton texte. « recherche » = les
   critères du voyage pour notre moteur (aéroport de départ, destination,
   date, nuits, adultes, enfants et leurs âges). N'écris jamais ces données
   dans le texte.

Règles strictes :
- Ne recommande jamais une autre agence, un comparateur ou un site de
  réservation (Expedia, RedTag, Booking…). Pour réserver : notre moteur.
- Ne promets jamais que notre prix sera plus bas ni un montant de rabais
  précis : « souvent moins cher », « jusqu'à 5 % avant taxes ».
- Tu ne peux pas réserver ni confirmer une disponibilité.
- Passeports, visas, santé, avis aux voyageurs (Cuba surtout) : idée
  générale et renvoi à voyage.gc.ca.
- Réservation existante, changement, annulation, envie de parler à un
  humain : bouton « Conseiller » ou Messenger (onglet « Aide »).
- Hors voyage : ramène gentiment la conversation au voyage.

Style : français québécois chaleureux, tu tutoies. Format en PUCES « - »,
**gras** pour les noms et les mots clés. Pas de titres avec #, pas de
tableaux, pas de séparateurs ---, pas de liens écrits dans le texte (les
sources s'affichent automatiquement). Deux ou trois emojis au maximum.

CONTENU DE DU VOYAGEUR (info générale seulement ; ne renvoie pas à nos pages)
Destinations que nous couvrons :
{catalogue}
{excerpts}
"""

# Structured results: the model calls this tool once at the end of its
# answer; the cards/tables are rendered from its validated input (never by
# parsing free text).
_STR = {"type": "string"}
_STR_NULL = {"type": ["string", "null"]}
_NUM_NULL = {"type": ["number", "null"]}
RESULTS_TOOL = {
    "name": "afficher_resultats",
    "description": ("Affiche les fiches et tableaux de ta réponse (destinations, hôtels, "
                    "prix vus, budget, questions à choix, suivis). Appelle-le une seule fois, "
                    "à la fin, après ton texte."),
    "input_schema": {
        "type": "object",
        "properties": {
            "recherche": {"type": "object", "description": "critères pour notre moteur de forfaits",
                          "properties": {
                              "depart": {"type": ["string", "null"], "description": "code d'aéroport, ex. YUL"},
                              "destination": _STR_NULL,
                              "date_depart": {"type": ["string", "null"], "description": "AAAA-MM-JJ"},
                              "nuits": _NUM_NULL, "adultes": _NUM_NULL, "enfants": _NUM_NULL,
                              "ages_enfants": {"type": "array", "items": {"type": "number"}}}},
            "destinations": {"type": "array", "maxItems": 4, "items": {
                "type": "object", "required": ["nom"], "properties": {
                    "nom": _STR, "accroche": _STR, "ideal_pour": _STR, "bemol": _STR,
                    "moment": _STR, "page_url": _STR_NULL}}},
            "hotels": {"type": "array", "maxItems": 5, "items": {
                "type": "object", "required": ["hotel", "titre", "resume"], "properties": {
                    "hotel": {"type": "string", "description": "Nom exact de l'hôtel"},
                    "destination": _STR,
                    "titre": {"type": "string", "description": "Angle qui le distingue, < 50 caractères"},
                    "type": {"type": "string", "description": "ex. Tout-inclus boutique · adultes seulement"},
                    "resume": {"type": "string", "description": "2 phrases concrètes et utiles"},
                    "clientele": {"type": "string", "enum": ["adultes", "familles", "tous"],
                                  "description": "adultes seulement, axé familles, ou tous"},
                    "formule": {"type": ["string", "null"],
                                "description": "tout inclus, petit-déjeuner, chambre seule… si vérifié"},
                    "restos": {"type": "array", "items": _STR, "maxItems": 4,
                               "description": "faits marquants sur les restaurants (vérifiés)"},
                    "activites": {"type": "array", "items": _STR, "maxItems": 4,
                                  "description": "activités et ambiance (vérifiées)"},
                    "points_forts": {"type": "array", "items": _STR, "maxItems": 3},
                    "bemol": {"type": "string", "description": "1 ou 2 faiblesses réelles, honnêtes"},
                    "depart": _STR_NULL, "nuits": _NUM_NULL,
                    "prix_personne": _NUM_NULL, "total": _NUM_NULL, "voyageurs": _STR_NULL,
                    "taxes": {"type": ["string", "null"], "enum": ["incluses", "en sus", None]},
                    "prix_note": {"type": ["string", "null"],
                                  "description": "précision sur le prix : dates/ville réelles du tarif, « à partir de »"},
                    "source_url": {"type": ["string", "null"], "description": "page du grossiste où tu as vu le prix"},
                    "fiche_url": {"type": ["string", "null"], "description": "fiche de l'hôtel chez un grossiste canadien"},
                    "fiches": {"type": "array", "items": _STR, "maxItems": 3,
                               "description": "autres fiches de l'hôtel chez d'autres grossistes"},
                    "note": {"type": ["number", "null"], "description": "note TripAdvisor vue"},
                    "avis": _NUM_NULL,
                    "avis_url": _STR_NULL,
                    "adresse": {"type": ["string", "null"], "description": "seulement si vue dans tes sources"},
                    "a_confirmer": _STR_NULL}}},
            "prix_vus": {"type": "array", "maxItems": 6, "items": {
                "type": "object", "required": ["hotel", "total", "statut"], "properties": {
                    "hotel": _STR, "dates": _STR, "depart": _STR, "voyageurs": _STR,
                    "total": {"type": "number"},
                    "taxes": {"type": ["string", "null"], "enum": ["incluses", "en sus", None]},
                    "statut": {"type": "string", "enum": ["vu", "estimation"]},
                    "url": _STR_NULL}}},
            "budget": {"type": "array", "maxItems": 6, "items": {
                "type": "object", "required": ["poste", "montant"], "properties": {
                    "poste": _STR, "montant": {"type": "number"}}}},
            "questions": {"type": "array", "maxItems": 3, "items": {
                "type": "object", "required": ["q", "options"], "properties": {
                    "q": _STR, "options": {"type": "array", "items": _STR, "minItems": 2, "maxItems": 5}}}},
            "suivis": {"type": "array", "items": _STR, "maxItems": 3,
                       "description": "2 ou 3 raffinements rédigés comme SA demande, < 40 caractères"},
        },
        "required": ["hotels", "questions", "suivis"],
    },
}

# Appended when PORTAL_ASSISTANT_STYLE=concis.
STYLE_CONCIS = """
LONGUEUR : CONCISE (remplace les consignes de longueur ci-dessus) : sections
très courtes, 2 ou 3 destinations, 2 ou 3 hôtels, une phrase chacune.
"""

_SEARCH_ON = ("tu as plusieurs recherches : utilise-les toutes si la demande le mérite.")
_SEARCH_OFF = ("PAS de recherche web pour cette réponse (limite atteinte) : recommande des "
               "hôtels que tu connais bien, sans prix ni note précise, et envoie-le vers "
               "notre moteur pour les prix.")

# Starter examples on the empty chat: COMPLETE requests (destination, dates,
# travellers, length, budget, wishes) so members see what a good ask looks like.
_SUGGESTIONS = [
    ("🌮", "Mexique gourmand",
     "Mexique côte Pacifique, départ de Montréal le 31 décembre 2026, 7 nuits, 2 adultes, "
     "budget 5 000 à 7 000 $ pour deux. On veut très bien manger et faire des activités."),
    ("👨‍👩‍👧", "Punta Cana en famille",
     "Punta Cana pendant la relâche, du 6 au 13 mars 2027, 2 adultes et 2 enfants de 5 et 9 ans, "
     "départ de Montréal, budget 8 000 $ en tout. Mini-club et glissades d'eau essentiels."),
    ("💍", "Lune de miel",
     "Lune de miel en février 2027, 10 nuits, 2 adultes, adultes seulement, départ de Montréal, "
     "budget 9 000 $ pour deux. Plage magnifique, chambre avec vue et restos à la carte."),
    ("💸", "Cuba à petit prix",
     "Cuba en novembre 2026, 7 nuits, 2 adultes, départ de Montréal, maximum 2 500 $ pour deux. "
     "Une belle plage avant tout, tout inclus correct sans plus."),
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


_NUM_RE = re.compile(r"^\d{1,2}[.)]\s+")
_MARK_ONLY = re.compile(r"^\[\[(?:TAB:\w+|DESTINATIONS|HOTELS|PRIX|BUDGET)\]\]$")
_MARK_LEAD = re.compile(r"^(\[\[(?:TAB:\w+|DESTINATIONS|HOTELS|PRIX|BUDGET)\]\])\s*(.+)$")
_SENT_END = re.compile(r"[.!?](?:\s?\[\[c:[\d,]+\]\])*(?=\s+[A-ZÀ-ÖØ-Ý«*\d])")
_LABEL_RE = re.compile(r"^([^:*\[\]\n]{2,32}?)\s?:\s+(\S.*)$")


def _sentences(s: str) -> list:
    out, start = [], 0
    for m in _SENT_END.finditer(s):
        out.append(s[start:m.end()].strip())
        start = m.end()
    out.append(s[start:].strip())
    return [x for x in out if x]


def _lead(s: str) -> str:
    """« Saison : haute… » -> « **Saison** : haute… » (scannable bullets)."""
    if s.startswith("**"):
        return s
    m = _LABEL_RE.match(s)
    if m and re.search(r"[A-Za-zÀ-ÿ]", m.group(1)) and "http" not in m.group(1):
        return f"**{m.group(1).strip()}** : {m.group(2)}"
    return s


def tidy_text(text: str) -> str:
    """Normalize an answer into short bullets, whatever the model wrote:
    numbered lists and long paragraphs become one bullet per sentence, a
    tab marker followed by text goes on its own line, labels get bolded.
    Applied when rendering, so stored answers are fixed too."""
    out = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s or _MARK_ONLY.match(s):
            out.append(s)
            continue
        m = _MARK_LEAD.match(s)
        if m:
            out.append(m.group(1))
            s = m.group(2).strip()
        s = _NUM_RE.sub("- ", s)
        bullet = s[:2] in ("- ", "* ", "• ")
        body = s[2:].strip() if bullet else s
        if re.fullmatch(r"\*\*[^*]+\*\*\s*:?", body):          # sub-heading line
            out.append(body)
            continue
        if len(body) > 120:
            parts = _sentences(body)
            if len(parts) > 1:
                out += ["- " + _lead(p) for p in parts]
                continue
        out.append("- " + _lead(body) if bullet or len(body) > 110 else body)
    return "\n".join(out)


def _lists(html: str) -> str:
    """Runs of « • » lines -> <ul>, bold-only lines -> sub-headings."""
    html = re.sub(r"(?m)(?:^• .*(?:\n|$))+",
                  lambda m: "<ul class='rb'>" + "".join(
                      f"<li>{x[2:]}</li>" for x in m.group(0).rstrip("\n").split("\n")) + "</ul>",
                  html)
    return re.sub(r"(?m)(^|</ul>|</div>)<b>([^<]{1,60})</b>\s*:?\s*$\n?", r"\1<div class='rsub'>\2</div>", html)


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
    out = re.sub(r"(?m)^\s*\[\[(DESTINATIONS|HOTELS|PRIX|BUDGET)\]\]\s*$",
                 lambda m: f"<div class='slot' data-slot='{m.group(1).lower()}'></div>", out)
    out = re.sub(r"(?m)^\s*\[\[TAB:(info|hotels|prix|sejour|vols)\]\]\s*$",
                 lambda m: f"<div class='tabmark' data-tab='{m.group(1)}'></div>", out)
    out = re.sub(r"\[\[TAB:\w+\]\]", "", out)
    out = _lists(out)
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
    """Destination photo = a generic ambiance photo from Pexels (credited),
    else the share image of the destination's page at a Canadian wholesaler."""
    for d in dests:
        d["page_url"] = _clean_url(d.get("page_url"), known, WHOLESALERS)
    urls = list({d["page_url"] for d in dests if d.get("page_url")})
    previews = {}
    if urls:
        with ThreadPoolExecutor(max_workers=4) as ex:
            previews = dict(zip(urls, ex.map(page_preview, urls)))
    with ThreadPoolExecutor(max_workers=4) as ex:
        ambiance = list(ex.map(lambda d: pexels.photo_for(d["nom"]), dests))
    for d, amb in zip(dests, ambiance):
        img, dead = previews.get(d.get("page_url"), ("", False))
        if dead:
            d["page_url"] = ""
        if amb:
            d["photo"] = amb["src"]
            d["photo_from"] = "Photo d'ambiance · " + (amb["photographer"] + " / " if amb["photographer"] else "") + "Pexels"
            d["photo_link"] = amb["page"]
        else:
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
               + (f"<figcaption><a href=\"{escape(d['photo_link'])}\" target='_blank' rel='noopener nofollow'>"
                  f"{escape(d.get('photo_from') or '')}</a></figcaption></figure>" if d.get("photo_link") else
                  f"<figcaption>{escape(d.get('photo_from') or '')}</figcaption></figure>")
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
        self.imgs: list = []          # (src, alt) of the page's <img> tags
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("img", "source") and len(self.imgs) < 120:
            src = a.get("data-src") or a.get("src") or (a.get("srcset") or a.get("data-srcset") or "").split(" ")[0]
            if src and not src.startswith("data:"):
                self.imgs.append((src.strip(), (a.get("alt") or a.get("title") or "").strip()))
            return
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
_IMGS: dict = {}                # page url -> [(img url, alt)] (process-wide)
_RAW: dict = {}                 # page url -> image URLs found anywhere in the HTML (embedded JSON too)
_DIAG: dict = {}                # page url -> what the server saw (status, sizes) — for the debug page
_RAW_IMG = re.compile(r"""(?:https?:)?(?:\\?/){2}[^"'\s()<>]+?\.(?:jpe?g|webp|png)(?:\?[^"'\s()<>]*)?""", re.I)
_SITE_IMG: dict = {}            # image url -> {page urls showing it as THEIR photo}
_NOT_PHOTO = re.compile(r"(?i)(logo|icon|sprite|placeholder|default|favicon|badge|flag|"
                        r"avatar|banner-generic|\.svg(\?|$)|\.gif(\?|$)|1x1|pixel)")
_NAME_STOP = {"hotel", "hôtel", "resort", "resorts", "spa", "and", "the", "club", "all",
              "inclusive", "suites", "beach", "playa", "by", "de", "del", "la", "le", "les", "&"}


def _allowed_domain(url: str) -> bool:
    """Sites a model-given link may point to without having been cited:
    the Canadian wholesalers, TripAdvisor, plus any configured allow-list."""
    host = (urlparse(url).hostname or "").lower()
    domains = list(WHOLESALERS) + ["tripadvisor.ca", "tripadvisor.com", "tripadvisor.fr"] + \
        [d.strip().lower() for d in settings.PORTAL_ASSISTANT_SEARCH_DOMAINS.split(",") if d.strip()]
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
        with urllib.request.urlopen(req, timeout=4) as r:
            _DIAG[url] = {"status": getattr(r, "status", 200), "type": r.headers.get("Content-Type") or ""}
            if "html" in (r.headers.get("Content-Type") or ""):
                p = _OgParser()
                raw = r.read(2_500_000).decode("utf-8", "ignore")
                p.feed(raw)
                # photos often live in the page's embedded data (JS frameworks), not in <img>
                _RAW[url] = list(dict.fromkeys(
                    urljoin(url, m.replace("\\/", "/")) for m in _RAW_IMG.findall(raw)))[:400]
                _DIAG[url].update(bytes=len(raw), imgs=len(p.imgs), raw_imgs=len(_RAW[url]))
                cand = urljoin(url, p.image) if p.image else ""
                if cand.startswith("https://"):
                    img = cand
                pics = [img] if img else []
                for u in _ld_images(p.ld):
                    u = urljoin(url, u.strip())
                    if u.startswith("https://") and u not in pics:
                        pics.append(u)
                _GALLERY[url] = pics[:8]
                _IMGS[url] = [(urljoin(url, s), alt) for s, alt in p.imgs
                              if urljoin(url, s).startswith("https://")]
    except urllib.error.HTTPError as exc:
        _DIAG[url] = {"status": exc.code}
        dead = exc.code in (404, 410)
        if not dead:            # 403/5xx: site blocks bots or is down — link may still work
            log.info("preview unavailable (%s) for %s", exc.code, url)
    except Exception as exc:  # noqa: BLE001 — a missing photo is fine
        log.info("preview unavailable for %s: %s", url, exc)
    if len(_OG_CACHE) > 500:
        _OG_CACHE.clear()
        _GALLERY.clear()
        _IMGS.clear()
        _RAW.clear()
        _DIAG.clear()
    _OG_CACHE[url] = (img, dead)
    return img, dead


def _name_tokens(hotel: str, area: str = "") -> set:
    drop = {t for t in hc_norm(area).split() if len(t) > 2}
    return {t for t in hc_norm(hotel).split() if len(t) > 2 and t not in _NAME_STOP and t not in drop}


def hc_norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def page_gallery(url: str, hotel: str = "", area: str = "") -> list:
    """Photos of THIS hotel on a page, live URLs: images whose alt text names
    the hotel first, then the page's JSON-LD photos, then its share image.
    Site-wide share images (same photo on many pages) are left out."""
    if url not in _OG_CACHE:
        page_preview(url)
    base = _GALLERY.get(url)
    if base is None:
        img = _OG_CACHE.get(url, ("", False))[0]
        base = [img] if img else []
    named = []
    toks = _name_tokens(hotel, area)
    if toks:
        need = 1 if len(toks) == 1 else 2
        for src, alt in _IMGS.get(url, []):
            if src not in named and len(toks & set(hc_norm(alt + " " + src).split())) >= need:
                named.append(src)
        for src in _RAW.get(url, []):     # e.g. …/royalton-splash-punta-cana/pool-01.jpg
            if src not in named and src.startswith("https://") and \
                    len(toks & set(hc_norm(urlparse(src).path).split())) >= need:
                named.append(src)
    out = []
    for p in named + base:
        if p in out or _NOT_PHOTO.search(p):
            continue
        _SITE_IMG.setdefault(p, set()).add(url)
        if len(_SITE_IMG[p]) >= 2 and p not in named:
            continue                       # same share image on several hotel pages: generic
        out.append(p)
    return out[:8]


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
        url = _clean_url(d.get("source_url"), known, WHOLESALERS)
        priced = bool(url)               # a price counts only if seen at a Canadian wholesaler
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
            "clientele": d.get("clientele") if d.get("clientele") in ("adultes", "familles", "tous") else "",
            "formule": _txt(d.get("formule"), 50),
            "restos": [_txt(x, 90) for x in (d.get("restos") if isinstance(d.get("restos"), list) else []) if _txt(x, 90)][:4],
            "activites": [_txt(x, 90) for x in (d.get("activites") if isinstance(d.get("activites"), list) else []) if _txt(x, 90)][:4],
            "bemol": _txt(d.get("bemol"), 200),
            "depart": _txt(d.get("depart"), 40),
            "nuits": nuits,
            "prix_personne": d.get("prix_personne") if priced else None,
            "total": d.get("total") if priced else None,
            "voyageurs": _txt(d.get("voyageurs"), 40),
            "taxes": d.get("taxes") if priced and d.get("taxes") in ("incluses", "en sus") else None,
            "prix_note": _txt(d.get("prix_note"), 140) if priced else "",
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
    official = {}
    for i, c in enumerate(cards):                 # official photos from Transat's public image sitemap
        hit = wholesaler_photos.lookup(c["hotel"], c.get("destination", ""))
        if hit:
            official[i] = hit
            if not c.get("fiche_url"):
                c["fiche_url"] = hit["page"]
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
        hit = official.get(cards.index(c))
        if hit:
            for img in hit["images"][:8]:
                src = img + ("&" if "?" in img else "?") + "width=900"
                gallery.append({"src": src, "host": hit["host"], "page": hit["page"], "official": True})
                seen.add(src)
        for u in _card_urls(c):
            if not _on(u, WHOLESALERS):
                continue
            for p in page_gallery(u, c["hotel"], c.get("destination", "")):
                if p not in seen:
                    seen.add(p)
                    gallery.append({"src": p, "host": _host(u), "page": u})
        c["gallery"] = gallery[:10]
    # a photo shown for two different hotels is a site image, not a hotel photo
    owners: dict = {}
    for i, c in enumerate(cards):
        for g in c["gallery"]:
            owners.setdefault(g["src"], set()).add(i)
    for c in cards:
        c["gallery"] = [g for g in c["gallery"] if g.get("official") or (
            len(owners.get(g["src"], ())) < 2
            and len({_host(u) + "|" + u for u in _SITE_IMG.get(g["src"], ())}) < 2)]
        c["image"] = c["gallery"][0]["src"] if c["gallery"] else ""
        c["image_from"] = c["gallery"][0]["host"] if c["gallery"] else ""
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


_CLIENTELE = {"adultes": "Adultes seulement", "familles": "Idéal en famille", "tous": "Couples et familles"}


def _badges(c: dict) -> str:
    b = [x for x in (_CLIENTELE.get(c.get("clientele") or ""), c.get("formule")) if x]
    return "".join(f"<span class='hc-badge'>{escape(x)}</span>" for x in b)


def _rating(c: dict) -> str:
    """TripAdvisor rating seen by the agent; else the Google rating (Places)."""
    if c.get("note"):
        rv = c.get("avis")
        return (f"★ {str(c['note']).replace('.', ',')}"
                + (f" · {rv:,} avis".replace(",", "\u202f") if rv else "") + " TripAdvisor")
    p = c.get("place") or {}
    if p.get("rating"):
        n = p.get("count")
        return (f"★ {str(p['rating']).replace('.', ',')}"
                + (f" · {n:,} avis".replace(",", "\u202f") if n else "") + " Google")
    return ""


def hotel_detail_html(c: dict) -> str:
    """Side panel (desktop) / sheet (mobile) for one hotel, chatgpt.com style."""
    # only the first photo loads with the panel; the others load when the member
    # browses the gallery (each Google photo shown is a billed request)
    pics = "".join(
        f"<figure class='hd-shot'><img {'src' if i == 0 else 'data-src'}=\"{escape(g['src'])}\" alt='' "
        "referrerpolicy='no-referrer' onerror=\"this.parentNode.remove()\">"
        f"<figcaption>Photo : {escape(g['host'])}</figcaption></figure>"
        for i, g in enumerate(c.get("gallery") or []))
    gal = (f"<div class='hd-gal'>{pics}</div>" if pics
           else "<div class='hd-gal hd-nogal'>Photos sur la fiche du grossiste</div>")
    meta = " · ".join(escape(x) for x in (_rating(c), c.get("type")) if x)
    p = c.get("place") or {}
    maps = p.get("maps") or ("https://www.google.com/maps/search/?api=1&query="
                             + quote_plus(f"{c['hotel']} {c.get('destination') or ''}"))
    fiche = c.get("fiche_url") or (c.get("fiches") or [""])[0]
    btns = (f"<a class='hd-b hd-main' href=\"{escape(c.get('search_url') or SEARCH_URL)}\">"
            + ("Voir les prix avec mon rabais" if (_money(c.get("prix_personne")) or _money(c.get("total")))
               else "Vérifier les forfaits") + "</a>"
            f"<a class='hd-b' href=\"{escape(maps)}\" target='_blank' rel='noopener'>Itinéraire</a>"
            + (f"<a class='hd-b' href=\"{escape(p['website'])}\" target='_blank' rel='noopener nofollow'>"
               "Site web ↗</a>" if p.get("website") else "")
            + (f"<a class='hd-b' href=\"{escape(fiche)}\" target='_blank' rel='noopener nofollow'>"
               f"Fiche {escape(_host(fiche))} ↗</a>" if fiche else ""))
    rows = ""
    addr = p.get("address") or c.get("adresse") or c.get("destination")
    if addr:
        rows += f"<div class='hd-row'><span>📍</span>{escape(addr)}</div>"
    if p.get("phone"):
        tel = re.sub(r"[^\d+]", "", p["phone"])
        rows += f"<div class='hd-row'><span>📞</span><a href='tel:{escape(tel)}'>{escape(p['phone'])}</a></div>"
    if c.get("depart") or c.get("nuits"):
        rows += ("<div class='hd-row'><span>🗓️</span>" + escape(", ".join(x for x in (
            c.get("depart") or "", f"{c['nuits']} nuits" if c.get("nuits") else "") if x)) + "</div>")
    desc = f"<p>{escape(c['resume'])}</p>" if c.get("resume") else ""
    if _badges(c):
        desc = f"<div class='hc-badges'>{_badges(c)}</div>" + desc
    if c.get("points_forts"):
        desc += "<h4>Forces</h4><ul>" + "".join(f"<li>{escape(p)}</li>" for p in c["points_forts"]) + "</ul>"
    if c.get("restos"):
        desc += "<h4>Restos</h4><ul>" + "".join(f"<li>{escape(p)}</li>" for p in c["restos"]) + "</ul>"
    if c.get("activites"):
        desc += "<h4>Activités</h4><ul>" + "".join(f"<li>{escape(p)}</li>" for p in c["activites"]) + "</ul>"
    if c.get("bemol"):
        desc += f"<p class='hd-bemol'><b>Bémol :</b> {escape(c['bemol'])}</p>"
    price = _price_line(c)
    conf = (f"<p class='hd-conf'>À confirmer : {escape(c['a_confirmer'])}</p>"
            if c.get("a_confirmer") else "")
    srcs = "".join(f"<a href=\"{escape(x['url'])}\" target='_blank' rel='noopener nofollow'>"
                   f"{escape(x['host'])}</a>" for x in c.get("sources") or [])
    if p.get("maps"):
        srcs += f"<a href=\"{escape(p['maps'])}\" target='_blank' rel='noopener'>Google Maps</a>"
    emb = places.embed_url(c)
    mapbox = (f"<iframe class='hd-map' loading='lazy' referrerpolicy='no-referrer-when-downgrade' "
              f"src=\"{escape(emb)}\" title='Carte'></iframe>") if emb else ""
    return (f"<div class='hd'>{gal}<div class='hd-body'>"
            f"<h3 class='hd-name'>{escape(c['hotel'])}</h3>"
            + (f"<div class='hd-meta'>{meta}</div>" if meta else "")
            + f"<div class='hd-btns'>{btns}</div>{rows}"
            + f"<div class='hd-desc'>{desc}{price}{conf}</div>{mapbox}"
            + (f"<div class='hd-srcs'><span>Sources</span>{srcs}</div>" if srcs else "")
            + ("<p class='hd-note'>Photos, note Google, adresse et téléphone : Google.</p>" if p else "")
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
        priced = bool(_money(c.get("prix_personne")) or _money(c.get("total")))
        go = ("Voir les prix →" if priced else "Vérifier les forfaits →")
        pts = ("<ul class='hc-pts'>" + "".join(f"<li>{escape(p)}</li>" for p in c["points_forts"]) + "</ul>"
               ) if c.get("points_forts") else ""
        loc = " · ".join(escape(x) for x in (c.get("destination"), _rating(c)) if x)
        pill = cite_html(list(range(len(c.get("sources") or []))), c.get("sources") or [])
        conf = (f"<div class='hc-conf'>À confirmer : {escape(c['a_confirmer'])}</div>"
                if c.get("a_confirmer") else "")
        out.append(
            f"<div class='hcard'>{pic}<div class='hc-body'>"
            f"<button type='button' class='hc-name hc-open'>{i}. {escape(c['hotel'])}</button>"
            + (f"<div class='hc-sub'>{sub}</div>" if sub else "")
            + (f"<div class='hc-loc'>{loc}</div>" if loc else "")
            + (f"<div class='hc-badges'>{_badges(c)}</div>" if _badges(c) else "")
            + (f"<div class='hc-desc'>{escape(c['resume'])}</div>" if c.get("resume") else "")
            + pts + _price_line(c) + conf
            + f"<div class='hc-foot'>{pill}<button type='button' class='hc-more hc-open'>Détails</button>"
              f"<label class='hc-sel'><input type='checkbox' class='hc-chk' data-i='{i - 1}'> Comparer</label>"
              f"<a class='hc-go' href=\"{escape(c.get('search_url') or SEARCH_URL)}\">{go}</a></div>"
            + f"</div><template class='hc-detail'>{hotel_detail_html(c)}</template></div>")
    cmp_btn = cmp = ""
    if len(cards) >= 2:
        def cell(v):
            return f"<td>{v or '—'}</td>"
        rows = [
            ("Pour qui", [escape(c.get("titre") or "") for c in cards]),
            ("Clientèle", [_CLIENTELE.get(c.get("clientele") or "", "") for c in cards]),
            ("Formule", [escape(c.get("formule") or "") for c in cards]),
            ("Type", [escape(c.get("type") or "") for c in cards]),
            ("Restos", [escape(" · ".join(c.get("restos") or [])) for c in cards]),
            ("Activités", [escape(" · ".join(c.get("activites") or [])) for c in cards]),
            ("Note", [escape(_rating(c)) for c in cards]),
            ("Prix / pers.", [_money(c.get("prix_personne")) for c in cards]),
            ("Total", [_money(c.get("total")) for c in cards]),
            ("Bémol", [escape(c.get("bemol") or "") for c in cards]),
        ]
        thead = "".join(f"<th>{escape(c['hotel'])}</th>" for c in cards)
        tbody = "".join(f"<tr><th>{k}</th>{''.join(cell(v) for v in vals)}</tr>"
                        for k, vals in rows if any(vals))
        cmp_btn = "<button type='button' class='pc-cmpbtn'>Comparer tout</button>"
        cmp = (f"<div class='pc-cmp' hidden><table><thead><tr><th></th>{thead}</tr></thead>"
               f"<tbody>{tbody}</tbody></table></div>")
    smap = places.static_map(cards)
    mapblk = (f"<div class='hmap'><div class='hm-h'>Où sont les hôtels ?</div>"
              f"<img src=\"{escape(smap)}\" alt='Carte des hôtels recommandés' loading='lazy'>"
              "<div class='hm-k'>" + "".join(f"<span>{i}. {escape(c['hotel'])}</span>"
                                             for i, c in enumerate(cards, 1) if c.get("place"))
              + "</div></div>") if smap else ""
    return (f"<div class='pcards hlist'><div class='pc-head'><span>Hôtels à regarder</span>{cmp_btn}</div>"
            f"{cmp}{''.join(out)}{mapblk}</div>")


def parse_prix(rows, sources: list) -> list:
    """Price comparison rows: real dates/departure city; a row is « vu » only
    with a Canadian wholesaler source page, otherwise it is an estimate."""
    known = {s["url"] for s in sources}
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not _txt(r.get("hotel"), 90) or not _money(r.get("total")):
            continue
        url = _clean_url(r.get("url"), known, WHOLESALERS)
        out.append({"hotel": _txt(r.get("hotel"), 90), "dates": _txt(r.get("dates"), 60),
                    "depart": _txt(r.get("depart"), 40), "voyageurs": _txt(r.get("voyageurs"), 40),
                    "total": r.get("total"),
                    "taxes": r.get("taxes") if r.get("taxes") in ("incluses", "en sus") else None,
                    "statut": "vu" if r.get("statut") == "vu" and url else "estimation",
                    "url": url})
    return out[:6]


def prix_from_cards(cards) -> list:
    """No price table from the model: build it from the hotel cards whose
    price was seen at a Canadian wholesaler (parse_pistes keeps only those)."""
    out = []
    for c in cards or []:
        if not _money(c.get("total")):
            continue
        out.append({"hotel": c["hotel"], "dates": c.get("prix_note") or "",
                    "depart": c.get("depart") or "", "voyageurs": c.get("voyageurs") or "",
                    "total": c["total"], "taxes": c.get("taxes"),
                    "statut": "vu" if c.get("source_url") else "estimation", "url": c.get("source_url") or ""})
    return out[:6]


def prix_html(rows) -> str:
    if not rows:
        return ""
    body = ""
    for r in rows:
        src = (f"<a class='pt-src' href=\"{escape(r['url'])}\" target='_blank' rel='noopener nofollow'>"
               f"{escape(_host(r['url']))}</a>") if r.get("url") else ""
        tag = "" if r["statut"] == "vu" else "<span class='pt-est'>estimation</span>"
        when = " · ".join(escape(x) for x in (r.get("dates"), r.get("depart") and f"départ {r['depart']}") if x)
        tax = {"incluses": "taxes comprises", "en sus": "taxes en sus"}.get(r.get("taxes"), "")
        body += (f"<tr><th>{escape(r['hotel'])}</th><td>{when or '—'}</td>"
                 f"<td class='pt-tot'>{_money(r['total'])}{tag}"
                 + (f"<small>{escape(r['voyageurs'])}{', ' + tax if tax else ''}</small>" if r.get("voyageurs") or tax else "")
                 + f"</td><td>{src}</td></tr>")
    return ("<div class='ptab'><table><thead><tr><th>Hôtel</th><th>Dates du tarif repéré</th>"
            "<th>Total</th><th>Source</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
            "<p class='pt-note'>Prix publics repérés pour les dates indiquées : des repères, pas des "
            "soumissions. Cherche le même forfait dans notre moteur, avec ton Rabais Récompense.</p></div>")


def parse_budget(rows) -> list:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and _txt(r.get("poste"), 50) and _money(r.get("montant")):
            out.append({"poste": _txt(r.get("poste"), 50), "montant": float(r["montant"])})
    return out[:6]


def budget_html(rows) -> str:
    if len(rows or []) < 2:
        return ""
    body = "".join(f"<tr><th>{escape(r['poste'])}</th><td>{_money(r['montant'])}</td></tr>" for r in rows)
    total = _money(sum(r["montant"] for r in rows))
    return ("<div class='btab'><div class='bt-h'>Budget cible pour ton voyage</div><table>"
            f"<tbody>{body}<tr class='bt-tot'><th>Total cible</th><td>{total}</td></tr></tbody></table>"
            "<p class='pt-note'>Budget de planification, pas une offre ni un prix garanti.</p></div>")


def parse_recherche(r) -> dict:
    """Trip criteria for the booking engine, validated."""
    if not isinstance(r, dict):
        return {}
    out = {}
    dep = str(r.get("depart") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", dep):
        out["dep"] = dep
    if _txt(r.get("destination"), 60):
        out["dest"] = _txt(r.get("destination"), 60)
    d = str(r.get("date_depart") or "").strip()
    if re.fullmatch(r"20\d\d-[01]\d-[0-3]\d", d):
        out["date"] = d
    for key, lo, hi in (("nuits", 1, 30), ("adultes", 1, 9), ("enfants", 0, 8)):
        try:
            v = int(r.get(key))
            if lo <= v <= hi:
                out[key] = v
        except (TypeError, ValueError):
            pass
    ages = [int(a) for a in (r.get("ages_enfants") or []) if isinstance(a, (int, float)) and 0 <= a <= 17]
    if ages:
        out["ages"] = ",".join(str(a) for a in ages[:8])
    return out


def search_link(rech: dict, hotel: str = "") -> str:
    """Our search tab with the trip criteria (the engine page maps them)."""
    q = dict(rech or {})
    if hotel:
        q["hotel"] = hotel
    return SEARCH_URL + ("?" + urlencode(q) if q else "")


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


def _fill_slots(body: str, dests: str, cards: str, prix: str = "", budget: str = "") -> tuple:
    """Put blocks where the agent placed [[DESTINATIONS]] / [[HOTELS]] /
    [[PRIX]] / [[BUDGET]]; returns (body, leftover blocks to append)."""
    tail = ""
    for slot, html in (("destinations", dests), ("hotels", cards), ("prix", prix), ("budget", budget)):
        marker = f"<div class='slot' data-slot='{slot}'></div>"
        if marker in body:
            body = body.replace(marker, f"<div class='slot' data-slot='{slot}'>{html}</div>", 1)
        else:
            tail += html
    return body, tail


TABS = (("info", "Info générale"), ("hotels", "Hôtels retenus"), ("prix", "Comparaison des prix"),
        ("sejour", "Ton séjour"), ("vols", "Vols suggérés"))
_SLOT_TAB = {"destinations": "info", "hotels": "hotels", "prix": "prix", "budget": "prix"}
_TABMARK_RE = re.compile(r"<div class='tabmark' data-tab='(\w+)'></div>")


def compose_answer(body: str, blocks: dict) -> str:
    """Rendered answer + its blocks (cards, tables). With [[TAB:…]] markers
    the answer becomes tabs; each block goes to its slot, else its tab."""
    if not _TABMARK_RE.search(body):
        b, tail = _fill_slots(body, blocks.get("destinations", ""), blocks.get("hotels", ""),
                              blocks.get("prix", ""), blocks.get("budget", ""))
        return f"<div>{b}</div>{tail}"
    parts = _TABMARK_RE.split(body)
    secs: dict = {}
    if parts[0].strip():
        secs["info"] = parts[0]
    for i in range(1, len(parts), 2):
        secs[parts[i]] = secs.get(parts[i], "") + parts[i + 1]
    for slot, html in blocks.items():
        if not html:
            continue
        marker = f"<div class='slot' data-slot='{slot}'></div>"
        for k in secs:
            if marker in secs[k]:
                secs[k] = secs[k].replace(marker, f"<div class='slot' data-slot='{slot}'>{html}</div>", 1)
                break
        else:
            k = _SLOT_TAB[slot]
            secs[k] = secs.get(k, "") + html
    keep = [(k, label) for k, label in TABS
            if k in secs and (re.sub(r"<[^>]+>|\s", "", secs[k]) or "<img" in secs[k])]
    if not keep:
        return f"<div>{body}</div>"
    nav = "".join(f"<button type='button' class='rt-btn{' on' if i == 0 else ''}' data-tab='{k}' "
                  f"role='tab'>{label}</button>" for i, (k, label) in enumerate(keep))
    panes = "".join(f"<section class='rt-pane' data-tab='{k}'{'' if i == 0 else ' hidden'}>"
                    f"{secs[k].strip()}</section>" for i, (k, _l) in enumerate(keep))
    return f"<div class='rtabs'><div class='rt-nav' role='tablist'>{nav}</div>{panes}</div>"


def steps_html(steps, live: bool = False) -> str:
    if not steps and not live:
        return ""
    items = "".join(f"<li>{escape(x)}</li>" for x in steps or [])
    label = "Recherche en cours…" if live else f"Recherche · {len(steps)} étape{'s' if len(steps) > 1 else ''}"
    return (f"<details class='asteps'{' open' if live else ''}><summary>{label}</summary>"
            f"<ol>{items}</ol></details>")


def needs_structure(m: dict) -> bool:
    """A stored answer whose hotel cards / price table never arrived."""
    if m.get("role") != "assistant" or m.get("brief") or m.get("fixed"):
        return False
    t = m.get("text") or ""
    want_hotels = "[[HOTELS]]" in t or "[[TAB:hotels]]" in t
    want_prix = "[[PRIX]]" in t or "[[TAB:prix]]" in t
    return (want_hotels and not m.get("cards")) or \
        (want_prix and not m.get("prix") and not prix_from_cards(m.get("cards")))


def _bubble(m: dict, last: bool = False) -> str:
    if m.get("role") == "user":
        return f"<div class='msg me'>{escape(m.get('text', ''))}</div>"
    if m.get("brief"):
        b = m["brief"]
        extra = (trip_brief.form_html(b.get("p") or {}, b.get("miss") or []) if last
                 else f"<div class='bf-known'>{trip_brief.known_chips(b.get('p') or {})}</div>")
        return f"<div class='msg ai'><div>{render_md(m.get('text', ''))}</div>{extra}</div>"
    content = compose_answer(
        render_md(tidy_text(m.get("marked") or m.get("text", "")), m.get("sources")),
        {"destinations": destinations_html(m.get("destinations")), "hotels": cards_html(m.get("cards")),
         "prix": prix_html(m.get("prix") or prix_from_cards(m.get("cards"))),
         "budget": budget_html(m.get("budget"))})
    fix = ""
    if needs_structure(m) and m.get("id"):
        fix = f" data-fix=\"{escape(m['id'])}\""
        content = "<div class='afix'>📸 Je prépare les fiches et le tableau…</div>" + content
    return (f"<div class='msg ai'{fix}>{content}"
            f"{_sources_html(m.get('sources'))}{rate_html(m.get('id', ''), m.get('rating', 0))}"
            f"{questions_html(m.get('questions')) if last else ''}</div>")


# --------------------------------------------------------------------------- #
# Conversations (many per member, « recent chats » sidebar)
# --------------------------------------------------------------------------- #
def _migrate_thread(db, client) -> None:
    """The single legacy thread (Client.assistant_chat) becomes a first chat."""
    old = list(client.assistant_chat or [])
    if not old:
        return
    if not db.query(AssistantChat).filter_by(client_id=client.id).first():
        first_q = next((m.get("text", "") for m in old if m.get("role") == "user"), "")
        db.add(AssistantChat(client_id=client.id, title=_chat_title(first_q), messages=old,
                             updated_at=datetime.utcnow()))
    client.assistant_chat = []
    db.commit()


def _chat_title(q: str) -> str:
    q = re.sub(r"\s+", " ", q or "").strip()
    return (q[:57] + "…") if len(q) > 60 else (q or "Nouvelle conversation")


def recent_chats(db, cid: int, limit: int = 30) -> list:
    return (db.query(AssistantChat).filter_by(client_id=cid)
            .order_by(AssistantChat.updated_at.desc()).limit(limit).all())


def get_chat(db, cid: int, chat_id):
    try:
        ch = db.get(AssistantChat, int(chat_id))
    except (TypeError, ValueError):
        return None
    return ch if ch and ch.client_id == cid else None


def _ago(dt) -> str:
    if not dt:
        return ""
    days = (datetime.utcnow().date() - dt.date()).days
    return "Aujourd'hui" if days <= 0 else "Hier" if days == 1 else \
        f"Il y a {days} jours" if days < 7 else f"{dt.day} {_MONTHS[dt.month - 1][:4]}."


def sidebar_html(chats: list, active) -> str:
    items, last = [], None
    for ch in chats:
        grp = _ago(ch.updated_at)
        if grp != last and grp in ("Aujourd'hui", "Hier"):
            items.append(f"<div class='csb-g'>{grp}</div>")
        elif grp != last and last in (None, "Aujourd'hui", "Hier"):
            items.append("<div class='csb-g'>Plus ancien</div>")
        last = grp if grp in ("Aujourd'hui", "Hier") else "old"
        on = " on" if active and ch.id == active else ""
        items.append(f"<div class='csb-i{on}' data-id='{ch.id}'><a href='/portail?c={ch.id}'>"
                     f"{escape(ch.title or 'Conversation')}</a>"
                     f"<button type='button' class='csb-x' aria-label='Supprimer'>✕</button></div>")
    return ("<aside class='csb' id='aSide'><a class='csb-new' href='/portail?c=new'>＋ Nouvelle conversation</a>"
            "<div class='csb-h'>Conversations récentes</div><div class='csb-l' id='aChats'>"
            + ("".join(items) or "<div class='csb-empty'>Tes conversations apparaîtront ici.</div>")
            + "</div></aside>")


def assistant_panel(client, chat_param: str = "") -> str:
    """Full-screen chat with the recent-chats sidebar."""
    from db import SessionLocal  # noqa: F811 — explicit for readers
    with SessionLocal() as db:
        c = db.get(Client, client.id) if getattr(client, "id", None) else None
        if c is not None:
            _migrate_thread(db, c)
            chats = recent_chats(db, c.id)
            if chat_param == "new":
                current = None
            elif chat_param:
                current = get_chat(db, c.id, chat_param)
            else:
                current = chats[0] if chats else None
            thread = list(current.messages or []) if current else []
            cur_id = current.id if current else None
            side = sidebar_html(chats, cur_id)
        else:                                   # unsaved client object (tests)
            thread, cur_id, side = list(getattr(client, "assistant_chat", None) or []), None, ""
    return ("<div class='cwrap'>" + side + _panel_main(client, thread, cur_id) + "</div>")


def _panel_main(client, thread: list, cur_id) -> str:
    """The portal home: a full-screen chat (chatgpt.com layout) — centered
    empty state with suggestions, then the thread with a sticky composer."""
    hi = escape(_first_name(client))
    msgs = "".join(_bubble(m, last=(i == len(thread) - 1)) for i, m in enumerate(thread))
    if not thread:
        chips = ("<div class='chips starters' id='aChips'>" + "".join(
            f"<button type='button' class='chip starter' data-q=\"{escape(q)}\">"
            f"<span class='st-ic'>{ic}</span><b>{escape(t)}</b><span class='st-q'>{escape(q)}</span></button>"
            for ic, t, q in _SUGGESTIONS) + "</div>")
    else:
        chip_list = thread[-1].get("suivis") or [] if thread[-1].get("role") == "assistant" else []
        chips = ("<div class='chips' id='aChips'>" + "".join(
            f"<button type='button' class='chip'>{escape(s)}</button>" for s in chip_list)
            + "</div>") if chip_list else ""
    reset = ("<button type='button' class='areset csb-tog' id='aSideTog' aria-label='Conversations'>"
             "☰ Conversations</button>")
    human = "<button type='button' class='areset' id='aHuman'>💬 Conseiller</button>"
    if is_tester(client):
        human = "<a class='areset' href='/portail/assistant/comparer'>🧪 Test A/B</a>" + human
    messenger = json.dumps(settings.MESSENGER_URL)
    return f"""
<div class='assist chat{'' if thread else ' empty'}' id='aBox' data-chat='{cur_id or ""}'>
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
 var SLOTS={{dests:'destinations',cards:'hotels',prix:'prix',budget:'budget'}};
 var MAXS={settings.PORTAL_ASSISTANT_SEARCHES_PER_ANSWER}||8;
 function say(t){{toast.textContent=t;toast.hidden=false;clearTimeout(say.t);
   say.t=setTimeout(function(){{toast.hidden=true;}},5000);}}
 function esc(s){{return s.replace(/[&<>"']/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#x27;'}}[c];}});}}
 function md(s){{return esc(s).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')
   .replace(/^\\s*(?:-{{3,}}|\\*{{3,}}|_{{3,}})\\s*$/gm,'').replace(/^\\s*[-*]\\s+/gm,'• ')
   .replace(/^#{{1,6}}\\s*/gm,'').replace(/\\n{{3,}}/g,'\\n\\n')
   .replace(/\\[\\[TAB:\\w+\\]\\]/g,'')
   .replace(/^\\s*\\[\\[(DESTINATIONS|HOTELS|PRIX|BUDGET)\\]\\]\\s*$/gm,function(_,k){{
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
   log.querySelectorAll('.brief').forEach(function(x){{x.querySelectorAll('.bf-f,.bf-go,.bf-skip,.bf-h').forEach(function(y){{y.remove();}});}});
   var me=add('me',q); inp.value=''; grow();
   me.style.scrollMarginTop='96px';me.scrollIntoView({{block:'start',behavior:'smooth'}});
   send.disabled=true; inp.disabled=true;
   var out=add('ai typing',''),body=document.createElement('div'),st=document.createElement('div');
   st.className='astatus';out.appendChild(st);out.appendChild(body);
   var raw='',buf='',done=false,nsrch=0,pr=document.createElement('div'),pct=0,cap=12;
   pr.className='aprog';pr.innerHTML='<div class="ap-lbl">Je lis ta demande…</div><div class="ap-bar"><span></span></div>';
   out.insertBefore(pr,st);
   function prog(p,label){{cap=Math.max(cap,p);if(p>pct)pct=p;pr.querySelector('span').style.width=pct+'%';
     if(label)pr.querySelector('.ap-lbl').textContent=label;}}
   prog(5);
   var creep=setInterval(function(){{if(pct<cap-1){{pct+=Math.max(.3,(cap-pct)/14);
     pr.querySelector('span').style.width=pct+'%';}}}},700);
   function handle(ev){{
     if(ev.t==='status'){{out.classList.remove('typing');
       if(/fiches/.test(ev.v))prog(88,'Je prépare les fiches et les photos…');
       else if(/cherche/.test(ev.v))prog(14,'Je cherche les meilleures options…');}}
     else if(ev.t==='step'){{out.classList.remove('typing');
       if(ev.v.indexOf('🔎')===0){{nsrch++;
         prog(14+Math.min(56,nsrch*56/MAXS),nsrch<3?'Je cherche les meilleures options…':
           (nsrch<6?'Je compare les hôtels, les avis et les prix…':'Je vérifie les derniers détails…'));}}}}
     else if(ev.t==='text'){{out.classList.remove('typing');raw+=ev.v;
       if(pct<72)prog(72,'Je rédige ta réponse…');}}
     else if(ev.t==='final'&&ev.v){{done=true;var w=document.createElement('div');w.innerHTML=ev.v.html;
       var nm=w.firstElementChild;if(ev.v.refine){{var r2=document.createElement('div');r2.innerHTML=ev.v.refine;
         if(r2.firstElementChild)nm.appendChild(r2.firstElementChild);}}
       out.replaceWith(nm);out=nm;initBrief(nm.parentNode);
       if(ev.v.chat){{var known=box.getAttribute('data-chat');box.setAttribute('data-chat',ev.v.chat);
         if(String(known)!==String(ev.v.chat)){{try{{history.replaceState(null,'','/portail?c='+ev.v.chat);}}catch(e){{}}}}
         var L=document.getElementById('aChats');
         if(L){{var ex=L.querySelector(".csb-i[data-id='"+ev.v.chat+"']");
           if(!ex){{var e0=L.querySelector('.csb-empty');if(e0)e0.remove();
             ex=document.createElement('div');ex.className='csb-i';ex.setAttribute('data-id',ev.v.chat);
             ex.innerHTML='<a></a><button type="button" class="csb-x" aria-label="Supprimer">✕</button>';
             ex.querySelector('a').href='/portail?c='+ev.v.chat;ex.querySelector('a').textContent=ev.v.title||q;}}
           L.querySelectorAll('.csb-i.on').forEach(function(x){{x.classList.remove('on');}});
           ex.classList.add('on');var g=L.querySelector('.csb-g');
           if(g&&g.textContent==="Aujourd'hui")g.after(ex);else{{var gg=document.createElement('div');gg.className='csb-g';
             gg.textContent="Aujourd'hui";L.prepend(ex);L.prepend(gg);}}}}}}}}
     else if(ev.t==='marked'){{}}
     else if(SLOTS[ev.t]&&ev.v&&false){{
       st.textContent='';var slot=body.querySelector(".slot[data-slot='"+SLOTS[ev.t]+"']");
       var cd=document.createElement('div');cd.innerHTML=ev.v;
       while(cd.firstChild)(slot||out).appendChild(cd.firstChild);}}
     else if(false){{
       st.textContent='';var cd2=document.createElement('div');cd2.innerHTML=ev.v;
       while(cd2.firstChild)out.appendChild(cd2.firstChild);}}
     else if(false){{
       var s=document.createElement('div');s.className='srcs';s.innerHTML='<span>Sources</span>';
       ev.v.forEach(function(x){{if(!/^https:\\/\\//.test(x.url))return;var a=document.createElement('a');
         a.href=x.url;a.target='_blank';a.rel='noopener nofollow';a.textContent=x.title||x.url;s.appendChild(a);}});
       out.appendChild(s);}}
   }}
   fetch('/portail/assistant',{{method:'POST',headers:{{'Content-Type':'application/json'}},
     body:JSON.stringify({{message:q,fmt:'ndjson',chat:box.getAttribute('data-chat')||null}})}}).then(function(r){{
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
   }}).finally(function(){{st.textContent='';send.disabled=false;inp.disabled=false;inp.focus();
     if(!done&&raw&&!body.textContent)body.innerHTML=md(raw);
     clearInterval(creep);prog(100,'Terminé');pr.classList.add('done');
     setTimeout(function(){{pr.remove();}},700);}});
 }}
 f.addEventListener('submit',function(e){{e.preventDefault();ask(inp.value);}});
 if(chips)chips.addEventListener('click',function(e){{
   var b=e.target.closest('.chip');if(b)ask(b.getAttribute('data-q')||b.textContent);}});
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
       b.onclick=function(){{more();g.scrollBy({{left:(i?1:-1)*g.clientWidth,behavior:'smooth'}});}};
       g.parentNode.insertBefore(b,g.nextSibling);}});
     var more=function(){{g.querySelectorAll('img[data-src]').forEach(function(im){{im.src=im.getAttribute('data-src');im.removeAttribute('data-src');}});}};
     g.addEventListener('scroll',more,{{once:true,passive:true}});}}
   document.getElementById('hpX').focus();}}
 document.getElementById('hpX').addEventListener('click',closeHotel);
 hov.addEventListener('click',closeHotel);
 document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&!hp.hidden)closeHotel();}});
 function initBrief(root){{(root||log).querySelectorAll('.brief').forEach(function(B){{
   var ag=B.querySelector('.bf-ages');if(!ag)return;var pre=(ag.getAttribute('data-ages')||'').split(',').filter(Boolean);
   ag.setAttribute('data-ages','');var n=+(B.querySelector("[data-n='enfants']")||{{textContent:0}}).textContent;
   var cur=[].slice.call(ag.querySelectorAll('select')).map(function(s){{return s.value;}});ag.innerHTML='';
   for(var i=0;i<n;i++){{var s=document.createElement('select');s.className='bf-age';
     s.innerHTML='<option value="">Âge enfant '+(i+1)+'</option>'+Array.from({{length:18}},function(_,a){{return '<option value="'+a+'">'+(a<2?a+' an':a+' ans')+'</option>';}}).join('');
     s.value=cur[i]||pre[i]||'';ag.appendChild(s);}}}});}}
 initBrief();
 function briefMsg(B){{var miss=(B.getAttribute('data-miss')||'').split(','),parts=[],bad=[];
   function val(f){{var i=B.querySelector(".bf-in[data-f='"+f+"']");if(i&&i.value.trim())return i.value.trim();
     var o=B.querySelector(".bf-o.on[data-f='"+f+"']");return o?o.getAttribute('data-v'):'';}}
   if(miss.indexOf('destination')>=0){{var d=val('destination');if(d)parts.push('Destination : '+d);else bad.push('la destination');}}
   if(miss.indexOf('depart')>=0){{var p=val('depart');if(p)parts.push('Départ : '+p);else bad.push('la ville de départ');}}
   if(miss.indexOf('date')>=0){{var dt=val('date');if(/^\\d{{4}}-\\d\\d-\\d\\d$/.test(dt)){{var M=['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre'],x=dt.split('-');
       dt=(+x[2])+' '+M[+x[1]-1]+' '+x[0];}}if(dt)parts.push('Date de départ : '+dt);else bad.push('la date');}}
   if(miss.indexOf('nuits')>=0){{var nn=val('nuits');if(nn)parts.push('Durée : '+nn);else bad.push('la durée');}}
   if(miss.indexOf('voyageurs')>=0||miss.indexOf('ages')>=0){{var a=+B.querySelector("[data-n='adultes']").textContent,
       k=+B.querySelector("[data-n='enfants']").textContent,ages=[].slice.call(B.querySelectorAll('.bf-age')).map(function(s){{return s.value;}});
     if(ages.some(function(v){{return v==='';}}))bad.push("l'âge des enfants");
     parts.push('Voyageurs : '+a+' adulte'+(a>1?'s':'')+(k?' et '+k+' enfant'+(k>1?'s':'')+' ('+ages.join(', ')+' ans)':''));}}
   if(miss.indexOf('budget')>=0){{var bu=val('budget');if(bu)parts.push('Budget : '+bu);else bad.push('le budget');}}
   if(bad.length){{say('Il manque encore : '+bad.join(', ')+'.');return '';}}
   return parts.join('\\n');}}
 log.addEventListener('click',function(e){{
   var bo=e.target.closest('.bf-o');
   if(bo){{var BB=bo.closest('.brief');BB.querySelectorAll(".bf-o[data-f='"+bo.getAttribute('data-f')+"']").forEach(function(x){{x.classList.toggle('on',x===bo);}});
     var ii=BB.querySelector(".bf-in[data-f='"+bo.getAttribute('data-f')+"']");if(ii&&ii.type!=='date')ii.value='';return;}}
   var pm=e.target.closest('.bf-m,.bf-p');
   if(pm){{var B2=pm.closest('.brief'),key=pm.getAttribute('data-k'),el=B2.querySelector("[data-n='"+key+"']"),
       v=+el.textContent+(pm.classList.contains('bf-p')?1:-1);v=Math.max(key==='adultes'?1:0,Math.min(key==='adultes'?8:6,v));
     el.textContent=v;if(key==='enfants')initBrief(B2.parentNode);return;}}
   var bg=e.target.closest('.bf-go');if(bg){{var m=briefMsg(bg.closest('.brief'));if(m)ask(m);return;}}
   var bs=e.target.closest('.bf-skip');if(bs){{ask('Suggère-moi des hôtels quand même avec ce que je t’ai donné.');return;}}
   var tb=e.target.closest('.rt-btn');
   if(tb){{var R=tb.closest('.rtabs'),k=tb.getAttribute('data-tab');
     R.querySelectorAll('.rt-btn').forEach(function(x){{x.classList.toggle('on',x===tb);}});
     R.querySelectorAll('.rt-pane').forEach(function(p){{p.hidden=p.getAttribute('data-tab')!==k;}});
     var top=R.getBoundingClientRect().top;if(top<80)window.scrollBy(0,top-90);return;}}
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
   if(c){{var pcs=c.closest('.pcards'),t=pcs.querySelector('.pc-cmp table');if(!t)return;
     var sel=[].slice.call(pcs.querySelectorAll('.hc-chk:checked')).map(function(x){{return +x.getAttribute('data-i');}});
     var tb=t.cloneNode(true);
     if(sel.length>=2)tb.querySelectorAll('tr').forEach(function(tr){{
       [].slice.call(tr.children).forEach(function(cell,j){{if(j>0&&sel.indexOf(j-1)<0)cell.remove();}});}});
     tb.querySelectorAll('tbody tr').forEach(function(tr){{
       if([].slice.call(tr.querySelectorAll('td')).every(function(td){{return td.textContent.trim()==='—';}}))tr.remove();}});
     hpb.innerHTML='<div class="hd-body"><h3 class="hd-name">Comparaison</h3><div class="pc-cmp hp-cmp"></div>'+
       '<a class="hd-b hd-main" style="align-self:flex-start" href="{SEARCH_URL}">Vérifier les forfaits</a></div>';
     hpb.querySelector('.hp-cmp').appendChild(tb);hp.hidden=false;hov.hidden=false;
     document.body.classList.add('has-panel');hp.scrollTop=0;return;}}
   var ck=e.target.closest('.hc-chk');
   if(ck){{var pc=ck.closest('.pcards'),n=pc.querySelectorAll('.hc-chk:checked').length;
     if(n>4){{ck.checked=false;say('Maximum 4 hôtels à comparer.');return;}}
     var cb=pc.querySelector('.pc-cmpbtn');if(cb)cb.textContent=n>=2?'Comparer ('+n+')':'Comparer tout';return;}}
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
 [].slice.call(log.querySelectorAll('.msg.ai[data-fix]')).slice(-3).forEach(function(el){{
   fetch('/portail/assistant/fix',{{method:'POST',headers:{{'Content-Type':'application/json'}},
     body:JSON.stringify({{chat:box.getAttribute('data-chat'),id:el.getAttribute('data-fix')}})}})
   .then(function(r){{return r.json();}}).then(function(j){{
     if(j&&j.html){{var t=document.createElement('div');t.innerHTML=j.html;
       var n=t.firstElementChild;if(n){{n.removeAttribute('data-fix');el.replaceWith(n);}}}}
     else{{var f=el.querySelector('.afix');if(f)f.remove();}}}})
   .catch(function(){{var f=el.querySelector('.afix');if(f)f.remove();}});
 }});
 var side=document.getElementById('aSide'),stog=document.getElementById('aSideTog');
 if(stog&&side)stog.addEventListener('click',function(e){{e.stopPropagation();side.classList.toggle('open');}});
 document.addEventListener('click',function(e){{if(side&&side.classList.contains('open')&&!side.contains(e.target))side.classList.remove('open');}});
 if(side)side.addEventListener('click',function(e){{
   var x=e.target.closest('.csb-x');if(!x)return;e.preventDefault();
   var it=x.closest('.csb-i'),id=it.getAttribute('data-id');
   if(!confirm('Supprimer cette conversation?'))return;
   fetch('/portail/assistant/chat/'+id+'/delete',{{method:'POST'}}).then(function(){{
     if(String(box.getAttribute('data-chat'))===String(id))location.href='/portail?c=new';else it.remove();}});}});
}})();
</script>"""


# --------------------------------------------------------------------------- #
# Chat endpoint (streamed NDJSON)
# --------------------------------------------------------------------------- #
def _ev(t: str, v) -> str:
    return json.dumps({"t": t, "v": v}, ensure_ascii=False) + "\n"


def _domains(raw: str) -> list:
    return [d.strip().lower() for d in (raw or "").split(",") if d.strip()]


def _blocked(url: str) -> bool:
    return _on(url, _domains(settings.PORTAL_ASSISTANT_BLOCKED_DOMAINS))


def _search_tool() -> dict:
    tool = {"type": "web_search_20250305", "name": "web_search",
            "max_uses": settings.PORTAL_ASSISTANT_SEARCHES_PER_ANSWER,
            "user_location": {"type": "approximate", "city": "Montreal",
                              "region": "Quebec", "country": "CA",
                              "timezone": "America/Toronto"}}
    allowed = _domains(settings.PORTAL_ASSISTANT_SEARCH_DOMAINS)
    if allowed:                      # the API takes one list or the other, not both
        tool["allowed_domains"] = allowed
    elif _domains(settings.PORTAL_ASSISTANT_BLOCKED_DOMAINS):
        tool["blocked_domains"] = _domains(settings.PORTAL_ASSISTANT_BLOCKED_DOMAINS)
    return tool


def _collect_sources(message) -> list:
    seen, out = set(), []
    for block in getattr(message, "content", None) or []:
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen and not _blocked(url):
                seen.add(url)
                out.append({"url": url, "title": getattr(c, "title", None) or ""})
    return out[:12]


# --------------------------------------------------------------------------- #
# Model providers — Claude (Anthropic) or ChatGPT (OpenAI), same event stream
# --------------------------------------------------------------------------- #
PROVIDERS = ("anthropic", "openai")


def _full() -> bool:
    return settings.PORTAL_ASSISTANT_STYLE.strip().lower() != "concis"


def _max_tokens() -> int:
    return 12000 if _full() else 6000          # text + the structured results
_LABELS = {"anthropic": "Claude", "openai": "ChatGPT"}


class SearchRefused(Exception):
    """The model rejected the web-search tool: retry the same turn without it."""


def provider_ready(provider: str) -> bool:
    return bool(settings.ANTHROPIC_API_KEY if provider == "anthropic" else settings.OPENAI_API_KEY)


def model_name(provider: str) -> str:
    return settings.PORTAL_ASSISTANT_MODEL if provider == "anthropic" else settings.OPENAI_ASSISTANT_MODEL


def _run_anthropic(search: bool, system: list, convo: list):
    """Yields ("status"|"text", str) then ("meta", {"sources", "searches",
    "cites", "chars", "data"}); data = the afficher_resultats tool input."""
    api = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    sources, searches, wrote, cites, base, data, seen = [], 0, False, [], 0, None, []
    usage = {"in": 0, "out": 0, "cache": 0}
    for _attempt in range(3):                     # pause_turn continuations
        qbuf: dict = {}                           # block index -> partial search input JSON
        blocks = [{"type": "text", "text": system[0], "cache_control": {"type": "ephemeral"}}]
        if system[1]:
            blocks.append({"type": "text", "text": system[1]})
        kw = dict(model=settings.PORTAL_ASSISTANT_MODEL, max_tokens=_max_tokens(),
                  system=blocks, messages=convo,
                  tools=([_search_tool()] if search else []) + [RESULTS_TOOL])
        if settings.PORTAL_ASSISTANT_THINKING > 0:
            kw["thinking"] = {"type": "enabled", "budget_tokens": settings.PORTAL_ASSISTANT_THINKING}
            kw["max_tokens"] += settings.PORTAL_ASSISTANT_THINKING
        try:
            with api.messages.stream(**kw) as stream:
                for event in stream:
                    et = getattr(event, "type", "")
                    if et == "content_block_start":
                        cb = getattr(event, "content_block", None)
                        if getattr(cb, "type", "") == "server_tool_use":
                            qbuf[getattr(event, "index", -1)] = ""
                            yield ("status", "🔎 Je cherche des options en ligne…")
                        elif getattr(cb, "type", "") == "web_search_tool_result":
                            n = len(cb.content) if isinstance(getattr(cb, "content", None), list) else 0
                            if n:
                                yield ("step", f"📄 {n} pages trouvées")
                        elif getattr(cb, "type", "") == "tool_use":
                            yield ("status", "📸 Je prépare les fiches…")
                    elif et == "content_block_delta":
                        d = getattr(event, "delta", None)
                        if getattr(d, "type", "") == "text_delta" and d.text:
                            wrote = True
                            yield ("text", d.text)
                        elif getattr(d, "type", "") == "input_json_delta" and \
                                getattr(event, "index", -1) in qbuf:
                            qbuf[event.index] += d.partial_json or ""
                    elif et == "content_block_stop" and getattr(event, "index", -1) in qbuf:
                        try:
                            qq = json.loads(qbuf.pop(event.index) or "{}").get("query", "")
                        except ValueError:
                            qq = ""
                        if qq:
                            yield ("step", f"🔎 {qq[:90]}")
                final = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            if search and not wrote:
                raise SearchRefused(str(exc)) from exc
            raise
        searches += getattr(getattr(final.usage, "server_tool_use", None),
                            "web_search_requests", 0) or 0
        usage["in"] += getattr(final.usage, "input_tokens", 0) or 0
        usage["out"] += getattr(final.usage, "output_tokens", 0) or 0
        usage["cache"] += getattr(final.usage, "cache_read_input_tokens", 0) or 0
        sources += [s for s in _collect_sources(final)
                    if s["url"] not in {x["url"] for x in sources}]
        for block in final.content or []:            # text blocks == streamed text, in order
            if getattr(block, "type", "") == "web_search_tool_result":
                for r in getattr(block, "content", None) or [] if isinstance(getattr(block, "content", None), list) else []:
                    u = getattr(r, "url", None)
                    if u and not _blocked(u):
                        seen.append({"url": u, "title": getattr(r, "title", None) or ""})
                continue
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == RESULTS_TOOL["name"]:
                data = block.input if isinstance(block.input, dict) else data
                continue
            if getattr(block, "type", "") != "text":
                continue
            base += len(block.text or "")
            for c in getattr(block, "citations", None) or []:
                if getattr(c, "url", None) and not _blocked(c.url):
                    cites.append({"start": None, "at": base, "url": c.url,
                                  "title": getattr(c, "title", None) or "",
                                  "snippet": getattr(c, "cited_text", None) or ""})
        if final.stop_reason == "pause_turn":
            convo = convo + [{"role": "assistant", "content": final.content}]
            continue
        break
    if final.stop_reason == "max_tokens":
        log.warning("assistant[anthropic]: answer cut at max_tokens (data=%s)", bool(data))
    yield ("meta", {"sources": sources, "searches": searches, "cites": cites, "chars": base,
                    "data": data, "seen": seen, "usage": usage,
                    "cut": final.stop_reason == "max_tokens"})


def _openai_tool() -> dict:
    domains = _domains(settings.PORTAL_ASSISTANT_SEARCH_DOMAINS)
    tool = {"type": "web_search",
            "user_location": {"type": "approximate", "country": "CA", "city": "Montreal",
                              "region": "Quebec", "timezone": "America/Toronto"}}
    if domains:
        tool["filters"] = {"allowed_domains": domains}
    return tool


def _openai_results_tool() -> dict:
    return {"type": "function", "name": RESULTS_TOOL["name"],
            "description": RESULTS_TOOL["description"],
            "parameters": RESULTS_TOOL["input_schema"], "strict": False}


def _run_openai(search: bool, system: list, convo: list):
    """Same contract as _run_anthropic, on OpenAI's Responses API."""
    import openai                                  # optional dependency
    api = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    kw = dict(model=settings.OPENAI_ASSISTANT_MODEL, instructions="\n\n".join(x for x in system if x),
              input=[{"role": m["role"], "content": m["content"]} for m in convo],
              max_output_tokens=_max_tokens() + 6000, stream=True)   # + reasoning
    if settings.OPENAI_REASONING_EFFORT:
        kw["reasoning"] = {"effort": settings.OPENAI_REASONING_EFFORT}
    kw["tools"] = ([_openai_tool()] if search else []) + [_openai_results_tool()]
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
            elif et == "response.output_item.added" and \
                    getattr(getattr(event, "item", None), "type", "") == "function_call":
                yield ("status", "📸 Je prépare les fiches…")
            elif et == "response.output_item.done" and \
                    getattr(getattr(event, "item", None), "type", "") == "web_search_call":
                qq = getattr(getattr(event.item, "action", None), "query", "") or ""
                if qq:
                    yield ("step", f"🔎 {qq[:90]}")
            elif et == "response.completed":
                final = event.response
            elif et in ("error", "response.failed"):
                raise RuntimeError(getattr(event, "message", None) or et)
    except openai.BadRequestError as exc:
        if search and not wrote:
            raise SearchRefused(str(exc)) from exc
        raise
    sources, searches, cites, base, data = [], 0, [], 0, None
    for item in getattr(final, "output", None) or []:
        if getattr(item, "type", "") == "web_search_call":
            searches += 1
        if getattr(item, "type", "") == "function_call" and getattr(item, "name", "") == RESULTS_TOOL["name"]:
            try:
                data = json.loads(getattr(item, "arguments", "") or "{}")
            except ValueError:
                log.warning("assistant[openai]: bad results arguments")
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
    sources = [x for x in sources if not _blocked(x["url"])]
    cites = [c for c in cites if not _blocked(c["url"])]
    u = getattr(final, "usage", None)
    usage = {"in": getattr(u, "input_tokens", 0) or 0, "out": getattr(u, "output_tokens", 0) or 0,
             "cache": getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0}
    yield ("meta", {"sources": sources[:12], "searches": searches, "cites": cites, "chars": base,
                    "data": data if isinstance(data, dict) else None, "usage": usage,
                    "cut": getattr(final, "status", "") == "incomplete"})


_RUNNERS = {"anthropic": _run_anthropic, "openai": _run_openai}
wholesaler_photos.ensure_fresh()            # loads the photo index in the background

_RESULT_KEYS = ("recherche", "destinations", "hotels", "prix_vus", "budget", "questions", "suivis")


def coerce_results(data) -> dict:
    """The tool input as clean Python data: models sometimes send nested
    arrays as JSON strings, or wrap everything in one key."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}
    if len(data) == 1 and isinstance(next(iter(data.values())), dict) and \
            not any(k in data for k in _RESULT_KEYS):
        data = next(iter(data.values()))          # {"resultats": {...}}
    out = dict(data)
    for k in _RESULT_KEYS:
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = [] if k != "recherche" else {}
    if isinstance(out.get("hotels"), dict):          # one hotel sent as an object
        out["hotels"] = [out["hotels"]]
    return out


def _valid_hotels(data: dict) -> bool:
    h = (data or {}).get("hotels")
    return isinstance(h, list) and any(isinstance(x, dict) and str(x.get("hotel") or "").strip() for x in h)


_REPAIR_ASK = ("Appelle maintenant l'outil afficher_resultats avec les données structurées qui "
               "correspondent exactement à ta réponse ci-dessus (hôtels, prix vus, budget, "
               "questions, suivis). N'écris rien d'autre.")


def repair_results(provider: str, system: list, msgs: list, answer: str):
    """Safety net: the answer was written but its structured results are
    missing (cut or skipped). One quick call, no search, tool forced."""
    convo = msgs + [{"role": "assistant", "content": answer},
                    {"role": "user", "content": _REPAIR_ASK}]
    try:
        if provider == "anthropic":
            api = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            r = api.messages.create(
                model=settings.PORTAL_ASSISTANT_MODEL, max_tokens=6000,
                system=[{"type": "text", "text": system[0], "cache_control": {"type": "ephemeral"}}]
                + ([{"type": "text", "text": system[1]}] if system[1] else []),
                messages=convo, tools=[RESULTS_TOOL],
                tool_choice={"type": "tool", "name": RESULTS_TOOL["name"]})
            for b in r.content or []:
                if getattr(b, "type", "") == "tool_use" and isinstance(getattr(b, "input", None), dict):
                    return b.input
        else:
            import openai
            api = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
            r = api.responses.create(
                model=settings.OPENAI_ASSISTANT_MODEL, instructions="\n\n".join(x for x in system if x),
                input=[{"role": m["role"], "content": m["content"]} for m in convo],
                tools=[_openai_results_tool()],
                tool_choice={"type": "function", "name": RESULTS_TOOL["name"]},
                max_output_tokens=8000)
            for it in getattr(r, "output", None) or []:
                if getattr(it, "type", "") == "function_call":
                    return json.loads(getattr(it, "arguments", "") or "{}")
    except Exception as exc:  # noqa: BLE001 — the text answer still stands
        log.warning("assistant[%s]: results repair failed: %s", provider, exc)
    return None

place_details = places.enrich            # Google Places (no-op without a key)


def _history_msgs(history: list, q: str) -> list:
    """Past turns for the model. What the cards showed is summarized in plain
    words (never as data tags, so the model keeps using the results tool)."""
    def ctx(m):
        notes = []
        if m.get("destinations"):
            notes.append("destinations : " + ", ".join(d.get("nom", "") for d in m["destinations"]))
        if m.get("cards"):
            notes.append("hôtels : " + "; ".join(
                c.get("hotel", "") + (f" ({c['titre']})" if c.get("titre") else "")
                + (f", {_money(c.get('total')) or _money(c.get('prix_personne'))}"
                   if (c.get("total") or c.get("prix_personne")) else "")
                for c in m["cards"]))
        if m.get("questions"):
            notes.append("questions posées : " + " / ".join(
                f"{x['q']} [{', '.join(x.get('options', []))}]" for x in m["questions"]))
        return m["text"] + (("\n\n[Affiché au membre — " + " | ".join(notes) + "]") if notes else "")
    msgs = [{"role": m["role"], "content": ctx(m)}
            for m in history[-_CONTEXT:] if m.get("text")]
    while msgs and msgs[0]["role"] != "user":    # start with user, alternate
        msgs.pop(0)
    msgs.append({"role": "user", "content": q})
    return msgs


def answer_events(provider: str, cid: int, q: str, history: list, name: str,
                  can_search: bool, persist, count_searches: bool = True,
                  debug: bool = False, triage: bool = False):
    """The whole answer pipeline as NDJSON lines, for either provider.
    `persist(user_entry, assistant_entry)` stores the exchange at the end."""
    last_ai = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
    if triage and trip_brief.enabled() and not (last_ai and last_ai.get("brief")):
        prof = trip_brief.extract(history, q)
        miss = trip_brief.missing(prof or {})
        if miss:                            # ask first: no web search, no Google calls
            now = datetime.utcnow().isoformat(timespec="seconds")
            entry = {"role": "assistant", "id": secrets.token_hex(8),
                     "text": trip_brief.intro_text(prof, miss), "brief": {"p": prof, "miss": miss},
                     "provider": "triage", "model": settings.PORTAL_ASSISTANT_TRIAGE_MODEL, "at": now}
            info = persist({"role": "user", "text": q, "at": now}, entry) or {}
            log.info("assistant: brief asked (missing %s)", ",".join(miss))
            yield _ev("text", entry["text"])
            yield _ev("final", {"html": _bubble(entry, last=True), "refine": "", **info})
            return
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
    steps: list = []
    t0 = time.monotonic()

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
                        if kind == "step":
                            steps.append(val)
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
            yield _ev("marked", render_md(tidy_text(marked), sources))
        else:
            marked = ""
    data = coerce_results(meta.get("data"))
    repaired = False
    want_hotels = "[[HOTELS]]" in answer or "[[TAB:hotels]]" in answer
    want_prix = "[[PRIX]]" in answer or "[[TAB:prix]]" in answer
    no_prix = not (data.get("prix_vus") or any(
        isinstance(h, dict) and h.get("total") for h in data.get("hotels") or [] if isinstance(data.get("hotels"), list)))
    if ((want_hotels and not _valid_hotels(data)) or (want_prix and no_prix)) and not _PISTES_RE.search(full):
        log.warning("assistant[%s]: hotels missing from results (cut=%s, keys=%s, hotels=%s)",
                    provider, meta.get("cut"), sorted(data), type(data.get("hotels")).__name__)
        repaired = True
        yield _ev("status", "📸 Je prépare les fiches…")
        fixed = coerce_results(repair_results(provider, system_for(search), msgs, answer))
        if _valid_hotels(fixed) or fixed.get("prix_vus"):
            data = {**data, **{k: v for k, v in fixed.items() if v and (k != "hotels" or _valid_hotels(fixed))}}
        log.info("assistant[%s]: results repaired=%s", provider, _valid_hotels(fixed))
    u = meta.get("usage") or {}
    log.info("assistant[%s] %s: %.1fs, %s searches, tokens in=%s (cache %s) out=%s, hotels=%s",
             provider, model_name(provider), time.monotonic() - t0, meta.get("searches", 0),
             u.get("in"), u.get("cache"), u.get("out"), len(data.get("hotels") or []))
    known = sources + [x for x in meta.get("seen") or [] if x["url"] not in {y["url"] for y in sources}]

    def block(key, regex):
        """Structured tool input first; old tagged blocks as a fallback."""
        if key in data:
            return json.dumps(data.get(key) or [], ensure_ascii=False)
        mm = regex.search(full)
        return mm.group(1).strip() if mm else ""
    dests = parse_destinations(block("destinations", _DEST_RE))
    if dests:
        dests = add_destination_images(dests, {x["url"] for x in known})
        yield _ev("dests", destinations_html(dests))
    rech = parse_recherche(data.get("recherche"))
    cards = parse_pistes(block("hotels", _PISTES_RE), known)
    for c in cards:
        c["search_url"] = search_link(rech, c["hotel"])
    if cards:
        yield _ev("status", "📸 Je prépare les fiches…")
        cards = add_images(cards, {x["url"]: x.get("title", "") for x in known})
        if place_details is not None:
            cards = place_details(cards)
        yield _ev("cards", cards_html(cards))
    prix = parse_prix(data.get("prix_vus"), known) or prix_from_cards(cards)
    if prix:
        yield _ev("prix", prix_html(prix))
    budget = parse_budget(data.get("budget"))
    if budget_html(budget):
        yield _ev("budget", budget_html(budget))
    if sources:
        yield _ev("sources", sources)
    suivis = parse_suivis(block("suivis", _SUIVIS_RE))
    if not answer:
        return
    mid = secrets.token_hex(8)
    yield _ev("rate", rate_html(mid))
    questions = parse_questions(block("questions", _QUEST_RE))
    if questions:
        yield _ev("questions", questions_html(questions))
    if suivis:
        yield _ev("refine", suivis_html(suivis))
    if not cards and not data and "[[HOTELS]]" in answer:
        log.warning("assistant[%s]: hotels slot without structured results", provider)
    now = datetime.utcnow().isoformat(timespec="seconds")
    entry = {"role": "assistant", "id": mid, "text": answer, "marked": marked, "cards": cards,
             "destinations": dests, "questions": questions, "prix": prix, "budget": budget,
             "suivis": suivis, "sources": sources, "steps": steps[:12], "recherche": rech,
             "provider": provider, "model": model_name(provider), "at": now}
    if repaired:
        entry["fixed"] = True              # already had its second chance
    info = persist({"role": "user", "text": q, "at": now}, entry) or {}
    yield _ev("final", {"html": _bubble(entry, last=True), "refine": suivis_html(suivis), **info})


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
        _migrate_thread(db, client)
        if "chat" in payload:              # current page: explicit chat id (null = new chat)
            ch = get_chat(db, cid, payload.get("chat")) if payload.get("chat") else None
        else:                              # pages opened before multi-chat: continue the latest
            ch = next(iter(recent_chats(db, cid, 1)), None)
        history = list(ch.messages or []) if ch else []
        chat_id = ch.id if ch else None
        name = _first_name(client)
    _HITS.setdefault(cid, []).append(time.monotonic())

    def persist(user_entry, ai_entry):
        with SessionLocal() as db2:
            c = get_chat(db2, cid, chat_id) if chat_id else None
            if c is None:
                c = AssistantChat(client_id=cid, title=_chat_title(user_entry.get("text", "")), messages=[])
                db2.add(c)
            c.messages = (list(c.messages or []) + [user_entry, ai_entry])[-_KEEP:]
            c.updated_at = datetime.utcnow()
            db2.commit()
            return {"chat": c.id, "title": c.title}

    return _stream(answer_events(provider, cid, q, history, name,
                                 _searches_left(cid) > 0, persist, triage=True), ndjson)


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
        idx, chat, conv = None, [], None
        for conv in recent_chats(db, cid, 200):
            chat = list(conv.messages or [])
            idx = next((i for i, m in enumerate(chat) if m.get("id") == mid), None)
            if idx is not None:
                break
        pending = _PENDING.get(mid)
        if idx is not None:
            chat[idx] = {**chat[idx], "rating": rating}
            conv.messages = chat
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


def restructure(entry: dict, before: list, q: str) -> dict:
    """Rebuild the hotel cards / price table of a stored answer from its own
    text (one forced-tool call, no web search). Returns the updated entry."""
    provider = entry.get("provider") if entry.get("provider") in PROVIDERS else "anthropic"
    if not provider_ready(provider):
        return entry
    system = [SYSTEM_PROMPT.format(today=_today(), search_rule=_SEARCH_OFF,
                                   catalogue=kb.catalogue(), excerpts=""), ""]
    data = coerce_results(repair_results(provider, system, _history_msgs(before, q), entry.get("text", "")))
    known = list(entry.get("sources") or [])
    out = dict(entry, fixed=True)
    rech = parse_recherche(data.get("recherche")) or entry.get("recherche") or {}
    if not entry.get("cards") and _valid_hotels(data):
        cards = parse_pistes(json.dumps(data["hotels"], ensure_ascii=False), known)
        for c in cards:
            c["search_url"] = search_link(rech, c["hotel"])
        if cards:
            cards = add_images(cards, {x["url"]: x.get("title", "") for x in known})
            if place_details is not None:
                cards = place_details(cards)
        out["cards"] = cards
    if not entry.get("prix"):
        out["prix"] = parse_prix(data.get("prix_vus"), known) or prix_from_cards(out.get("cards"))
    if not entry.get("budget"):
        out["budget"] = parse_budget(data.get("budget"))
    if rech and not entry.get("recherche"):
        out["recherche"] = rech
    log.info("assistant: restructured %s (hotels=%s, prix=%s)", entry.get("id"),
             len(out.get("cards") or []), len(out.get("prix") or []))
    return out


@router.post("/portail/assistant/fix")
async def assistant_fix(request: Request):
    """Lazy repair of a stored answer shown without its cards/table."""
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        payload = {}
    mid = str(payload.get("id") or "")
    with SessionLocal() as db:
        ch = get_chat(db, cid, payload.get("chat"))
        msgs = list(ch.messages or []) if ch else []
    i = next((k for k, m in enumerate(msgs) if m.get("id") == mid and m.get("role") == "assistant"), -1)
    if i < 1 or not needs_structure(msgs[i]):
        return JSONResponse({"html": _bubble(msgs[i], last=(i == len(msgs) - 1)) if i >= 0 else ""})
    q = msgs[i - 1].get("text", "") if msgs[i - 1].get("role") == "user" else ""
    new = restructure(msgs[i], msgs[:i - 1], q)
    with SessionLocal() as db:
        ch = get_chat(db, cid, payload.get("chat"))
        if ch:
            cur = list(ch.messages or [])
            for k, m in enumerate(cur):
                if m.get("id") == mid:
                    cur[k] = new
            ch.messages = cur
            db.commit()
    return JSONResponse({"html": _bubble(dict(new, fixed=True), last=(i == len(msgs) - 1))})


@router.post("/portail/assistant/chat/{chat_id}/delete")
def assistant_chat_delete(request: Request, chat_id: int):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"error": "auth"}, status_code=401)
    with SessionLocal() as db:
        ch = get_chat(db, cid, chat_id)
        if not ch:
            return JSONResponse({"error": "not found"}, status_code=404)
        db.delete(ch)
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
_CRIT_KEYS = ("dep", "dest", "date", "nuits", "adultes", "enfants", "ages", "hotel")


def _criteria(request: Request) -> dict:
    """Trip criteria passed from the assistant (validated, same rules as the model's)."""
    p = request.query_params
    raw = {"depart": p.get("dep"), "destination": p.get("dest"), "date_depart": p.get("date"),
           "nuits": p.get("nuits"), "adultes": p.get("adultes"), "enfants": p.get("enfants"),
           "ages_enfants": [int(x) for x in (p.get("ages") or "").split(",") if x.strip().isdigit()]}
    crit = parse_recherche(raw)
    if _txt(p.get("hotel"), 90):
        crit["hotel"] = _txt(p.get("hotel"), 90)
    return crit


def _engine_url(crit: dict | None = None) -> str:
    """DMO engine URL; trip criteria mapped to the engine's parameter names
    (SIRRVA_PARAM_MAP — to confirm with Softvoyage)."""
    q = {"code_ag": settings.SIRRVA_CODE_AG, "alias": settings.SIRRVA_ALIAS or "duvoyageur",
         "language": "fr"}
    mapping = dict(x.split("=", 1) for x in settings.SIRRVA_PARAM_MAP.split(",") if "=" in x)
    for k, v in (crit or {}).items():
        if mapping.get(k):
            q[mapping[k].strip()] = v
    return f"{settings.SIRRVA_HOST.rstrip('/')}/cgi-bin/querypackage.cgi?{urlencode(q)}"


def _crit_chips(crit: dict) -> str:
    if not crit:
        return ""
    lab = []
    if crit.get("dep"):
        lab.append(f"✈️ Départ {crit['dep']}")
    if crit.get("dest"):
        lab.append(f"📍 {crit['dest']}")
    if crit.get("date"):
        y, m, d = crit["date"].split("-")
        lab.append(f"🗓️ {int(d)} {_MONTHS[int(m) - 1]} {y}")
    if crit.get("nuits"):
        lab.append(f"🌙 {crit['nuits']} nuits")
    if crit.get("adultes"):
        lab.append(f"👤 {crit['adultes']} adulte{'s' if crit['adultes'] > 1 else ''}")
    if crit.get("enfants"):
        lab.append(f"🧒 {crit['enfants']} enfant{'s' if crit['enfants'] > 1 else ''}"
                   + (f" ({crit['ages']} ans)" if crit.get("ages") else ""))
    if crit.get("hotel"):
        lab.append(f"🏨 {crit['hotel']}")
    return ("<div class='crit'><span class='crit-h'>Ta recherche</span>"
            + "".join(f"<span class='crit-c'>{escape(x)}</span>" for x in lab) + "</div>")


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
    crit = _criteria(request)
    note = "" if settings.SIRRVA_ALIAS else (
        "<div class='note warn'>Moteur en cours de configuration avec Softvoyage : tes critères "
        "ci-dessus lui seront transmis automatiquement dès son activation.</div>")
    body = ("<div class='hello'><h2>Rechercher un forfait</h2>"
            "<p class='lede'>Ton Rabais Récompense est déjà appliqué aux prix affichés.</p></div>"
            + _crit_chips(crit) + note
            + f"<div class='engine'><iframe src=\"{escape(_engine_url(crit))}\" "
              "title='Moteur de recherche de forfaits' loading='eager'></iframe></div>"
            + "<p class='muted'><a href='/portail'>← Retour à ton assistant voyage</a></p>")
    resp = HTMLResponse(_shell("Rechercher", body, logged_in=True,
                               nav=_nav("recherche"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.get("/portail/assistant/debug-photos", response_class=HTMLResponse)
def debug_photos(request: Request, url: str = "", hotel: str = "", area: str = ""):
    """Testers only: what our server actually sees on a wholesaler hotel page."""
    cid = current_portal_client_id(request)
    with SessionLocal() as db:
        if not is_tester(db.get(Client, cid) if cid else None):
            return HTMLResponse(status_code=404)
    st = wholesaler_photos.status()
    out = (f"<p>Index Transat : <b>{st['hotels']}</b> hôtels"
           + (f" · erreur : {escape(st['error'])}" if st["error"] else "") + "</p>")
    if hotel and not url:
        hit = wholesaler_photos.lookup(hotel, area)
        out += (f"<p>Correspondance : <a href=\"{escape(hit['page'])}\">{escape(hit['slug'])}</a></p>"
                + "".join(f"<img src=\"{escape(i)}?width=400\" style='height:110px;margin:4px;border-radius:8px'>"
                          for i in hit["images"])) if hit else "<p>Aucune correspondance.</p>"
    if url.startswith("https://"):
        _OG_CACHE.pop(url, None)
        img, dead = page_preview(url)
        pics = page_gallery(url, hotel, area)
        d = _DIAG.get(url, {})
        out += (f"<p>Statut HTTP : <b>{escape(str(d.get('status', '—')))}</b> · type : {escape(d.get('type', '—'))} · "
               f"taille : {d.get('bytes', 0):,} octets · &lt;img&gt; : {d.get('imgs', 0)} · "
               f"images dans les données : {d.get('raw_imgs', 0)}</p>"
               f"<p>Image de partage (og:image) : {escape(img or '—')}</p>"
               f"<h3>Photos retenues pour « {escape(hotel)} » : {len(pics)}</h3>"
               + "".join(f"<img src=\"{escape(p)}\" referrerpolicy='no-referrer' "
                         "style='height:110px;margin:4px;border-radius:8px'>" for p in pics)
               + "<h3>Toutes les images trouvées (40 premières)</h3><ol>"
               + "".join(f"<li>{escape(u)}</li>" for u in (_RAW.get(url) or [])[:40]) + "</ol>")
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Debug photos</title>"
        "<body style='font-family:system-ui;padding:20px;background:#03121b;color:#eafcff'>"
        "<h2>Debug photos grossiste</h2><form>"
        f"<input name='url' value=\"{escape(url)}\" placeholder='URL de la fiche (Transat, Sunwing…)' style='width:60%'> "
        f"<input name='hotel' value=\"{escape(hotel)}\" placeholder=\"Nom de l'hôtel\"> "
        f"<input name='area' value=\"{escape(area)}\" placeholder='Destination'> <button>Analyser</button></form>{out}")
