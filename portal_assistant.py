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
Tu es l'assistant voyage de Du Voyageur, une agence de voyages québécoise
(permis du Québec 700495). Tu parles à un membre connecté à son espace client.
Nous sommes le {today}.

Comment fonctionne Du Voyageur : le membre cherche et réserve LUI-MÊME son
forfait dans notre moteur de réservation (bouton « Rechercher un forfait » dans
son espace). Son Rabais Récompense (jusqu'à 5 % sur le prix avant taxes) est
déjà appliqué aux prix affichés dans ce moteur.

TON RÔLE : un conseiller d'expérience qui aide à CHOISIR, pas un moteur de prix.

1. Avant de chercher, vérifie que tu as l'essentiel : la période (dates ou
   mois), qui voyage (adultes, enfants et leurs âges) et la ville de départ.
   - S'il manque la période ou les voyageurs, NE CHERCHE PAS encore : pose en
     un seul message court les 2 ou 3 questions qui manquent, avec des
     exemples de réponse, puis arrête-toi.
   - Ville de départ non précisée : suppose Montréal (YUL) et dis-le.
   - Budget, style, priorités : utiles mais pas obligatoires.
   - Ne repose jamais une question déjà répondue plus haut dans la conversation.
2. Quand tu as l'essentiel : {search_rule}
   Propose 2 ou 3 pistes VARIÉES (pas trois hôtels identiques) qui répondent
   vraiment aux priorités nommées.
3. Raisonne comme un conseiller qui connaît le Sud, et dis-le en une ligne
   par piste quand c'est pertinent :
   - Saison des ouragans dans l'Atlantique et les Caraïbes : juin à novembre,
     surtout août à octobre.
   - Sargasses : surtout sur la côte caraïbe du Mexique (Cancún, Riviera Maya)
     et parfois à Punta Cana, plutôt du printemps à l'automne ; ça varie
     beaucoup d'une année à l'autre. Côté Pacifique (Puerto Vallarta, Los
     Cabos, Huatulco), ce n'est pas un enjeu.
   - Plage : mer calme ou vagues, sable ou roches, plage en pente douce pour
     les enfants. Ne l'affirme que si tes sources ou notre contenu le disent.
   - Durée du transfert aéroport-hôtel, vol direct ou avec escale, taille du
     complexe, adultes seulement vs famille (mini-club, glissades).
   - Cuba : rappelle de vérifier l'avis aux voyageurs actuel sur voyage.gc.ca.
4. Utilise d'abord le CONTENU VÉRIFIÉ DE DU VOYAGEUR ci-dessous : nos
   destinations et nos coups de cœur. Quand un de nos coups de cœur colle aux
   critères, privilégie-le. Ne contredis jamais ce contenu. Les liens vers nos
   pages s'affichent automatiquement : ne les écris pas.
5. Sois honnête : dis clairement ce que tu n'as pas pu confirmer (vol direct,
   date exacte, prix total). Les prix publics changent vite : présente-les
   comme « vu en ligne », jamais comme garantis.
6. Termine une réponse avec des prix par une phrase du genre : « Ces prix sont
   des prix publics. Cherche le même forfait dans notre moteur : c'est souvent
   moins cher, avec ton Rabais Récompense déjà appliqué. » Puis, si utile, UNE
   question pour affiner.
7. Questions générales (saison, tout inclus, bagages, pourboires) : réponds
   directement, sans chercher, en t'appuyant sur notre contenu.

Règles strictes :
- Ne promets jamais que notre prix sera plus bas ni un montant de rabais
  précis : dis « souvent moins cher » et « jusqu'à 5 % avant taxes ».
- Tu ne peux pas réserver ni confirmer une disponibilité.
- N'invente jamais un hôtel, un prix ou un détail : un prix vient de tes
  résultats de recherche ; un détail d'hôtel vient de tes sources ou de notre
  contenu.
- Ne recommande pas d'autres agences de voyages.
- Passeports, visas, santé : idée générale, et renvoie à voyage.gc.ca.
- Problème avec une réservation, changement, annulation, ou envie de parler à
  un humain : bouton « Parler à un conseiller » ou Messenger (onglet « Aide »).
- Hors voyage : ramène gentiment la conversation au voyage.

BLOCS DE DONNÉES (à la TOUTE FIN de ta réponse, sans rien après, jamais
mentionnés dans ton texte) :
a) Quand tu proposes des forfaits trouvés en ligne avec un prix :
<pistes>[{{"hotel": "Ocean El Faro", "destination": "Punta Cana",
"depart": "15 novembre 2026", "nuits": 7, "prix_personne": 1887,
"total": 3774, "voyageurs": "2 adultes", "taxes": "incluses",
"source_url": "https://…", "a_confirmer": "vol direct"}}]</pistes>
   - Un objet par piste (3 maximum), uniquement avec des données vues dans
     tes résultats ; null pour ce que tu ne sais pas.
   - source_url = l'adresse exacte de la page où tu as vu le prix.
   - taxes : "incluses", "en sus" ou null. Montants en dollars, sans symbole.
   - Ces fiches s'affichent en cartes avec photo : ne répète pas les chiffres
     dans ton texte. Texte court : une phrase d'intro, une ligne de conseil par
     piste (pourquoi elle colle, ce qui reste à confirmer), la phrase sur notre
     moteur, ta question.
b) Après une réponse qui propose des pistes ou des destinations, 3 ou 4
   raffinements que le membre pourrait vouloir, rédigés comme SA demande,
   courts (moins de 40 caractères) et adaptés à la conversation :
<suivis>["Moins cher, autour de 3 000 $", "Seulement avec vol direct", "Plus calme, adultes seulement"]</suivis>

Style : français québécois chaleureux, tu tutoies, concis. Tu peux mettre
quelques mots en **gras** et faire des listes avec des tirets « - ». Pas de
titres (#), pas de séparateurs (---), pas de tableaux, pas de liens dans le
texte. Emojis : deux ou trois au maximum. Pas de résumé qui répète ce que tu
viens de dire.

CONTENU VÉRIFIÉ DE DU VOYAGEUR
Destinations que nous couvrons :
{catalogue}
{excerpts}
"""

_SEARCH_ON = ("cherche sur le web pour lui proposer des options réelles (ta "
              "recherche est limitée aux sites des voyagistes et d'information).")
_SEARCH_OFF = ("la recherche web n'est pas disponible pour cette réponse : donne "
               "des pistes générales de destinations SANS prix ni hôtel précis, et "
               "envoie-le vers notre moteur pour voir les prix.")

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
_TAGS = ("<pistes>", "<suivis>")


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


def og_image(url: str) -> str:
    """The page's share image (og:image / twitter:image), like a link preview.
    Best effort: 3 s timeout, 600 KB read cap, cached; "" when unavailable."""
    if url in _OG_CACHE:
        return _OG_CACHE[url]
    img = ""
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
    except Exception as exc:  # noqa: BLE001 — a missing photo is fine
        log.info("og:image unavailable for %s: %s", url, exc)
    if len(_OG_CACHE) > 500:
        _OG_CACHE.clear()
    _OG_CACHE[url] = img
    return img


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
        if len(cards) == 3:
            break
    return cards


def add_images(cards: list) -> list:
    urls = list({c["source_url"] for c in cards if c.get("source_url")})
    if urls:
        with ThreadPoolExecutor(max_workers=3) as ex:
            imgs = dict(zip(urls, ex.map(og_image, urls)))
        for c in cards:
            c["image"] = imgs.get(c.get("source_url"), "")
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
        ours = kb.hotel_page(c["hotel"])
        if ours:
            chip += (f"<a class='pc-ours' href=\"{escape(ours)}\" target='_blank' rel='noopener'>"
                     "⭐ Notre coup de cœur</a>")
        conf = (f"<div class='pc-conf'>À confirmer : {escape(c['a_confirmer'])}</div>"
                if c.get("a_confirmer") else "")
        img = c.get("image") or ""
        pic = (f"<img class='pc-img' src=\"{escape(img)}\" alt='' loading='lazy' "
               "referrerpolicy='no-referrer' onerror=\"this.remove()\">") if img else ""
        out.append(f"<div class='pcard{'' if pic else ' noimg'}'>{pic}<div class='pc-body'>"
                   f"{title}<div class='pc-sub'>{sub}</div>{price}{total}"
                   f"<div class='pc-foot'>{chip}<a class='pc-go' href='{SEARCH_URL}'>"
                   f"Voir dans notre moteur →</a></div>{conf}</div></div>")
    head = "Meilleure piste trouvée" if len(cards) == 1 else "Pistes trouvées"
    cmp_btn = cmp = ""
    if len(cards) >= 2:
        def cell(v):
            return f"<td>{v or '—'}</td>"
        rows = [
            ("Destination", [escape(c.get("destination") or "") for c in cards]),
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


def _bubble(m: dict) -> str:
    if m.get("role") == "user":
        return f"<div class='msg me'>{escape(m.get('text', ''))}</div>"
    return (f"<div class='msg ai'>{render_md(m.get('text', ''))}"
            f"{cards_html(m.get('cards'))}{guides_html(m.get('guides'))}"
            f"{_sources_html(m.get('sources'))}{rate_html(m.get('id', ''), m.get('rating', 0))}</div>")


def assistant_panel(client) -> str:
    """The home-page block: search CTA + chat thread + composer."""
    thread = list(client.assistant_chat or [])
    hi = escape(_first_name(client))
    intro = (f"Salut{(' ' + hi) if hi else ''}! Je suis ton assistant voyage. "
             "Décris-moi ton voyage (budget, dates, voyageurs, ce qui compte pour toi) "
             "et je te propose des pistes. Ensuite, trouve le même forfait avec ton "
             "rabais dans notre moteur de recherche.")
    msgs = (f"<div class='msg ai'>{intro}</div>"
            + "".join(_bubble(m) for m in thread))
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
   .replace(/^#{{1,6}}\\s*/gm,'').replace(/\\n{{3,}}/g,'\\n\\n');}}
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
   var old=log.querySelectorAll('.refine');old.forEach(function(x){{x.remove();}});
   add('me',q); inp.value=''; grow();
   send.disabled=true; inp.disabled=true;
   var out=add('ai typing',''),body=document.createElement('div'),st=document.createElement('div');
   st.className='astatus';out.appendChild(st);out.appendChild(body);
   var raw='',buf='';
   function handle(ev){{
     if(ev.t==='status'){{st.textContent=ev.v;out.classList.remove('typing');}}
     else if(ev.t==='text'){{out.classList.remove('typing');st.textContent='';raw+=ev.v;body.innerHTML=md(raw);}}
     else if((ev.t==='cards'||ev.t==='guides'||ev.t==='refine'||ev.t==='rate')&&ev.v){{
       st.textContent='';var cd=document.createElement('div');cd.innerHTML=ev.v;
       while(cd.firstChild)out.appendChild(cd.firstChild);}}
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
   var txt='Bonjour! Je viens de l\'assistant voyage de mon espace client.'+
     (me.length?'\nMa demande :\n'+me.join('\n'):'')+(pc.length?'\nPistes vues : '+pc.join(', '):'');
   var w=window.open(MSGR,'_blank','noopener');
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
    if not q:
        return HTMLResponse("Écris ta question 🙂", status_code=400)
    if not settings.ANTHROPIC_API_KEY:
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

    msgs = [{"role": m["role"], "content": m["text"] + (
                ("\n<pistes>" + json.dumps(m["cards"], ensure_ascii=False) + "</pistes>")
                if m.get("cards") else "")}
            for m in history[-_CONTEXT:] if m.get("text")]
    while msgs and msgs[0]["role"] != "user":   # API: start with user, alternate
        msgs.pop(0)
    msgs.append({"role": "user", "content": q})
    can_search = _searches_left(cid) > 0
    convo_text = " ".join([m["text"] for m in history[-6:] if m.get("role") == "user"] + [q])
    excerpts = kb.context_for(convo_text)
    catalogue = kb.catalogue()

    def system_for(search: bool) -> str:
        s = SYSTEM_PROMPT.format(
            today=_today(), search_rule=_SEARCH_ON if search else _SEARCH_OFF,
            catalogue=catalogue,
            excerpts=("\nExtraits de nos pages liés à la conversation :\n" + excerpts)
            if excerpts else "")
        return s + (f"\nLe membre s'appelle {name}." if name else "")

    def run(api, search: bool, convo: list):
        """Stream one model call; yields NDJSON events, returns final message."""
        kw = dict(model=settings.PORTAL_ASSISTANT_MODEL, max_tokens=1500,
                  system=system_for(search), messages=convo)
        if search:
            kw["tools"] = [_search_tool()]
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
                        yield ("text", d.text)
            yield ("final", stream.get_final_message())

    def gen():
        parts: list[str] = []          # everything the model wrote
        shown = [0]                    # chars of `parts` already sent
        sources: list = []
        def visible_cut(full: str) -> int:
            cuts = [i for i in (full.find(t) for t in _TAGS) if i != -1]
            if cuts:
                return min(cuts)
            for k in range(len("<suivis>") - 1, 0, -1):     # possible partial tag
                if any(full.endswith(t[:k]) for t in _TAGS):
                    return len(full) - k
            return len(full)

        def visible_delta() -> str:
            full = "".join(parts)
            cut = visible_cut(full)
            if cut > shown[0]:
                d = full[shown[0]:cut]
                shown[0] = cut
                return d
            return ""
        api = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        search = can_search
        convo = list(msgs)
        try:
            for _attempt in range(3):            # pause_turn continuations
                final = None
                try:
                    for kind, val in run(api, search, convo):
                        if kind == "final":
                            final = val
                        else:
                            if kind == "text":
                                parts.append(val)
                                d = visible_delta()
                                if d:
                                    yield _ev("text", d)
                            else:
                                yield _ev(kind, val)
                except anthropic.BadRequestError as exc:
                    if search and not parts:     # model/tool not supported -> no search
                        log.warning("assistant: web search refused, retrying without: %s", exc)
                        search = False
                        continue
                    raise
                if final is None:
                    break
                used = getattr(getattr(final.usage, "server_tool_use", None),
                               "web_search_requests", 0) or 0
                if used:
                    _SEARCHES.setdefault(cid, []).extend([time.monotonic()] * used)
                sources += [s for s in _collect_sources(final)
                            if s["url"] not in {x["url"] for x in sources}]
                if final.stop_reason == "pause_turn":
                    convo = convo + [{"role": "assistant", "content": final.content}]
                    continue
                break
        except Exception as exc:  # noqa: BLE001 — never break the page
            log.warning("assistant stream failed: %s", exc)
            if not parts:
                yield _ev("text", "Oups, je n'ai pas pu répondre. Réessaie dans un instant.")
            return
        full = "".join(parts)
        answer = strip_blocks(full)
        tail = full[shown[0]:len(answer) + (len(full) - len(full.lstrip()))]
        if shown[0] < len(answer) and tail.strip():     # text held back for a partial tag
            yield _ev("text", full[shown[0]:visible_cut(full)])
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
        if suivis:
            yield _ev("refine", suivis_html(suivis))
        now = datetime.utcnow().isoformat(timespec="seconds")
        with SessionLocal() as db2:
            c = db2.get(Client, cid)
            if c:
                c.assistant_chat = (list(c.assistant_chat or []) + [
                    {"role": "user", "text": q, "at": now},
                    {"role": "assistant", "id": mid, "text": answer, "cards": cards,
                     "guides": guides, "suivis": suivis, "sources": sources,
                     "model": settings.PORTAL_ASSISTANT_MODEL, "at": now},
                ])[-_KEEP:]
                db2.commit()

    def plain():
        for line in gen():
            ev = json.loads(line)
            if ev["t"] == "text":
                yield ev["v"]
            elif ev["t"] == "sources":
                yield "\n\nSources : " + " · ".join(x["url"] for x in ev["v"])

    if not ndjson:
        return StreamingResponse(plain(), media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})
    return StreamingResponse(gen(), media_type="application/x-ndjson; charset=utf-8",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


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
        if idx is None:                       # only the member's own answers
            return JSONResponse({"error": "not found"}, status_code=404)
        chat[idx] = {**chat[idx], "rating": rating}
        c.assistant_chat = chat
        q = next((chat[j]["text"] for j in range(idx - 1, -1, -1)
                  if chat[j].get("role") == "user"), None)
        row = db.query(AssistantFeedback).filter_by(message_id=mid).first()
        if not row:
            row = AssistantFeedback(message_id=mid, client_id=cid)
            db.add(row)
        row.rating, row.question, row.answer = rating, q, chat[idx].get("text")
        row.cards, row.model = chat[idx].get("cards") or [], chat[idx].get("model")
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
