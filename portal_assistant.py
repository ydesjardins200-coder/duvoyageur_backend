"""
portal_assistant.py
===================
The portal home's AI travel assistant ("Ton assistant voyage") + the package
search page it sends people to.

Scope is deliberately narrow: answer GENERIC travel questions (season, weather
in general terms, what "tout inclus" means, packing, documents to check…) and
steer the client to the self-service search engine (Softvoyage DMO, at
/portail/recherche) to find and book their package. It never quotes prices,
never confirms availability, never books, and never invents specific hotel
details.

- Replies stream to the browser (plain text chunks) so the UI feels live.
- The thread is stored on the Client (assistant_chat), capped to the last
  _KEEP messages; only the last _CONTEXT are sent to the model.
- Per-client daily cap (PORTAL_ASSISTANT_DAILY_MAX) keeps API cost bounded.
- Built so an API failure never breaks the page: the client gets a short
  apology and the rest of the portal keeps working.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from html import escape
from urllib.parse import urlencode

import anthropic
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from config import settings
from db import Client, SessionLocal
from portal import (_bell_panel, _logged_out, _nav, _set_session_cookie, _shell,
                    current_portal_client_id)

router = APIRouter()
log = logging.getLogger("duvoyageur.portal.assistant")

_KEEP = 60                  # messages stored per client
_CONTEXT = 16               # messages sent to the model (≈ 8 exchanges)
_MAX_LEN = 1000             # characters per client message
_DAY = 86400
_HITS: dict = {}            # client id -> [monotonic timestamps] (single replica)

SEARCH_URL = "/portail/recherche"

SYSTEM_PROMPT = """\
Tu es l'assistant voyage de Du Voyageur, une agence de voyages québécoise.
Tu parles à un membre connecté à son espace client.

Comment fonctionne Du Voyageur pour ce membre : il cherche et réserve LUI-MÊME
son forfait dans notre moteur de recherche (bouton « Rechercher un forfait »
dans son espace). Son Rabais Récompense est déjà appliqué automatiquement aux
prix affichés dans ce moteur.

Ton rôle :
- Répondre aux questions GÉNÉRALES de voyage : quand partir, saisons et météo en
  termes généraux, idées de destinations soleil, ce que veut dire « tout
  inclus », bagages, quoi prévoir avant de partir, différences entre régions.
- Dès que la personne veut voir des prix, des disponibilités, des hôtels précis
  ou réserver, l'envoyer vers le bouton « Rechercher un forfait ». Fais-le aussi
  naturellement en fin de réponse quand ça aide la personne à passer à l'action.

Règles strictes :
- Ne donne JAMAIS de prix, de montant de rabais, de disponibilité, et ne
  promets ou ne garantis rien. Tu ne peux pas réserver.
- N'invente jamais de détails précis (hôtels, restaurants sur place, horaires de
  vol, règles d'un transporteur). Si tu n'es pas certain, dis-le simplement.
- Passeports, visas, avis aux voyageurs et santé : donne seulement l'idée
  générale et renvoie au site officiel voyage.gc.ca pour l'information à jour.
- Pour un problème avec une réservation existante, un changement, une
  annulation ou s'il veut parler à un humain : dis-lui d'écrire à l'équipe sur
  Messenger (onglet « Aide » de son espace).
- Si la question n'a rien à voir avec le voyage, ramène gentiment la
  conversation au voyage.

Style : français québécois chaleureux, tu tutoies, réponses courtes (2 à 5
phrases). Pas de markdown, pas de titres ni d'astérisques ; une liste simple
avec des tirets seulement si c'est vraiment utile.
"""

_SUGGESTIONS = [
    "Quand partir au Mexique?",
    "Qu'est-ce qui est inclus dans un tout inclus?",
    "Quoi mettre dans ma valise pour le Sud?",
    "Quelle destination pour une famille avec des petits?",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _first_name(client) -> str:
    return ((client.kyc or {}).get("legal_first_name")
            or (client.display_name or "").split(" ")[0] or "")


def _rate_ok(cid: int) -> bool:
    now = time.monotonic()
    hits = [t for t in _HITS.get(cid, []) if now - t < _DAY]
    _HITS[cid] = hits
    return len(hits) < settings.PORTAL_ASSISTANT_DAILY_MAX


def _bubble(role: str, text: str) -> str:
    cls = "me" if role == "user" else "ai"
    return f"<div class='msg {cls}'>{escape(text)}</div>"


def assistant_panel(client) -> str:
    """The home-page block: search CTA + chat thread + composer."""
    thread = list(client.assistant_chat or [])
    hi = escape(_first_name(client))
    intro = (f"Salut{(' ' + hi) if hi else ''}! Je suis ton assistant voyage. "
             "Pose-moi tes questions (destinations, saison, bagages…) — et quand tu "
             "es prêt, trouve ton forfait avec ton rabais dans le moteur de recherche.")
    msgs = _bubble("assistant", intro) + "".join(
        _bubble(m.get("role", "assistant"), m.get("text", "")) for m in thread)
    chips = "" if thread else (
        "<div class='chips' id='aChips'>" + "".join(
            f"<button type='button' class='chip'>{escape(s)}</button>" for s in _SUGGESTIONS)
        + "</div>")
    reset = ("<button type='button' class='areset' id='aReset'>Nouvelle conversation</button>"
             if thread else "")
    return f"""
<a class='searchcta' href='{SEARCH_URL}'>
  <div><b>🔎 Rechercher un forfait</b>
  <span>Ton Rabais Récompense est déjà appliqué aux prix affichés.</span></div>
  <span class='sc-go'>→</span></a>
<div class='assist'>
  <div class='a-head'><div><h3>Ton assistant voyage</h3>
    <span class='a-sub'>Propulsé par l'IA · réponses générales, sans prix</span></div>{reset}</div>
  <div class='a-log' id='aLog' aria-live='polite'>{msgs}</div>
  {chips}
  <form class='a-form' id='aForm' autocomplete='off'>
    <textarea id='aIn' rows='1' maxlength='{_MAX_LEN}' placeholder='Écris ta question…'
      aria-label='Ta question'></textarea>
    <button class='btn a-send' type='submit' id='aSend' aria-label='Envoyer'>Envoyer</button>
  </form>
</div>
<script>
(function(){{
 var log=document.getElementById('aLog'),f=document.getElementById('aForm'),
     inp=document.getElementById('aIn'),send=document.getElementById('aSend'),
     chips=document.getElementById('aChips'),rst=document.getElementById('aReset');
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
   if(chips)chips.remove();
   add('me',q); inp.value=''; grow();
   send.disabled=true; inp.disabled=true;
   var out=add('ai typing','');
   fetch('/portail/assistant',{{method:'POST',headers:{{'Content-Type':'application/json'}},
     body:JSON.stringify({{message:q}})}}).then(function(r){{
     if(!r.ok||!r.body){{return r.text().then(function(t){{throw new Error(t);}});}}
     var rd=r.body.getReader(),dec=new TextDecoder(),first=true;
     function pump(){{return rd.read().then(function(x){{
       if(x.done)return;
       if(first){{out.classList.remove('typing');first=false;}}
       out.textContent+=dec.decode(x.value,{{stream:true}});bottom();return pump();}});}}
     return pump();
   }}).catch(function(e){{
     out.classList.remove('typing');
     out.textContent=(e&&e.message&&e.message.length<300)?e.message:
       "Oups, je n'ai pas pu répondre. Réessaie dans un instant.";
   }}).finally(function(){{send.disabled=false;inp.disabled=false;inp.focus();}});
 }}
 f.addEventListener('submit',function(e){{e.preventDefault();ask(inp.value);}});
 if(chips)chips.addEventListener('click',function(e){{
   var b=e.target.closest('.chip');if(b)ask(b.textContent);}});
 if(rst)rst.addEventListener('click',function(){{
   fetch('/portail/assistant/reset',{{method:'POST'}}).then(function(){{location.reload();}});}});
}})();
</script>"""


# --------------------------------------------------------------------------- #
# Chat endpoint (streamed)
# --------------------------------------------------------------------------- #
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

    msgs = [{"role": m["role"], "content": m["text"]}
            for m in history[-_CONTEXT:] if m.get("text")]
    # The API needs the thread to start with a user turn and alternate.
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    msgs.append({"role": "user", "content": q})
    system = SYSTEM_PROMPT + (f"\nLe membre s'appelle {name}." if name else "")

    def gen():
        parts: list[str] = []
        try:
            api = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            with api.messages.stream(model=settings.PORTAL_ASSISTANT_MODEL,
                                     max_tokens=600, system=system,
                                     messages=msgs) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
                    yield chunk
        except Exception as exc:  # noqa: BLE001 — never break the page
            log.warning("assistant stream failed: %s", exc)
            if not parts:
                yield "Oups, je n'ai pas pu répondre. Réessaie dans un instant."
            return
        answer = "".join(parts).strip()
        if not answer:
            return
        now = datetime.utcnow().isoformat(timespec="seconds")
        with SessionLocal() as db2:
            c = db2.get(Client, cid)
            if c:
                c.assistant_chat = (list(c.assistant_chat or []) + [
                    {"role": "user", "text": q, "at": now},
                    {"role": "assistant", "text": answer, "at": now},
                ])[-_KEEP:]
                db2.commit()

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


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
