"""Passkeys (WebAuthn) for the client portal: Face ID / Touch ID / fingerprint
login. Members enable it from « Mon profil »; the login page then offers a
one-tap sign-in. Only public keys are stored; the existing e-mail + date of
birth login stays available as a fallback.

Passkeys are bound to the domain (RP ID). They are derived from the request
host, so after moving to duvoyageur.ca members re-enable them once.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from html import escape
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from config import settings
from db import Client, PasskeyCredential, SessionLocal
from i18n_portal import L
from portal import _set_session_cookie, current_portal_client_id

router = APIRouter()

RP_NAME = "duvoyageur.ca"
CHALLENGE_COOKIE = "dv_wa"
CHALLENGE_TTL = 300
_signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dv-passkey-challenge")
_LOGIN_HITS: dict = {}


def _origin(request: Request) -> tuple[str, str]:
    """(rp_id, expected_origin) from the request as the browser sees it."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    rp_id = host.split(":")[0]
    return rp_id, f"{proto}://{host}"


def _set_challenge(resp, kind: str, challenge: bytes, cid: int | None = None) -> None:
    resp.set_cookie(CHALLENGE_COOKIE, _signer.dumps({"k": kind, "c": bytes_to_base64url(challenge), "cid": cid}),
                    max_age=CHALLENGE_TTL, path="/portail/passkey", httponly=True, samesite="strict",
                    secure=settings.SECURE_COOKIES)


def _get_challenge(request: Request, kind: str) -> dict | None:
    raw = request.cookies.get(CHALLENGE_COOKIE)
    if not raw:
        return None
    try:
        data = _signer.loads(raw, max_age=CHALLENGE_TTL)
    except BadSignature:
        return None
    return data if data.get("k") == kind else None


def _device_label(ua: str) -> str:
    u = ua.lower()
    if "iphone" in u:
        return "iPhone"
    if "ipad" in u:
        return "iPad"
    if "android" in u:
        return "Android"
    if "macintosh" in u or "mac os" in u:
        return "Mac"
    if "windows" in u:
        return "Windows"
    return L("Appareil", "Device")


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


# --------------------------------------------------------------------------- #
# Registration (logged-in member, from « Mon profil »)
# --------------------------------------------------------------------------- #
@router.post("/portail/passkey/register/options")
def register_options(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return _err(L("Connecte-toi d'abord.", "Please log in first."), 401)
    rp_id, _ = _origin(request)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return _err(L("Compte introuvable.", "Account not found."), 404)
        existing = db.query(PasskeyCredential).filter(PasskeyCredential.client_id == cid).all()
        name = client.primary_email or f"membre-{cid}"
        display = client.display_name or name
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=f"dv-{cid}".encode(), user_name=name, user_display_name=display,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing],
        timeout=120000)
    resp = JSONResponse(json.loads(options_to_json(opts)))
    _set_challenge(resp, "reg", opts.challenge, cid)
    return resp


@router.post("/portail/passkey/register/verify")
async def register_verify(request: Request):
    cid = current_portal_client_id(request)
    ch = _get_challenge(request, "reg")
    if not cid or not ch or ch.get("cid") != cid:
        return _err(L("La demande a expiré. Réessaie.", "The request expired. Please try again."))
    rp_id, origin = _origin(request)
    body = await request.body()
    try:
        v = verify_registration_response(credential=body.decode(), expected_challenge=base64url_to_bytes(ch["c"]),
                                         expected_rp_id=rp_id, expected_origin=origin,
                                         require_user_verification=True)
    except Exception:  # noqa: BLE001
        return _err(L("Impossible d'activer la connexion rapide sur cet appareil.",
                      "Couldn't enable quick login on this device."))
    try:
        transports = json.loads(body).get("response", {}).get("transports") or []
    except ValueError:
        transports = []
    with SessionLocal() as db:
        cred_id = bytes_to_base64url(v.credential_id)
        if not db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == cred_id).first():
            db.add(PasskeyCredential(client_id=cid, credential_id=cred_id,
                                     public_key=bytes_to_base64url(v.credential_public_key),
                                     sign_count=v.sign_count, transports=transports[:8],
                                     label=_device_label(request.headers.get("user-agent", ""))))
            db.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(CHALLENGE_COOKIE, path="/portail/passkey")
    return resp


@router.post("/portail/passkey/{pid}/retirer")
def remove_passkey(request: Request, pid: int):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        db.query(PasskeyCredential).filter(PasskeyCredential.id == pid, PasskeyCredential.client_id == cid
                                           ).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse("/portail/profil?passkey=retire", status_code=303)


# --------------------------------------------------------------------------- #
# Login (no session)
# --------------------------------------------------------------------------- #
@router.post("/portail/passkey/login/options")
def login_options(request: Request):
    rp_id, _ = _origin(request)
    opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
                                           timeout=120000)
    resp = JSONResponse(json.loads(options_to_json(opts)))
    _set_challenge(resp, "auth", opts.challenge)
    return resp


@router.post("/portail/passkey/login/verify")
async def login_verify(request: Request):
    ip = request.client.host if request.client else "?"
    now = datetime.utcnow().timestamp()
    hits = [t for t in _LOGIN_HITS.get(ip, []) if now - t < 600]
    if len(hits) >= 20:
        return _err(L("Trop de tentatives. Réessaie dans quelques minutes.", "Too many attempts. Try again in a few minutes."), 429)
    ch = _get_challenge(request, "auth")
    if not ch:
        return _err(L("La demande a expiré. Réessaie.", "The request expired. Please try again."))
    body = await request.body()
    try:
        cred_id = json.loads(body).get("id") or ""
    except ValueError:
        cred_id = ""
    rp_id, origin = _origin(request)
    with SessionLocal() as db:
        pk = db.query(PasskeyCredential).filter(PasskeyCredential.credential_id == cred_id).first()
        if not pk or not db.get(Client, pk.client_id):
            _LOGIN_HITS[ip] = hits + [now]
            return _err(L("Cet appareil n'est pas reconnu. Connecte-toi avec ton courriel, puis active la connexion "
                          "rapide dans Mon profil.",
                          "This device isn't recognized. Log in with your email, then enable quick login in My profile."))
        try:
            v = verify_authentication_response(credential=body.decode(), expected_challenge=base64url_to_bytes(ch["c"]),
                                               expected_rp_id=rp_id, expected_origin=origin,
                                               credential_public_key=base64url_to_bytes(pk.public_key),
                                               credential_current_sign_count=pk.sign_count,
                                               require_user_verification=True)
        except Exception:  # noqa: BLE001
            _LOGIN_HITS[ip] = hits + [now]
            return _err(L("La vérification a échoué. Réessaie.", "Verification failed. Please try again."))
        pk.sign_count = v.new_sign_count
        pk.last_used_at = datetime.utcnow()
        cid = pk.client_id
        db.commit()
    resp = JSONResponse({"ok": True, "redirect": "/portail"})
    resp.delete_cookie(CHALLENGE_COOKIE, path="/portail/passkey")
    _set_session_cookie(resp, cid)
    return resp


# --------------------------------------------------------------------------- #
# UI fragments
# --------------------------------------------------------------------------- #
_JS = """<script>
window.dvPk=(function(){
 function b2u(b){var s='',a=new Uint8Array(b);for(var i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'')}
 function u2b(u){u=u.replace(/-/g,'+').replace(/_/g,'/');while(u.length%4)u+='=';var s=atob(u),a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a.buffer}
 function post(url,body){return fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):'{}'}).then(function(r){return r.json()})}
 function supported(){return !!(window.PublicKeyCredential&&navigator.credentials)}
 function register(){return post('/portail/passkey/register/options').then(function(o){
   if(o.ok===false)throw new Error(o.error);
   o.challenge=u2b(o.challenge);o.user.id=u2b(o.user.id);(o.excludeCredentials||[]).forEach(function(c){c.id=u2b(c.id)});
   return navigator.credentials.create({publicKey:o})}).then(function(c){
   return post('/portail/passkey/register/verify',{id:c.id,rawId:b2u(c.rawId),type:c.type,
     response:{clientDataJSON:b2u(c.response.clientDataJSON),attestationObject:b2u(c.response.attestationObject),
     transports:c.response.getTransports?c.response.getTransports():[]},clientExtensionResults:c.getClientExtensionResults?c.getClientExtensionResults():{}})})}
 function login(){return post('/portail/passkey/login/options').then(function(o){
   o.challenge=u2b(o.challenge);(o.allowCredentials||[]).forEach(function(c){c.id=u2b(c.id)});
   return navigator.credentials.get({publicKey:o})}).then(function(c){
   return post('/portail/passkey/login/verify',{id:c.id,rawId:b2u(c.rawId),type:c.type,
     response:{clientDataJSON:b2u(c.response.clientDataJSON),authenticatorData:b2u(c.response.authenticatorData),
     signature:b2u(c.response.signature),userHandle:c.response.userHandle?b2u(c.response.userHandle):null},
     clientExtensionResults:c.getClientExtensionResults?c.getClientExtensionResults():{}})})}
 function label(){var u=navigator.userAgent;return /iphone|ipad/i.test(u)?'Face ID':(/android/i.test(u)?'@@FP@@':(/mac/i.test(u)?'Touch ID':'@@PK@@'))}
 return {supported:supported,register:register,login:login,label:label};
})();
</script>"""


def _js() -> str:
    return (_JS.replace("@@FP@@", L("ton empreinte", "your fingerprint"))
               .replace("@@PK@@", L("ta clé d’accès", "your passkey")))


_CSS = """<style>
.pk-btn{display:flex;align-items:center;justify-content:center;gap:.55rem;width:100%;border:0;background:var(--navy);color:#fff;
 border-radius:99px;padding:.85rem 1rem;font:800 .95rem Montserrat,sans-serif;cursor:pointer;text-transform:none;letter-spacing:0;box-shadow:none}
.pk-btn svg{width:22px;height:22px;flex:none}
.pk-or{display:flex;align-items:center;gap:.7rem;margin:1rem 0;color:var(--mist);font:500 .8rem Inter,sans-serif}
.pk-or::before,.pk-or::after{content:"";flex:1;height:1px;background:var(--line)}
.pk-msg{margin:.6rem 0 0;font:500 .85rem/1.4 Inter,sans-serif;color:#9B1C1C;text-align:center}
.pk-card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:1.1rem 1.2rem;margin:0 0 1.2rem;box-shadow:var(--shadow)}
.pk-card h3{margin:0 0 .3rem;font:800 1.05rem Montserrat,sans-serif;color:var(--navy);display:flex;align-items:center;gap:.5rem}
.pk-card p{margin:0 0 .9rem;color:var(--mist);font:400 .9rem/1.5 Inter,sans-serif}
.pk-list{list-style:none;margin:0 0 .9rem;padding:0;display:grid;gap:.45rem}
.pk-list li{display:flex;align-items:center;justify-content:space-between;gap:.6rem;background:var(--warm);border-radius:12px;padding:.55rem .8rem;font:500 .88rem Inter,sans-serif;color:var(--navy)}
.pk-list small{display:block;color:var(--mist);font-size:.76rem}
.pk-list form{margin:0}.pk-list button{background:none;border:1px solid rgba(15,42,71,.2);border-radius:99px;padding:.3rem .7rem;font:600 .78rem Inter,sans-serif;color:var(--mist);cursor:pointer;width:auto;box-shadow:none;text-transform:none;letter-spacing:0}
.pk-card .pk-btn{width:auto;display:inline-flex;padding:.7rem 1.2rem}
</style>"""

_FACE = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
         'stroke-linejoin="round" aria-hidden="true"><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 '
         '2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><path d="M9 9v1M15 9v1M12 9v4h-1M9 16c1.7 1.3 4.3 1.3 6 0"/></svg>')


def login_block() -> str:
    """« Se connecter avec Face ID » above the e-mail form (hidden if unsupported)."""
    return (_CSS + _js() + f"<div id='pkLogin' hidden><button class='pk-btn' id='pkLoginBtn' type='button'>{_FACE}"
            f"<span>{L('Se connecter avec', 'Log in with')} <span class='pk-l'>Face ID</span></span></button>"
            "<p class='pk-msg' id='pkLoginMsg' hidden></p>"
            f"<div class='pk-or'>{L('ou avec ton courriel', 'or with your email')}</div></div>"
            "<script>(function(){if(!dvPk.supported())return;var w=document.getElementById('pkLogin');w.hidden=false;"
            "w.querySelector('.pk-l').textContent=dvPk.label();var m=document.getElementById('pkLoginMsg');"
            "document.getElementById('pkLoginBtn').onclick=function(){m.hidden=true;dvPk.login().then(function(r){"
            "if(r&&r.ok){location.href=r.redirect||'/portail'}else{m.textContent=(r&&r.error)||'';m.hidden=false}})"
            ".catch(function(e){if(e&&e.name==='NotAllowedError')return;m.textContent=" + json.dumps(
                L("La connexion rapide n'a pas fonctionné. Utilise ton courriel.", "Quick login didn't work. Use your email.")) +
            ";m.hidden=false})}})();</script>")


def profile_card(client_id: int, flash: str = "") -> str:
    with SessionLocal() as db:
        creds = (db.query(PasskeyCredential).filter(PasskeyCredential.client_id == client_id)
                 .order_by(PasskeyCredential.created_at.desc()).all())
    items = "".join(
        f"<li><span>{escape(c.label or L('Appareil', 'Device'))}<small>{L('Activée le', 'Enabled on')} {c.created_at:%Y-%m-%d}"
        + (f" · {L('dernière utilisation', 'last used')} {c.last_used_at:%Y-%m-%d}" if c.last_used_at else "")
        + f"</small></span><form method='post' action='/portail/passkey/{c.id}/retirer'>"
          f"<button type='submit'>{L('Retirer', 'Remove')}</button></form></li>"
        for c in creds)
    note = f"<div class='note ok'>{escape(flash)}</div>" if flash else ""
    return (_CSS + _js() + note +
            f"<div class='pk-card' id='pkCard'><h3>{_FACE.replace('<svg ', '<svg width=22 height=22 ')} "
            f"<span>{L('Connexion rapide avec', 'Quick login with')} <span class='pk-l'>Face ID</span></span></h3>"
            f"<p>{L('Connecte-toi en un regard ou une empreinte, sans courriel ni date de naissance. Ton visage et tes empreintes restent dans ton téléphone : on ne les reçoit jamais.', 'Log in with a glance or a fingerprint, no email or date of birth needed. Your face and fingerprints stay on your phone: we never receive them.')}</p>"
            + (f"<ul class='pk-list'>{items}</ul>" if items else "")
            + f"<button class='pk-btn' id='pkRegBtn' type='button'>{_FACE}<span>{L('Activer sur cet appareil', 'Enable on this device')}</span></button>"
            "<p class='pk-msg' id='pkRegMsg' hidden></p>"
            f"<p id='pkUnsup' hidden style='margin:.6rem 0 0'>{L('Cet appareil ou ce navigateur ne permet pas la connexion rapide.', 'This device or browser does not support quick login.')}</p></div>"
            "<script>(function(){var b=document.getElementById('pkRegBtn'),m=document.getElementById('pkRegMsg');"
            "document.querySelectorAll('#pkCard .pk-l').forEach(function(e){e.textContent=dvPk.label()});"
            "if(!dvPk.supported()){b.hidden=true;document.getElementById('pkUnsup').hidden=false;return}"
            "b.onclick=function(){m.hidden=true;b.disabled=true;dvPk.register().then(function(r){"
            "if(r&&r.ok){location.href='/portail/profil?passkey=ok'}else{m.textContent=(r&&r.error)||'';m.hidden=false;b.disabled=false}})"
            ".catch(function(e){b.disabled=false;if(e&&e.name==='NotAllowedError')return;"
            "if(e&&e.name==='InvalidStateError'){m.textContent=" + json.dumps(L("La connexion rapide est déjà activée sur cet appareil.", "Quick login is already enabled on this device.")) + ";m.hidden=false;return}"
            "m.textContent=(e&&e.message)||'';m.hidden=false})}})();</script>")
