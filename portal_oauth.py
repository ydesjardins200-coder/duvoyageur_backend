"""
portal_oauth.py
===============
"Continuer avec Google / Facebook" for the client portal (espace client).

Standard OAuth 2.0 authorization-code flow, server side, no extra dependency:

1. /portail/auth/{provider}            -> random `state` in a short-lived signed
                                          cookie, redirect to the provider.
2. /portail/auth/{provider}/callback   -> check `state`, swap the code for a
                                          token, read id / email / name.
3. Find or create the Client, then set the normal portal session cookie.

Matching order: the provider account id (kind="google"/"facebook" identity),
then the email the provider returns. Google only gives us emails it has
verified (email_verified), and Facebook only returns confirmed emails, so
linking an existing account by email is safe. Neither provider gives a date of
birth without special review; the portal shows a soft "complete your profile"
banner instead of gating on it.
"""
from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from config import settings
from db import Client, SessionLocal, add_identity, find_client_by_identity, log_activity, normalize_email
from portal import _info_page, _set_session_cookie

router = APIRouter()
log = logging.getLogger("duvoyageur.portal.oauth")

_STATE_COOKIE = "dv_oauth_state"
_STATE_PATH = "/portail/auth"
_STATE_MAX_AGE = 600                     # 10 minutes to complete the round-trip
_state_signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dv-portal-oauth")

_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_USER = "https://openidconnect.googleapis.com/v1/userinfo"


def _fb(path: str) -> str:
    return f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/{path}"


def _enabled(provider: str) -> bool:
    if provider == "google":
        return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)
    if provider == "facebook":
        return bool(settings.FB_LOGIN_APP_ID and settings.FB_LOGIN_APP_SECRET)
    return False


def _redirect_uri(provider: str) -> str:
    return f"{settings.PUBLIC_BASE_URL}/portail/auth/{provider}/callback"


def _http_json(url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _fail(msg: str = "La connexion n'a pas pu être complétée. Réessaie, ou "
                     "crée ton compte avec ton courriel."):
    resp = HTMLResponse(_info_page("Connexion impossible", msg + (
        "<br><br><a class='btn' href='/portail/connexion'>Retour à la connexion</a>")),
        status_code=400)
    resp.delete_cookie(_STATE_COOKIE, path=_STATE_PATH)
    return resp


# --------------------------------------------------------------------------- #
# Step 1 — send the visitor to the provider
# --------------------------------------------------------------------------- #
@router.get("/portail/auth/{provider}")
def oauth_start(provider: str):
    if not _enabled(provider):
        return RedirectResponse("/portail/connexion", status_code=303)
    state = secrets.token_urlsafe(24)
    if provider == "google":
        url = _GOOGLE_AUTH + "?" + urllib.parse.urlencode({
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": _redirect_uri("google"),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        })
    else:
        url = (f"https://www.facebook.com/{settings.FB_GRAPH_VERSION}/dialog/oauth?"
               + urllib.parse.urlencode({
                   "client_id": settings.FB_LOGIN_APP_ID,
                   "redirect_uri": _redirect_uri("facebook"),
                   "response_type": "code",
                   "scope": "email,public_profile",
                   "state": state,
               }))
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(_STATE_COOKIE, _state_signer.dumps({"s": state, "p": provider}),
                    max_age=_STATE_MAX_AGE, path=_STATE_PATH, httponly=True,
                    samesite="lax", secure=settings.SECURE_COOKIES)
    return resp


# --------------------------------------------------------------------------- #
# Step 2 — provider sends the visitor back
# --------------------------------------------------------------------------- #
def _google_profile(code: str) -> dict | None:
    tok = _http_json(_GOOGLE_TOKEN, {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": _redirect_uri("google"),
        "grant_type": "authorization_code",
    })
    info = _http_json(_GOOGLE_USER, headers={
        "Authorization": f"Bearer {tok['access_token']}"})
    return {
        "id": info.get("sub"),
        "email": info.get("email") if info.get("email_verified") else None,
        "first": info.get("given_name") or "",
        "last": info.get("family_name") or "",
    }


def _facebook_profile(code: str) -> dict | None:
    tok = _http_json(_fb("oauth/access_token") + "?" + urllib.parse.urlencode({
        "client_id": settings.FB_LOGIN_APP_ID,
        "client_secret": settings.FB_LOGIN_APP_SECRET,
        "redirect_uri": _redirect_uri("facebook"),
        "code": code,
    }))
    info = _http_json(_fb("me") + "?" + urllib.parse.urlencode({
        "fields": "id,email,first_name,last_name",
        "access_token": tok["access_token"],
    }))
    return {
        "id": info.get("id"),
        "email": info.get("email"),          # FB only returns confirmed emails
        "first": info.get("first_name") or "",
        "last": info.get("last_name") or "",
    }


def _find_or_create(provider: str, prof: dict) -> int:
    email = normalize_email(prof.get("email"))
    with SessionLocal() as db:
        client = find_client_by_identity(db, provider, prof["id"])
        if not client and email:
            client = (db.query(Client).filter(Client.primary_email == email).first()
                      or find_client_by_identity(db, "email", email))
        label = "Google" if provider == "google" else "Facebook"
        if client:
            kyc = dict(client.kyc or {})
            # Fill only what's missing — never overwrite a name the client typed.
            if not kyc.get("legal_first_name") and prof["first"]:
                kyc["legal_first_name"] = prof["first"]
            if not kyc.get("legal_last_name") and prof["last"]:
                kyc["legal_last_name"] = prof["last"]
            client.kyc = kyc
            if email and not client.primary_email:
                client.primary_email = email
            if not client.display_name:
                client.display_name = " ".join(
                    x for x in (kyc.get("legal_first_name"), kyc.get("legal_last_name")) if x) or None
            add_identity(db, client, provider, prof["id"])
            add_identity(db, client, "email", email)
            log_activity(db, client.id, "note", f"Connexion espace client via {label}")
        else:
            name = " ".join(x for x in (prof["first"], prof["last"]) if x) or None
            client = Client(display_name=name, primary_email=email,
                            preferred_channel="email",
                            kyc={"legal_first_name": prof["first"] or None,
                                 "legal_last_name": prof["last"] or None})
            db.add(client)
            db.flush()
            add_identity(db, client, provider, prof["id"])
            add_identity(db, client, "email", email)
            log_activity(db, client.id, "request_created",
                         f"Compte créé via {label} (espace client)")
        db.commit()
        return client.id


@router.get("/portail/auth/{provider}/callback")
def oauth_callback(provider: str, request: Request, code: str = "", state: str = "",
                   error: str = ""):
    if not _enabled(provider):
        return RedirectResponse("/portail/connexion", status_code=303)
    if error:                                   # visitor cancelled on the provider
        resp = RedirectResponse("/portail/connexion", status_code=303)
        resp.delete_cookie(_STATE_COOKIE, path=_STATE_PATH)
        return resp
    try:
        saved = _state_signer.loads(request.cookies.get(_STATE_COOKIE) or "",
                                    max_age=_STATE_MAX_AGE)
    except BadSignature:                        # includes SignatureExpired
        return _fail()
    if (not code or saved.get("p") != provider
            or not secrets.compare_digest(saved.get("s", ""), state)):
        return _fail()
    try:
        prof = _google_profile(code) if provider == "google" else _facebook_profile(code)
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        log.warning("oauth %s exchange failed: %s", provider, exc)
        return _fail()
    if not prof or not prof.get("id"):
        return _fail()
    if not prof.get("email"):
        return _fail("Ton compte ne nous a pas transmis de courriel vérifié. "
                     "Crée plutôt ton compte avec ton courriel.")
    cid = _find_or_create(provider, prof)
    resp = RedirectResponse("/portail", status_code=303)
    resp.delete_cookie(_STATE_COOKIE, path=_STATE_PATH)
    _set_session_cookie(resp, cid)
    return resp
