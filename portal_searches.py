"""Portal « Mes recherches » — a member's package-search history.

Each search a logged-in member runs in the Softvoyage engine reaches our
/results page (the engine POSTs there with every search field, then we frame
their results). We keep those fields per member so they can relaunch a search
in one click. Hotels viewed inside the engine are NOT visible to us (cross-origin
iframe), so only the search criteria are stored. Kept 12 months (Loi 25).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db import Client, DmoSearch, SessionLocal
from i18n_portal import L, LANG
from portal import (_bell_panel, _logged_out, _nav, _set_session_cookie, _shell,
                    current_portal_client_id, results_client_id)

router = APIRouter()

TZ = ZoneInfo("America/Toronto")
RETENTION_DAYS = 365
DEDUPE_MINUTES = 15
MAX_SHOWN = 50
_SKIP = {"sid"}                      # per-session token: never stored or replayed

GATEWAYS = {
    "YUL": "Montréal", "YQB": "Québec", "YOW": "Ottawa", "YYZ": "Toronto", "YHM": "Hamilton",
    "YKF": "Kitchener", "YXU": "London", "YQG": "Windsor", "YQT": "Thunder Bay", "YSB": "Sudbury",
    "YHZ": "Halifax", "YQM": "Moncton", "YFC": "Fredericton", "YSJ": "Saint John",
    "YYG": "Charlottetown", "YYT": "St. John's", "YDF": "Deer Lake", "YQX": "Gander",
    "YYR": "Goose Bay", "YBG": "Bagotville", "YWG": "Winnipeg", "YBR": "Brandon",
    "YQR": "Regina", "YXE": "Saskatoon", "YYC": "Calgary", "YEG": "Edmonton",
    "YMM": "Fort McMurray", "YQU": "Grande Prairie", "YVR": "Vancouver", "YYJ": "Victoria",
    "YLW": "Kelowna", "YKA": "Kamloops", "YXX": "Abbotsford", "YQQ": "Comox", "YXC": "Cranbrook",
    "YCD": "Nanaimo", "YXJ": "Fort Saint John", "YXS": "Prince George",
}
_DUR = {"4": ("3 ou 4 jours", "3 or 4 days"), "7": ("5 à 10 jours", "5 to 10 days"),
        "14": ("11 à 16 jours", "11 to 16 days"), "21": ("17 jours ou plus", "17 days or more")}
_FEATURES = {
    "all_inclusive": ("Tout inclus", "All inclusive"), "beach": ("Sur la plage", "Beachfront"),
    "adults_only": ("Adultes seulement", "Adults only"), "family": ("Familial", "Family"),
    "spa": ("Spa", "Spa"), "golf": ("Golf", "Golf"), "casino": ("Casino", "Casino"),
    "weddings": ("Mariages", "Weddings"), "kitchenette": ("Cuisinette", "Kitchenette"),
    "oceanview": ("Vue mer", "Ocean view"), "miniclub": ("Mini-club", "Kids club"),
    "villa": ("Villa", "Villa"), "wifi": ("Wifi", "Wifi"), "waterpark": ("Jeux d'eau", "Water park"),
    "swimout": ("Swim out", "Swim-out"), "eco_responsible": ("Écoresponsable", "Eco-friendly"),
    "no_extra_single": ("Sans supplément simple", "No single supplement"),
}
_MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _t(pair):
    return L(*pair)


# --------------------------------------------------------------------------- #
# Recording (called from POST /results)
# --------------------------------------------------------------------------- #
def record_search(request: Request, form, lang: str) -> None:
    """Store the search for the member recognised by the /results cookie.
    Anonymous visitors are not stored. Same search within 15 min = one entry."""
    cid = results_client_id(request)
    if not cid:
        return
    pairs = [[k, v] for k, v in form.multi_items()
             if isinstance(v, str) and k not in _SKIP and len(v) <= 4000][:120]
    if not any(k == "gateway_dep" for k, _ in pairs):
        return                                   # not a search (e.g. a stray post)
    now = datetime.utcnow()
    with SessionLocal() as db:
        if db.get(Client, cid) is None:
            return
        last = (db.query(DmoSearch).filter(DmoSearch.client_id == cid)
                .order_by(DmoSearch.created_at.desc()).first())
        if last and last.fields == pairs and now - last.created_at < timedelta(minutes=DEDUPE_MINUTES):
            last.created_at = now
        else:
            db.add(DmoSearch(client_id=cid, fields=pairs, lang=lang if lang in ("fr", "en") else "fr",
                             created_at=now))
        db.query(DmoSearch).filter(DmoSearch.client_id == cid,
                                   DmoSearch.created_at < now - timedelta(days=RETENTION_DAYS)
                                   ).delete(synchronize_session=False)
        db.commit()


# --------------------------------------------------------------------------- #
# Display helpers
# --------------------------------------------------------------------------- #
def _fields(s: DmoSearch) -> dict:
    out: dict = {}
    for k, v in s.fields or []:
        out.setdefault(k, v)
    return out


def _date(v: str):
    try:
        return datetime.strptime((v or "").replace("-", "/"), "%Y/%m/%d").date()
    except ValueError:
        return None


def _fmt_date(d) -> str:
    if LANG.get() == "en":
        return f"{_MONTHS_EN[d.month - 1]} {d.day}, {d.year}"
    return f"{d.day} {_MONTHS_FR[d.month - 1]} {d.year}"


def _when(created_utc: datetime) -> str:
    local = created_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    today = datetime.now(TZ).date()
    hm_fr, hm_en = f"{local.hour} h {local.minute:02d}", local.strftime("%-I:%M %p")
    if local.date() == today:
        return L(f"Recherché aujourd'hui à {hm_fr}", f"Searched today at {hm_en}")
    if local.date() == today - timedelta(days=1):
        return L(f"Recherché hier à {hm_fr}", f"Searched yesterday at {hm_en}")
    return L(f"Recherché le {_fmt_date(local.date())}", f"Searched on {_fmt_date(local.date())}")


def _dest(v: str) -> str:
    label = (v or "").split(" xxx")[0].strip()
    return label or L("Destination", "Destination")


def _chips(f: dict) -> list[tuple[str, bool]]:
    """(label, highlighted) chips describing the search."""
    chips: list[tuple[str, bool]] = []
    d = _date(f.get("date_dep", ""))
    if d:
        chips.append((_fmt_date(d), True))
    dur = f.get("duration", "")
    if dur in _DUR:
        chips.append((_t(_DUR[dur]), False))
    elif dur.endswith("DAYS") and dur[:-4].isdigit():
        n = dur[:-4]
        chips.append((L(f"{n} jours exactement", f"Exactly {n} days"), False))
    try:
        ad = int(f.get("nb_adult") or 0)
        rooms = int(f.get("nb_rooms") or 1)
        kids = int(f.get("nb_child") or 0)
    except ValueError:
        ad, rooms, kids = 0, 1, 0
    if ad:
        people = L(f"{ad} adulte{'s' if ad > 1 else ''}", f"{ad} adult{'s' if ad > 1 else ''}")
        if kids:
            ages = []
            for i in range(1, kids + 1):
                a = f.get(f"non_adult{i}", "")
                if a == "1":
                    ages.append(L("moins de 2", "under 2"))
                elif a.isdigit():
                    ages.append(a)
            age_txt = (L(" (" + " et ".join([", ".join(ages[:-1]), ages[-1]] if len(ages) > 1 else ages) + " ans)",
                         " (ages " + ", ".join(ages) + ")") if ages else "")
            people += L(f", {kids} enfant{'s' if kids > 1 else ''}", f", {kids} child{'ren' if kids > 1 else ''}") + age_txt
        rooms_txt = L(f" · {rooms} chambre{'s' if rooms > 1 else ''}", f" · {rooms} room{'s' if rooms > 1 else ''}")
        chips.append((people + rooms_txt, False))
    pm = f.get("price_max", "")
    if pm.isdigit() and pm != "99999":
        chips.append((L(f"Moins de {int(pm):,} $".replace(",", " "), f"Under ${int(pm):,}"), False))
    star = f.get("star", "")
    if star.isdigit():
        chips.append((L("5 étoiles", "5 stars") if star == "5" else L(f"{star} étoiles et +", f"{star}+ stars"), False))
    if f.get("direct_flight") == "Y":
        chips.append((L("Vol direct", "Direct flight"), False))
    mw = f.get("max_wait", "")
    if mw.isdigit():
        h = int(mw) // 60
        chips.append((L(f"Escale de {h} h max", f"Layover {h} h max"), False))
    if f.get("no_hotel"):
        chips.append((L("Hôtels précis", "Specific hotels"), False))
    for k, pair in _FEATURES.items():
        if f.get(k) == "Y":
            chips.append((_t(pair), False))
    return chips


def _relaunch_url(s: DmoSearch) -> str:
    f = _fields(s)
    rp = f.get("revelex_page", "")
    host = (urlsplit(rp).hostname or "").lower()
    if not (rp.startswith("https://") and (host == "softvoyage.com" or host.endswith(".softvoyage.com"))):
        return "/portail"
    pairs = [(k, v) for k, v in (s.fields or []) if k not in _SKIP]
    return "/results?" + urlencode(pairs)


_CSS = """<style>
.ms-top{display:flex;justify-content:space-between;align-items:flex-end;gap:1rem;flex-wrap:wrap;margin-bottom:1.1rem}
.ms-top h2{margin:0}
.ms-top p{margin:.35rem 0 0;color:var(--mist)}
.ms-clear{background:none;border:0;padding:0;color:var(--mist);text-decoration:underline;font:500 .9rem Inter,sans-serif;cursor:pointer}
.ms-list{display:grid;gap:.9rem}
.ms-card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:1.1rem 1.25rem;box-shadow:var(--shadow);display:grid;grid-template-columns:1fr auto;gap:1rem;align-items:center}
.ms-route{font:800 1.12rem/1.25 Montserrat,sans-serif;color:var(--navy);display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
.ms-route .arr{color:#C9A227}
.ms-when{font:500 .78rem Inter,sans-serif;color:#8592A0;margin-top:.15rem}
.ms-chips{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.65rem}
.ms-chip{background:var(--warm);border:1px solid rgba(15,42,71,.1);border-radius:99px;padding:.3rem .7rem;font:500 .82rem Inter,sans-serif;color:var(--navy)}
.ms-chip.gold{background:rgba(244,197,67,.16);border-color:rgba(244,197,67,.5);color:#6B4E00}
.ms-act{display:flex;flex-direction:column;gap:.5rem;align-items:stretch;min-width:150px}
.ms-act form{margin:0;display:flex}
.ms-btn{display:block;background:var(--gold);color:var(--navy);border:0;border-radius:99px;padding:.7rem 1.1rem;font:800 .9rem Montserrat,sans-serif;text-align:center;text-decoration:none;box-shadow:0 4px 0 #C9A227}
.ms-btn:hover{text-decoration:none;filter:brightness(1.03)}
.ms-del{flex:1;background:#fff;border:1px solid rgba(15,42,71,.18);border-radius:99px;padding:.5rem 1rem;font:600 .82rem Inter,sans-serif;color:var(--mist);cursor:pointer}
.ms-del:hover{border-color:var(--navy);color:var(--navy)}
.ms-note{margin-top:1.2rem;font:500 .8rem/1.5 Inter,sans-serif;color:#8592A0;text-align:center}
.ms-past .ms-route,.ms-past .ms-chips{opacity:.6}
.ms-past .ms-btn{background:#fff;border:1px solid var(--navy);box-shadow:none}
.ms-empty{background:#fff;border:1px dashed rgba(15,42,71,.2);border-radius:18px;padding:2.2rem 1.4rem;text-align:center}
.ms-empty h3{margin:0 0 .4rem;font:800 1.2rem Montserrat,sans-serif;color:var(--navy)}
.ms-empty p{margin:0 auto 1.1rem;max-width:460px;color:var(--mist)}
.ms-empty .ms-btn{display:inline-block}
@media(max-width:640px){.ms-card{grid-template-columns:1fr}.ms-act{flex-direction:row}.ms-act>*{flex:1}}
</style>"""


def _card(s: DmoSearch) -> str:
    f = _fields(s)
    gw = f.get("gateway_dep", "")
    origin = GATEWAYS.get(gw, gw) or L("Départ", "Departure")
    d = _date(f.get("date_dep", ""))
    past = bool(d and d < datetime.now(TZ).date())
    chips = "".join(f"<span class='ms-chip{' gold' if hi else ''}'>{escape(t)}</span>" for t, hi in _chips(f))
    when = _when(s.created_at) + (L(" · date de départ passée", " · departure date passed") if past else "")
    if past:
        btn = f"<a class='ms-btn' href='/portail'>{L('Relancer avec une nouvelle date', 'Search again with a new date')}</a>"
    else:
        btn = f"<a class='ms-btn' href=\"{escape(_relaunch_url(s))}\">{L('Relancer →', 'Search again →')}</a>"
    return (f"<div class='ms-card{' ms-past' if past else ''}'><div>"
            f"<div class='ms-route'>{escape(origin)} <span class='arr'>→</span> {escape(_dest(f.get('dest_dep', '')))}</div>"
            f"<div class='ms-when'>{escape(when)}</div><div class='ms-chips'>{chips}</div></div>"
            f"<div class='ms-act'>{btn}"
            f"<form method='post' action='/portail/recherches/{s.id}/retirer'>"
            f"<button class='ms-del' type='submit'>{L('Retirer', 'Remove')}</button></form></div></div>")


def _page(searches: list[DmoSearch], flash: str = "") -> str:
    head = (f"<div class='ms-top'><div><h2>{L('Mes recherches', 'My searches')}</h2>"
            f"<p>{L('Retrouve tes recherches et relance-les en un clic. Ton Rabais Récompense est toujours appliqué.', 'Find your searches and run them again in one click. Your Reward Discount is always applied.')}</p></div>")
    if searches:
        confirm = L("Effacer tout ton historique de recherches ?", "Delete your whole search history?")
        head += (f"<form method='post' action='/portail/recherches/effacer' onsubmit=\"return confirm('{confirm}')\">"
                 f"<button class='ms-clear' type='submit'>{L('Effacer tout mon historique', 'Delete all my history')}</button></form>")
    head += "</div>"
    note = f"<div class='note ok'>{escape(flash)}</div>" if flash else ""
    if not searches:
        body = (f"<div class='ms-empty'><h3>{L('Aucune recherche pour l’instant', 'No searches yet')}</h3>"
                f"<p>{L('Tes recherches dans le moteur apparaîtront ici, prêtes à être relancées en un clic.', 'Your searches in the booking engine will show up here, ready to run again in one click.')}</p>"
                f"<a class='ms-btn' href='/portail'>{L('Rechercher un forfait', 'Find a package')}</a></div>")
    else:
        body = "<div class='ms-list'>" + "".join(_card(s) for s in searches) + "</div>"
    foot = (f"<p class='ms-note'>{L('On conserve tes recherches 12 mois pour te les proposer et t’aider à trouver ton forfait. Tu peux les effacer en tout temps.', 'We keep your searches for 12 months to show them to you and help you find your package. You can delete them at any time.')}</p>")
    return _CSS + note + head + body + foot


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/portail/recherches", response_class=HTMLResponse)
def my_searches(request: Request, ok: str = ""):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        client = db.get(Client, cid)
        if not client:
            return _logged_out()
        cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
        rows = (db.query(DmoSearch).filter(DmoSearch.client_id == cid, DmoSearch.created_at >= cutoff)
                .order_by(DmoSearch.created_at.desc()).limit(MAX_SHOWN).all())
        flash = {"retire": L("Recherche retirée.", "Search removed."),
                 "efface": L("Ton historique a été effacé.", "Your history has been deleted.")}.get(ok, "")
        body = _page(rows, flash)
        bell = _bell_panel(client)
    resp = HTMLResponse(_shell(L("Mes recherches", "My searches"), body, logged_in=True,
                               nav=_nav("recherches"), bell=bell))
    _set_session_cookie(resp, cid)
    return resp


@router.post("/portail/recherches/{search_id}/retirer")
def remove_search(request: Request, search_id: int):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        db.query(DmoSearch).filter(DmoSearch.id == search_id, DmoSearch.client_id == cid
                                   ).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse("/portail/recherches?ok=retire", status_code=303)


@router.post("/portail/recherches/effacer")
def clear_searches(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        db.query(DmoSearch).filter(DmoSearch.client_id == cid).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse("/portail/recherches?ok=efface", status_code=303)
