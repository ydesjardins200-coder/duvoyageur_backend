"""
portal.py
=========
Client-facing self-service portal ("espace client") with passwordless,
single-use magic-link auth — completely separate from the admin session.

How auth works
--------------
1. An agent (or an automated notification) issues a magic link for a client:
   `build_portal_login_url(client_id)` sets a fresh single-use nonce on the
   client and returns an absolute URL like
   `{PUBLIC_BASE_URL}/portail/login?token=<signed>`.
2. The client opens the link. `/portail/login` verifies the signed token
   (HMAC over SECRET_KEY, time-limited) AND that its nonce still matches the
   client's stored nonce. On success it clears the nonce (so the link can't be
   reused) and sets a signed `dv_portal` cookie scoped to `/portail`.
3. `/portail` reads that cookie and shows the client ONLY their own,
   client-safe data (no confidence scores, internal notes, or raw parser data).

Security notes
--------------
- The portal cookie is a DIFFERENT cookie (`dv_portal`, path=`/portail`) signed
  with a DIFFERENT salt than anything admin-related. A portal visitor can never
  reach `require_admin`, which checks the Starlette session's `admin` flag.
- Links are single-use (nonce) and short-lived (PORTAL_LINK_MAX_AGE). Issuing a
  new link overwrites the nonce, so only the most recent link works.
"""
from __future__ import annotations

import secrets
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings
from db import Case, Client, SessionLocal, add_identity, log_activity, normalize_email, normalize_phone

router = APIRouter()

PORTAL_COOKIE = "dv_portal"
_COOKIE_PATH = "/portail"

_login_signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dv-portal-login")
_session_signer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dv-portal-session")


# --------------------------------------------------------------------------- #
# Tokens & session
# --------------------------------------------------------------------------- #
def _issue_login_token(client_id: int, nonce: str) -> str:
    return _login_signer.dumps({"cid": client_id, "n": nonce})


def _read_login_token(token: str):
    """Return {'cid', 'n'} for a valid, unexpired token, else None."""
    try:
        return _login_signer.loads(token, max_age=settings.PORTAL_LINK_MAX_AGE)
    except (BadSignature, SignatureExpired, Exception):  # noqa: BLE001
        return None


def _make_session_value(client_id: int) -> str:
    return _session_signer.dumps({"cid": client_id})


def _set_session_cookie(resp, client_id: int) -> None:
    """Set/refresh the signed portal cookie (sliding expiry: each visit renews
    it, so an active client effectively stays logged in)."""
    resp.set_cookie(
        PORTAL_COOKIE, _make_session_value(client_id),
        max_age=settings.PORTAL_SESSION_MAX_AGE, path=_COOKIE_PATH,
        httponly=True, samesite="lax", secure=settings.SECURE_COOKIES)


def current_portal_client_id(request: Request):
    """Client id from the signed portal cookie, or None if absent/invalid."""
    raw = request.cookies.get(PORTAL_COOKIE)
    if not raw:
        return None
    try:
        data = _session_signer.loads(raw, max_age=settings.PORTAL_SESSION_MAX_AGE)
        return int(data["cid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, Exception):  # noqa: BLE001
        return None


def build_portal_login_url(client_id: int):
    """Set a fresh single-use nonce on the client and return an absolute magic
    link, or None if the client doesn't exist."""
    with SessionLocal() as db:
        client = db.get(Client, client_id)
        if not client:
            return None
        nonce = secrets.token_urlsafe(16)
        client.portal_nonce = nonce
        db.commit()
        token = _issue_login_token(client_id, nonce)
    return f"{settings.PUBLIC_BASE_URL}/portail/login?token={token}"


# --------------------------------------------------------------------------- #
# Client-safe view model
# --------------------------------------------------------------------------- #
# Friendly status: (label, css-class). Internal statuses collapse to client view.
_STATUS_FR = {
    "new": ("En préparation", "prep"),
    "needs_info": ("En préparation", "prep"),
    "quoted": ("Soumission prête", "quote"),
    "booked": ("Réservé", "booked"),
    "closed": ("Voyage terminé", "done"),
}


def _trip_where(t: dict) -> str:
    return str(t.get("hotel_name_raw") or t.get("destination") or "Ton voyage")


def _trip_dates(c, t: dict):
    dep = t.get("departure_date") or c.flight_depart
    ret = t.get("return_date") or c.flight_return
    if dep or ret:
        return f"{dep or '?'} → {ret or '?'}"
    return t.get("dates_raw")


def _trip_travelers(t: dict):
    a, k = t.get("num_adults"), t.get("num_children")
    bits = []
    if a:
        bits.append(f"{a} adulte" + ("s" if a > 1 else ""))
    if k:
        bits.append(f"{k} enfant" + ("s" if k > 1 else ""))
    return ", ".join(bits) if bits else None


def _trip_card(c) -> str:
    t = c.trip or {}
    label, cls = _STATUS_FR.get(c.status, ("En cours", "prep"))
    where = escape(_trip_where(t))
    rows = []
    if t.get("hotel_name_raw") and t.get("destination"):
        rows.append(("Destination", escape(str(t.get("destination")))))
    dates = _trip_dates(c, t)
    if dates:
        rows.append(("Dates", escape(str(dates))))
    pax = _trip_travelers(t)
    if pax:
        rows.append(("Voyageurs", escape(pax)))
    meta = "".join(
        f"<div class='row'><span class='k'>{k}</span><span class='v'>{v}</span></div>"
        for k, v in rows)

    action = ""
    if c.status == "quoted":
        eco = (f"<div class='eco'>Tu économises <b>{escape(c.savings)}</b> 💸</div>"
               if c.savings else "")
        btn = (f"<a class='btn' href=\"{escape(c.quote_url)}\" target='_blank' "
               "rel='noopener'>Voir ma soumission →</a>" if c.quote_url else "")
        action = f"{eco}{btn}"
    elif c.status == "booked":
        ref = (f"<div class='row'><span class='k'>Confirmation</span>"
               f"<span class='v'>{escape(c.booking_ref)}</span></div>"
               if c.booking_ref else "")
        eco = (f"<div class='eco'>Économie réalisée : <b>{escape(c.savings)}</b> 🎉</div>"
               if c.savings else "")
        action = f"{ref}{eco}<div class='muted'>Confirmation détaillée envoyée par Tripbook. Bon voyage ! 🌴</div>"
    elif c.status == "closed":
        action = "<div class='muted'>Voyage terminé — merci de voyager avec nous ! 🙌</div>"
    else:  # new / needs_info
        action = "<div class='muted'>On prépare ta meilleure offre — on te revient très vite. ✈️</div>"

    return (
        "<div class='tcard'>"
        f"<div class='tchdr'><h3>{where}</h3><span class='badge {cls}'>{label}</span></div>"
        f"{meta}{action}"
        "</div>")


def _dashboard(client, cases) -> str:
    name = escape(client.display_name or "")
    hello = f"Bonjour {name} 👋" if name else "Bonjour 👋"
    trips = [c for c in cases if (c.kind or "trip") == "trip"]
    if trips:
        cards = "".join(_trip_card(c) for c in trips)
    else:
        cards = ("<div class='tcard empty'>Aucune demande pour l'instant. "
                 "Écris-nous sur Messenger pour trouver ton prochain voyage à rabais ! 🌴</div>")
    return (
        f"<div class='hello'><h2>{hello}</h2>"
        "<p class='lede'>Voici tes demandes de voyage et leurs soumissions.</p></div>"
        + _kyc_banner(client)
        + f"<div class='tgrid'>{cards}</div>")


# --------------------------------------------------------------------------- #
# Identity / KYC
# --------------------------------------------------------------------------- #
# (key, label, input type, required, half-width). email/phone live on the
# Client; everything else is stored in the `kyc` JSON blob.
_KYC_FIELDS = [
    ("legal_first_name", "Prénom légal", "text", True, True),
    ("legal_last_name", "Nom légal", "text", True, True),
    ("date_of_birth", "Date de naissance", "date", True, True),
    ("phone", "Téléphone", "tel", True, True),
    ("email", "Courriel", "email", True, False),
    ("address", "Adresse", "text", True, False),
    ("city", "Ville", "text", True, True),
    ("province", "Province / État", "text", True, True),
    ("postal_code", "Code postal", "text", True, True),
    ("country", "Pays", "text", True, True),
    ("passport_number", "N° de passeport", "text", False, True),
    ("passport_expiry", "Expiration du passeport", "date", False, True),
]


def _kyc_value(client, key):
    if key == "email":
        return client.primary_email
    if key == "phone":
        return client.primary_phone
    return (client.kyc or {}).get(key)


def kyc_status(client):
    """(filled, total, [missing labels]) over the required identity fields."""
    req = [f for f in _KYC_FIELDS if f[3]]
    missing = [label for (k, label, _t, _r, _h) in req if not _kyc_value(client, k)]
    return len(req) - len(missing), len(req), missing


def _kyc_banner(client) -> str:
    done, total, missing = kyc_status(client)
    if not missing:
        return ""
    n = len(missing)
    return (
        "<a class='banner' href='/portail/profil'>"
        "<div class='banner-txt'><b>Complète ton identité</b>"
        f"<span>{n} champ{'s' if n > 1 else ''} à remplir pour qu'on puisse réserver tes voyages.</span></div>"
        "<span class='banner-cta'>Compléter →</span></a>")


def _profile_form(client, saved: bool = False) -> str:
    done, total, _missing = kyc_status(client)
    pct = int(done / total * 100) if total else 100
    ok = "<div class='note ok'>Profil enregistré ✓</div>" if saved else ""
    prog = (f"<div class='prog'><span style='width:{pct}%'></span></div>"
            f"<p class='lede'>{done}/{total} champs essentiels remplis.</p>")

    def field(k, label, typ, req, half):
        val = escape(str(_kyc_value(client, k) or ""))
        miss = req and not _kyc_value(client, k)
        star = " <span class='req'>*</span>" if req else ""
        cls = "field half" if half else "field"
        if miss:
            cls += " miss"
        return (f"<div class='{cls}'><label>{label}{star}</label>"
                f"<input name='{k}' type='{typ}' value=\"{val}\" inputmode='"
                f"{'email' if typ == 'email' else ('tel' if typ == 'tel' else 'text')}'"
                f"{' required' if req else ''}></div>")

    def group(title, keys, sub=""):
        fields = "".join(field(*f) for f in _KYC_FIELDS if f[0] in keys)
        s = f" <span class='muted'>{sub}</span>" if sub else ""
        return (f"<div class='fset'><h3>{title}{s}</h3>"
                f"<div class='formgrid'>{fields}</div></div>")

    return (
        ok + prog
        + "<form class='form' method='post' action='/portail/profil'>"
        + group("Identité", ("legal_first_name", "legal_last_name", "date_of_birth"))
        + group("Coordonnées", ("phone", "email", "address", "city",
                                "province", "postal_code", "country"))
        + group("Passeport", ("passport_number", "passport_expiry"),
                "voyages internationaux")
        + "<div class='actions'>"
          "<button class='btn block' type='submit'>Enregistrer mon profil</button>"
          "<a class='btn ghost' href='/portail'>Retour</a></div>"
          "</form>")


# --------------------------------------------------------------------------- #
# Shell + nav
# --------------------------------------------------------------------------- #
def _nav(active: str) -> str:
    items = [("voyages", "/portail", "Mes voyages"),
             ("profil", "/portail/profil", "Mon profil")]
    links = "".join(
        f"<a class='pill{' on' if k == active else ''}' href='{href}'>{label}</a>"
        for k, href, label in items)
    return f"<nav class='pnav'>{links}</nav>"


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def _shell(title: str, body: str, logged_in: bool = False, nav: str = "") -> str:
    logout = ("<a class='logout' href='/portail/logout'>Déconnexion</a>"
              if logged_in else "")
    return _PORTAL_PAGE.format(title=escape(title), body=body, logout=logout, nav=nav)


def _info_page(title: str, message: str, logged_in: bool = False) -> str:
    body = (f"<div class='infobox'><h2>{escape(title)}</h2>"
            f"<p>{message}</p></div>")
    return _shell(title, body, logged_in)


def _confirm_page(token: str, name) -> str:
    """Interstitial shown on the GET — a human clicks the button to POST and log
    in. Link-preview crawlers do GET only, so they never consume the token."""
    hello = f"Bonjour {escape(name)} 👋" if name else "Bienvenue 👋"
    body = (
        "<div class='infobox'>"
        f"<h2>{hello}</h2>"
        "<p>Clique ci-dessous pour ouvrir ton espace client en toute sécurité.</p>"
        "<form method='post' action='/portail/login' style='margin-top:18px'>"
        f"<input type='hidden' name='token' value=\"{escape(token)}\">"
        "<button class='btn' type='submit'>Accéder à mon espace →</button>"
        "</form></div>")
    return _shell("Connexion", body)


def _expired_page():
    return HTMLResponse(_info_page(
        "Lien expiré",
        "Ce lien n'est plus valide (il a peut-être expiré ou déjà été "
        "utilisé). Écris-nous sur Messenger et on t'en renvoie un tout neuf. 🙂"))


def _invalid_page():
    return HTMLResponse(_info_page(
        "Lien invalide",
        "Ce lien n'est plus valide. Écris-nous sur Messenger pour en "
        "recevoir un nouveau. 🙂"))


def _check_token(token: str):
    """Return (client_id, client_name) if the token is signed, unexpired AND its
    nonce still matches the client; else None. Does NOT consume the nonce."""
    data = _read_login_token(token) if token else None
    if not data:
        return None
    cid, nonce = data.get("cid"), data.get("n")
    with SessionLocal() as db:
        client = db.get(Client, cid) if cid else None
        if not client or not client.portal_nonce or not secrets.compare_digest(
                client.portal_nonce, nonce or ""):
            return None
        return cid, client.display_name


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/portail/login", response_class=HTMLResponse)
def portal_login_confirm(request: Request, token: str = ""):
    """GET shows a confirmation page (no consumption) so link-preview bots that
    only fetch the URL can't burn the single-use token before the human clicks."""
    if not token or not _read_login_token(token):
        # Already logged in? Send them straight in. Otherwise it's a dead link.
        if current_portal_client_id(request):
            return RedirectResponse("/portail", status_code=303)
        return _expired_page()
    checked = _check_token(token)
    if not checked:
        if current_portal_client_id(request):
            return RedirectResponse("/portail", status_code=303)
        return _invalid_page()
    _cid, name = checked
    return HTMLResponse(_confirm_page(token, name))


@router.post("/portail/login")
async def portal_login_consume(request: Request):
    """POST consumes the single-use token (only a human clicking the button gets
    here), burns the nonce, and establishes the portal session."""
    form = await request.form()
    token = (form.get("token") or "").strip()
    if not token or not _read_login_token(token):
        return _expired_page()
    cid, nonce = (_read_login_token(token) or {}).get("cid"), (_read_login_token(token) or {}).get("n")
    with SessionLocal() as db:
        client = db.get(Client, cid) if cid else None
        if not client or not client.portal_nonce or not secrets.compare_digest(
                client.portal_nonce, nonce or ""):
            return _invalid_page()
        client.portal_nonce = None          # single use — burn it now
        db.commit()
    resp = RedirectResponse("/portail", status_code=303)
    _set_session_cookie(resp, cid)
    return resp


@router.get("/portail", response_class=HTMLResponse)
def portal_home(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return HTMLResponse(_info_page(
            "Espace client",
            "Pour accéder à ton espace, ouvre le lien personnel qu'on t'a "
            "envoyé. Tu peux aussi le redemander à tout moment sur Messenger : "
            "menu ☰ → « 🔐 Mon espace client ». 🔐"))
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return HTMLResponse(_info_page(
                "Espace client",
                "On n'a pas retrouvé ton dossier. Écris-nous sur Messenger. 🙂"))
        cases = list(client.requests)
        body = _dashboard(client, cases)
    resp = HTMLResponse(_shell("Mon espace", body, logged_in=True, nav=_nav("voyages")))
    _set_session_cookie(resp, cid)          # sliding expiry on every visit
    return resp


@router.get("/portail/profil", response_class=HTMLResponse)
def portal_profile(request: Request, saved: int = 0):
    cid = current_portal_client_id(request)
    if not cid:
        return HTMLResponse(_info_page(
            "Espace client",
            "Pour accéder à ton profil, ouvre ton lien personnel ou redemande-le "
            "sur Messenger (menu ☰ → « 🔐 Mon espace client »). 🔐"))
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return HTMLResponse(_info_page("Espace client", "Dossier introuvable. 🙂"))
        body = ("<div class='hello'><h2>Mon profil</h2>"
                "<p class='lede'>Ces infos nous servent à réserver tes voyages au bon nom.</p></div>"
                + _profile_form(client, saved=bool(saved)))
    resp = HTMLResponse(_shell("Mon profil", body, logged_in=True, nav=_nav("profil")))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/profil")
async def portal_profile_save(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail", status_code=303)
    form = await request.form()
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if client:
            kyc = dict(client.kyc or {})
            for k, _label, _typ, _req, _half in _KYC_FIELDS:
                v = (form.get(k) or "").strip()
                if k == "email":
                    em = normalize_email(v)
                    if em:
                        client.primary_email = em
                        add_identity(db, client, "email", em)
                elif k == "phone":
                    ph = normalize_phone(v)
                    if ph:
                        client.primary_phone = ph
                        add_identity(db, client, "phone", ph)
                else:
                    kyc[k] = v or None
            client.kyc = kyc
            if not client.display_name:
                full = " ".join(x for x in (kyc.get("legal_first_name"),
                                            kyc.get("legal_last_name")) if x)
                if full:
                    client.display_name = full
            log_activity(db, cid, "note", "Identité mise à jour (espace client)", None)
            db.commit()
    return RedirectResponse("/portail/profil?saved=1", status_code=303)


@router.get("/portail/logout")
def portal_logout():
    resp = HTMLResponse(_info_page(
        "À bientôt 👋",
        "Tu es déconnecté de ton espace client.", logged_in=False))
    resp.delete_cookie(PORTAL_COOKIE, path=_COOKIE_PATH)
    return resp


# --------------------------------------------------------------------------- #
# HTML shell template (standalone, client-facing brand — no admin nav)
# --------------------------------------------------------------------------- #
_PORTAL_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#03121b">
<title>Du Voyageur — {title}</title>
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600&display=swap" rel="stylesheet">
<style>
 :root{{
   --abyss:#03121b;--deep:#0a3346;--pacific:#19d3e6;--lagoon:#3df0c5;
   --surf:#9bf6ec;--gold:#ffd23f;--foam:#eafcff;--mist:#94b8c6;
   --line:rgba(155,246,236,.16);--glow:rgba(25,211,230,.55);
   --field:rgba(3,18,27,.55);
 }}
 *{{box-sizing:border-box}}
 html{{-webkit-text-size-adjust:100%}}
 body{{margin:0;min-height:100vh;font-family:"Inter",system-ui,sans-serif;color:var(--foam);
   font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased;
   background:
     radial-gradient(90% 60% at 85% -8%, rgba(25,211,230,.18), transparent 60%),
     linear-gradient(180deg, rgba(3,18,27,.93), rgba(3,18,27,.98)),
     url("/static/login-bg.webp") center/cover fixed no-repeat;}}
 a{{color:var(--pacific)}}
 h1,h2,h3{{font-family:"Bricolage Grotesque",sans-serif;letter-spacing:-.02em}}
 :focus-visible{{outline:2px solid var(--pacific);outline-offset:2px;border-radius:6px}}
 /* Header */
 .top{{display:flex;align-items:center;justify-content:space-between;gap:12px;
   padding:14px 18px;padding-top:max(14px,env(safe-area-inset-top));
   border-bottom:1px solid var(--line);
   background:linear-gradient(180deg, rgba(8,33,47,.72), rgba(8,33,47,.35));
   backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
   position:sticky;top:0;z-index:5}}
 .brand{{display:flex;align-items:center;gap:11px;min-width:0}}
 .brand img{{width:40px;height:40px;border-radius:50%;box-shadow:0 0 0 1px var(--line);flex:none}}
 .brand b{{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:17px;display:block;line-height:1.1}}
 .brand span{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.2em;
   font-size:10px;color:var(--pacific);display:block;margin-top:2px}}
 .logout{{font-size:13px;color:var(--mist);text-decoration:none;white-space:nowrap;
   padding:8px 10px;border-radius:10px}}
 .logout:hover,.logout:active{{color:var(--foam)}}
 /* Pill nav */
 .pnav{{display:flex;gap:8px;overflow-x:auto;padding:12px 18px 0;max-width:980px;margin:0 auto;
   -webkit-overflow-scrolling:touch;scrollbar-width:none}}
 .pnav::-webkit-scrollbar{{display:none}}
 .pill{{flex:none;font-size:14px;font-weight:600;text-decoration:none;color:var(--mist);
   padding:9px 16px;border-radius:999px;border:1px solid var(--line);
   background:rgba(8,33,47,.4)}}
 .pill.on{{color:#02161c;background:linear-gradient(120deg,var(--pacific),var(--lagoon));border-color:transparent}}
 /* Layout */
 .wrap{{max-width:980px;margin:0 auto;padding:22px 18px 64px;
   padding-left:max(18px,env(safe-area-inset-left));padding-right:max(18px,env(safe-area-inset-right))}}
 .hello h2{{font-weight:800;font-size:26px;margin:6px 0 2px}}
 .lede{{color:var(--mist);margin:0 0 20px;font-size:14px}}
 /* Banner */
 .banner{{display:flex;align-items:center;justify-content:space-between;gap:14px;
   text-decoration:none;color:var(--foam);margin:0 0 18px;
   background:linear-gradient(120deg, rgba(255,210,63,.14), rgba(255,210,63,.06));
   border:1px solid rgba(255,210,63,.42);border-radius:16px;padding:15px 18px}}
 .banner-txt b{{display:block;font-family:"Bricolage Grotesque",sans-serif;font-weight:700;margin-bottom:2px}}
 .banner-txt span{{color:var(--mist);font-size:13px}}
 .banner-cta{{color:var(--gold);font-weight:600;white-space:nowrap;flex:none}}
 /* Trip cards */
 .tgrid{{display:grid;gap:16px}}
 .tcard{{background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:18px;padding:20px 22px;
   box-shadow:0 24px 60px -28px rgba(0,0,0,.8)}}
 .tcard.empty{{color:var(--mist);text-align:center}}
 .tchdr{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;flex-wrap:wrap}}
 .tchdr h3{{font-weight:700;font-size:19px;margin:0}}
 .badge{{font-family:"Space Grotesk",monospace;text-transform:uppercase;letter-spacing:.08em;
   font-size:11px;padding:5px 11px;border-radius:999px;white-space:nowrap}}
 .badge.prep{{background:rgba(148,184,198,.16);color:var(--surf)}}
 .badge.quote{{background:rgba(255,210,63,.16);color:var(--gold)}}
 .badge.booked{{background:rgba(61,240,197,.16);color:var(--lagoon)}}
 .badge.done{{background:rgba(148,184,198,.12);color:var(--mist)}}
 .row{{display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-top:1px solid var(--line);font-size:14px}}
 .row .k{{color:var(--mist);flex:none}}
 .row .v{{font-weight:600;text-align:right;word-break:break-word}}
 .eco{{margin:12px 0 4px;font-size:15px}}
 .muted{{color:var(--mist);font-size:13px;margin-top:10px;font-weight:400}}
 /* Buttons */
 .btn{{display:inline-flex;align-items:center;justify-content:center;gap:6px;
   font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:15px;
   min-height:48px;padding:12px 22px;border-radius:999px;text-decoration:none;cursor:pointer;
   color:#02161c;border:0;background:linear-gradient(120deg,var(--pacific),var(--lagoon));
   box-shadow:0 12px 30px -12px var(--glow);transition:transform .15s,box-shadow .15s}}
 .btn:hover{{transform:translateY(-1px);box-shadow:0 16px 36px -12px var(--glow)}}
 .btn.block{{width:100%}}
 .btn.ghost{{background:transparent;color:var(--foam);border:1px solid var(--line);box-shadow:none}}
 .actions{{display:flex;gap:12px;margin-top:22px;flex-wrap:wrap}}
 .actions .btn{{flex:1;min-width:160px}}
 /* Forms */
 .form{{margin-top:6px}}
 .fset{{background:linear-gradient(180deg, rgba(20,62,82,.4), rgba(8,33,47,.5));
   border:1px solid var(--line);border-radius:18px;padding:18px 18px 20px;margin-bottom:16px}}
 .fset>h3{{font-size:15px;margin:0 0 14px;font-weight:700}}
 .fset>h3 .muted{{font-size:12px;margin:0}}
 .formgrid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
 .field{{display:flex;flex-direction:column;min-width:0}}
 .field.half{{grid-column:span 1}}
 .field:not(.half){{grid-column:1 / -1}}
 .field label{{font-size:12px;color:var(--mist);margin:0 0 6px;font-weight:500}}
 .field .req{{color:var(--gold)}}
 .field input{{width:100%;font:inherit;font-size:16px;color:var(--foam);
   min-height:48px;padding:12px 13px;border-radius:12px;
   border:1px solid var(--line);background:var(--field)}}
 .field input::placeholder{{color:#6f93a3}}
 .field input:focus{{outline:none;border-color:var(--pacific);box-shadow:0 0 0 3px rgba(25,211,230,.18)}}
 .field.miss input{{border-color:rgba(255,210,63,.55)}}
 /* Progress */
 .prog{{height:8px;border-radius:999px;background:rgba(8,33,47,.7);overflow:hidden;margin:2px 0 6px}}
 .prog span{{display:block;height:100%;border-radius:999px;
   background:linear-gradient(90deg,var(--pacific),var(--lagoon));transition:width .4s ease}}
 .note{{border-radius:12px;padding:11px 14px;font-size:14px;margin:0 0 16px}}
 .note.ok{{background:rgba(61,240,197,.12);border:1px solid rgba(61,240,197,.4);color:var(--lagoon)}}
 /* Info / confirm pages */
 .infobox{{max-width:460px;margin:56px auto;text-align:center;
   background:linear-gradient(180deg, rgba(20,62,82,.5), rgba(8,33,47,.6));
   border:1px solid var(--line);border-radius:20px;padding:34px 26px}}
 .infobox h2{{font-weight:800;margin:0 0 10px}}
 .infobox p{{color:var(--mist);font-size:15px;line-height:1.55;margin:0}}
 .foot{{text-align:center;color:var(--mist);opacity:.7;font-size:11px;margin-top:34px}}
 /* Mobile */
 @media (max-width:560px){{
   .wrap{{padding:18px 15px 56px}}
   .hello h2{{font-size:23px}}
   .tcard,.fset{{padding:17px 16px}}
   .formgrid{{grid-template-columns:1fr;gap:13px}}
   .field.half{{grid-column:1 / -1}}
   .actions{{flex-direction:column-reverse}}
   .actions .btn{{width:100%}}
   .brand span{{font-size:9px;letter-spacing:.16em}}
 }}
 @media (prefers-reduced-motion: reduce){{
   *{{transition:none !important;animation:none !important}}
 }}
</style></head><body>
 <div class="top">
   <div class="brand"><img src="/static/logo.png" alt="Du Voyageur">
     <div><b>Du Voyageur</b><span>Espace client</span></div></div>
   {logout}
 </div>
 {nav}
 <div class="wrap">{body}
   <div class="foot">Du Voyageur · Permis d'agence 700495</div>
 </div>
</body></html>"""
