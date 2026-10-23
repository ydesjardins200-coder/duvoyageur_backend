"""Admin pages for the self-service engine model: dashboard, all searches,
engine & content settings, and the « searches » card on a member's file.

Data: DmoSearch rows (client_id 0 = anonymous visitor, stats only) and Client.
Rendering reuses the admin shell from main (lazy import: main imports us).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from html import escape
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from auth import require_admin
from config import settings
from db import Client, DmoSearch, SessionLocal, get_setting
from i18n_portal import LANG
from portal_searches import GATEWAYS, TZ, _chips, _date, _fields, _relaunch_url

router = APIRouter()

_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

_CSS = """<style>
.ae-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.ae-kpi{border:1px solid rgba(255,255,255,.12);border-radius:14px;padding:14px 16px;background:rgba(255,255,255,.03)}
.ae-kpi b{display:block;font-size:1.7rem;line-height:1.1;color:#fff}
.ae-kpi span{font-size:.78rem;color:#93a4b8;text-transform:uppercase;letter-spacing:.06em}
.ae-kpi small{display:block;margin-top:4px;color:#93a4b8;font-size:.78rem}
.ae-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:16px}
.ae-chart{display:flex;align-items:flex-end;gap:2px;height:150px;padding:6px 0 0;border-bottom:1px solid rgba(255,255,255,.18)}
.ae-chart div{flex:1;min-width:3px;background:#F4C543;border-radius:4px 4px 0 0;position:relative;cursor:default}
.ae-chart div:hover{filter:brightness(1.15)}
.ae-chart div.z{background:rgba(255,255,255,.08);height:2px!important;border-radius:0}
.ae-axis{display:flex;justify-content:space-between;color:#93a4b8;font-size:.72rem;margin-top:4px}
.ae-tip{position:fixed;pointer-events:none;background:#0F2A47;color:#fff;border:1px solid rgba(255,255,255,.2);border-radius:8px;padding:6px 9px;font-size:.8rem;display:none;z-index:50}
.ae-bars{display:grid;gap:7px}
.ae-bar{display:grid;grid-template-columns:minmax(140px,46%) 1fr auto;gap:10px;align-items:center;font-size:.86rem}
.ae-bar .t{height:10px;border-radius:99px;background:rgba(255,255,255,.07);overflow:hidden}
.ae-bar .t i{display:block;height:100%;background:#F4C543;border-radius:99px}
.ae-bar .v{color:#93a4b8;font-variant-numeric:tabular-nums}
.ae-bar .l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ae-tbl td,.ae-tbl th{vertical-align:top}
.ae-tag{display:inline-block;background:rgba(244,197,67,.14);color:#F4C543;border-radius:6px;padding:1px 7px;margin:1px 3px 1px 0;font-size:.76rem}
.ae-anon{color:#93a4b8;font-style:italic}
.ae-hot{color:#ff9f6b;font-weight:600;font-size:.78rem}
.ae-filters{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.ae-filters a{border:1px solid rgba(255,255,255,.18);border-radius:99px;padding:5px 12px;color:#cfe0ee;text-decoration:none;font-size:.85rem}
.ae-filters a.on{background:#F4C543;color:#0F2A47;border-color:#F4C543;font-weight:600}
.ae-filters form{display:flex;gap:6px;margin-left:auto}
.ae-links a{margin-right:12px;white-space:nowrap}
.ae-sum{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 12px}
.ae-sum div{border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:8px 12px;min-width:120px}
.ae-sum b{display:block;color:#F4C543}
.ae-sum span{font-size:.75rem;color:#93a4b8}
</style>"""


def _shell(body: str, active: str) -> HTMLResponse:
    from main import render_page   # lazy: main imports this module
    return HTMLResponse(render_page(_CSS + body, active))


def _header(title: str, refresh: str | None = None) -> str:
    from main import page_header
    return page_header(title, refresh)


def _local(dt: datetime) -> datetime:
    from zoneinfo import ZoneInfo
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)


def _when(dt: datetime) -> str:
    l = _local(dt)
    today = datetime.now(TZ).date()
    if l.date() == today:
        return f"Auj. {l:%H:%M}"
    if l.date() == today - timedelta(days=1):
        return f"Hier {l:%H:%M}"
    return f"{l.day} {_MONTHS[l.month - 1]} {l:%H:%M}"


def _dest_key(v: str) -> tuple[str, str]:
    """(stable key, label) — the engine label differs by language, the ids don't."""
    label, _, ids = (v or "").partition(" xxx")
    return (ids or label.strip().lower(), label.strip() or "—")


def _summary(f: dict) -> str:
    gw = f.get("gateway_dep", "")
    return f"{escape(gw)} → {escape(_dest_key(f.get('dest_dep', ''))[1])}"


def _fr(fn, *a):
    tok = LANG.set("fr")
    try:
        return fn(*a)
    finally:
        LANG.reset(tok)


def _rows(db, days: int, client_id: int | None = None, limit: int | None = None) -> list[DmoSearch]:
    q = db.query(DmoSearch).filter(DmoSearch.created_at >= datetime.utcnow() - timedelta(days=days))
    if client_id is not None:
        q = q.filter(DmoSearch.client_id == client_id)
    q = q.order_by(DmoSearch.created_at.desc())
    return (q.limit(limit) if limit else q).all()


def _bars(counter: Counter, n: int = 8, label=lambda k: k) -> str:
    items = counter.most_common(n)
    if not items:
        return "<p class='muted'>Pas encore de données.</p>"
    top = items[0][1] or 1
    return "<div class='ae-bars'>" + "".join(
        f"<div class='ae-bar'><span class='l' title=\"{escape(str(label(k)))}\">{escape(str(label(k)))}</span>"
        f"<span class='t'><i style='width:{max(4, round(100 * c / top))}%'></i></span><span class='v'>{c}</span></div>"
        for k, c in items) + "</div>"


def _aggregate(rows: list[DmoSearch]) -> dict:
    dests, dlabels, gws, months, party, budgets = Counter(), {}, Counter(), Counter(), Counter(), Counter()
    fam = 0
    for s in rows:
        f = _fields(s)
        k, lab = _dest_key(f.get("dest_dep", ""))
        dests[k] += 1
        dlabels.setdefault(k, lab)
        gw = f.get("gateway_dep", "")
        gws[f"{GATEWAYS.get(gw, gw)} ({gw})" if gw else "—"] += 1
        d = _date(f.get("date_dep", ""))
        if d:
            months[(d.year, d.month)] += 1
        try:
            ad, kids = int(f.get("nb_adult") or 0), int(f.get("nb_child") or 0)
        except ValueError:
            ad, kids = 0, 0
        if kids:
            fam += 1
        party["2 adultes" if (ad == 2 and not kids) else
              ("Famille (avec enfants)" if kids else
               ("1 adulte" if ad == 1 else f"{ad} adultes"))] += 1
        pm = f.get("price_max", "")
        budgets["Sans limite" if (not pm or pm == "99999") else f"≤ {int(pm):,} $".replace(",", " ")] += 1
    return {"dests": dests, "dlabels": dlabels, "gws": gws, "months": months, "party": party,
            "budgets": budgets, "fam": fam}


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@router.get("/admin/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_dashboard():
    now = datetime.utcnow()
    with SessionLocal() as db:
        n_members = db.query(Client).filter(Client.primary_email.isnot(None)).count()
        new7 = db.query(Client).filter(Client.created_at >= now - timedelta(days=7)).count()
        new30 = db.query(Client).filter(Client.created_at >= now - timedelta(days=30)).count()
        active7 = db.query(Client).filter(Client.last_seen_at >= now - timedelta(days=7)).count()
        rows30 = _rows(db, 30)
        rows90 = _rows(db, 90)
        latest = rows30[:12]
        names = {c.id: c.display_name or f"Membre #{c.id}" for c in
                 db.query(Client).filter(Client.id.in_({s.client_id for s in latest if s.client_id})).all()}
    s7 = sum(1 for s in rows30 if s.created_at >= now - timedelta(days=7))
    members30 = len({s.client_id for s in rows30 if s.client_id})
    anon30 = sum(1 for s in rows30 if not s.client_id)
    agg = _aggregate(rows90)

    # searches per day, last 30 days (local dates)
    today = datetime.now(TZ).date()
    per_day = Counter(_local(s.created_at).date() for s in rows30)
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    top = max([per_day[d] for d in days] + [1])
    bars = "".join(
        f"<div class='{'' if per_day[d] else 'z'}' style='height:{round(100 * per_day[d] / top)}%' "
        f"data-tip=\"{d.day} {_MONTHS[d.month - 1]} : {per_day[d]} recherche{'s' if per_day[d] != 1 else ''}\"></div>"
        for d in days)
    chart = (f"<div class='card'><h3>Recherches par jour (30 jours)</h3><div class='ae-chart'>{bars}</div>"
             f"<div class='ae-axis'><span>{days[0].day} {_MONTHS[days[0].month - 1]}</span>"
             f"<span>Aujourd'hui</span></div></div>")

    kpis = (
        "<div class='ae-kpis'>"
        f"<div class='ae-kpi'><span>Membres</span><b>{n_members}</b><small>+{new7} en 7 j · +{new30} en 30 j</small></div>"
        f"<div class='ae-kpi'><span>Actifs (7 j)</span><b>{active7}</b><small>visite du portail</small></div>"
        f"<div class='ae-kpi'><span>Recherches (7 j)</span><b>{s7}</b><small>{len(rows30)} en 30 j</small></div>"
        f"<div class='ae-kpi'><span>Membres qui cherchent</span><b>{members30}</b><small>30 j · +{anon30} recherches anonymes</small></div>"
        f"<div class='ae-kpi'><span>Familles</span><b>{round(100 * agg['fam'] / len(rows90)) if rows90 else 0} %</b><small>des recherches (90 j)</small></div>"
        "</div>")
    month_lbl = lambda k: f"{_MONTHS[k[1] - 1]} {k[0]}"  # noqa: E731
    grid = (
        "<div class='ae-grid'>"
        f"<div class='card'><h3>Top destinations (90 j)</h3>{_bars(agg['dests'], 8, lambda k: agg['dlabels'].get(k, k))}</div>"
        f"<div class='card'><h3>Mois de départ visés</h3>{_bars(agg['months'], 8, month_lbl)}</div>"
        f"<div class='card'><h3>Villes de départ</h3>{_bars(agg['gws'], 6)}</div>"
        f"<div class='card'><h3>Voyageurs</h3>{_bars(agg['party'], 6)}</div>"
        f"<div class='card'><h3>Budget indiqué</h3>{_bars(agg['budgets'], 6)}</div>"
        "</div>")
    latest_rows = "".join(
        f"<tr><td>{_when(s.created_at)}</td>"
        f"<td>{(f'<a href=/admin/clients/{s.client_id}>{escape(names.get(s.client_id, str(s.client_id)))}</a>') if s.client_id else '<span class=ae-anon>visiteur</span>'}</td>"
        f"<td><b>{_summary(_fields(s))}</b><br><span class='muted'>{escape(' · '.join(t for t, _ in _fr(_chips, _fields(s))[:3]))}</span></td>"
        f"<td><a href=\"{escape(_relaunch_url(s))}\" target='_blank'>↻ Ouvrir</a></td></tr>"
        for s in latest) or "<tr><td colspan='4' class='muted'>Aucune recherche pour l'instant.</td></tr>"
    latest_card = (f"<div class='card'><h3>Dernières recherches</h3><table class='ae-tbl'>"
                   f"<tr><th>Quand</th><th>Qui</th><th>Recherche</th><th></th></tr>{latest_rows}</table>"
                   f"<p style='margin-top:10px'><a href='/admin/recherches'>Toutes les recherches →</a></p></div>")
    tip = ("<div class='ae-tip' id='aeTip'></div><script>(function(){var t=document.getElementById('aeTip');"
           "document.querySelectorAll('[data-tip]').forEach(function(b){b.addEventListener('mousemove',function(e){"
           "t.textContent=b.dataset.tip;t.style.display='block';t.style.left=(e.clientX+12)+'px';t.style.top=(e.clientY-30)+'px'});"
           "b.addEventListener('mouseleave',function(){t.style.display='none'})})})();</script>")
    body = _header("Tableau de bord", "/admin/dashboard") + kpis + chart + "<div style='height:16px'></div>" + grid + latest_card + tip
    return _shell(body, "dashboard")


# --------------------------------------------------------------------------- #
# All searches
# --------------------------------------------------------------------------- #
@router.get("/admin/recherches", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_searches(qui: str = "tous", q: str = "", jours: int = 30):
    jours = jours if jours in (7, 30, 90, 365) else 30
    with SessionLocal() as db:
        rows = _rows(db, jours, limit=2000)
        ids = {s.client_id for s in rows if s.client_id}
        names = {c.id: c.display_name or f"Membre #{c.id}" for c in
                 db.query(Client).filter(Client.id.in_(ids)).all()} if ids else {}
    if qui == "membres":
        rows = [s for s in rows if s.client_id]
    elif qui == "anonymes":
        rows = [s for s in rows if not s.client_id]
    needle = q.strip().lower()
    if needle:
        rows = [s for s in rows if needle in (_summary(_fields(s)) + " " + names.get(s.client_id, "")).lower()]
    shown = rows[:300]

    def flt(k, v, label):
        on = " class='on'" if (k == "qui" and qui == v) or (k == "jours" and jours == v) else ""
        params = {"qui": qui, "jours": jours, "q": q}
        params[k] = v
        return f"<a{on} href='/admin/recherches?{urlencode(params)}'>{label}</a>"

    filters = ("<div class='ae-filters'>" + flt("qui", "tous", "Tous") + flt("qui", "membres", "Membres")
               + flt("qui", "anonymes", "Visiteurs anonymes") + "&nbsp;&nbsp;"
               + flt("jours", 7, "7 j") + flt("jours", 30, "30 j") + flt("jours", 90, "90 j") + flt("jours", 365, "12 mois")
               + f"<form method='get' action='/admin/recherches'><input type='hidden' name='qui' value='{escape(qui)}'>"
               f"<input type='hidden' name='jours' value='{jours}'><input name='q' value=\"{escape(q)}\" placeholder='Destination, départ, membre…'>"
               "<button>Filtrer</button></form></div>")
    today = datetime.now(TZ).date()
    trs = []
    for s in shown:
        f = _fields(s)
        d = _date(f.get("date_dep", ""))
        soon = d and today <= d <= today + timedelta(days=60)
        who = (f"<a href='/admin/clients/{s.client_id}'>{escape(names.get(s.client_id, str(s.client_id)))}</a>"
               if s.client_id else "<span class='ae-anon'>visiteur</span>")
        trs.append(
            f"<tr><td>{_when(s.created_at)}</td><td>{who}</td><td><b>{_summary(f)}</b>"
            + (" <span class='ae-hot'>● départ &lt; 60 j</span>" if soon else "")
            + f"<br><span class='muted'>{escape(' · '.join(t for t, _ in _fr(_chips, f)))}</span></td>"
            f"<td>{s.lang.upper()}</td><td><a href=\"{escape(_relaunch_url(s))}\" target='_blank'>↻ Ouvrir</a></td></tr>")
    table = ("<table class='ae-tbl'><tr><th>Quand</th><th>Qui</th><th>Recherche</th><th>Langue</th><th></th></tr>"
             + ("".join(trs) or "<tr><td colspan='5' class='muted'>Aucune recherche.</td></tr>") + "</table>")
    more = f"<p class='muted'>{len(rows)} recherches · 300 premières affichées.</p>" if len(rows) > 300 else f"<p class='muted'>{len(rows)} recherche(s).</p>"
    body = _header("Recherches", "/admin/recherches") + filters + more + table
    return _shell(body, "recherches")


# --------------------------------------------------------------------------- #
# Engine & content
# --------------------------------------------------------------------------- #
@router.get("/admin/moteur", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_engine(saved: int = 0):
    from main import _portal_banner_form          # the banner form lives with its POST handler
    from portal_home import _fetch_ads
    ads = _fetch_ads("fr")
    ads_state = (f"<span style='color:#4ade80'>● Active</span> — {len(ads)} forfait(s) vedette(s) reçus."
                 if ads else "<span style='color:#fbbf24'>● En attente</span> — l'API TRAVELSAVERS ne renvoie "
                 "aucun forfait pour l'alias pour l'instant. Le portail affiche le bloc d'attente (destinations populaires).")
    portal = escape(settings.PUBLIC_BASE_URL.rstrip("/") + "/portail")
    body = (
        _header("Moteur et contenu")
        + "<div class='ae-grid'>"
        "<div class='card'><h3>Moteur Softvoyage</h3>"
        f"<div class='kv'><span class='k'>Agence / alias</span><span class='v'>{escape(settings.SIRRVA_CODE_AG)} / {escape(settings.SIRRVA_ALIAS)}</span></div>"
        "<div class='kv'><span class='k'>Page de résultats</span><span class='v'>/results</span></div>"
        "<p class='ae-links' style='margin-top:12px'><a href='/admin/dmo-lab'>🧪 Labo moteur</a>"
        f"<a href='{portal}' target='_blank'>Voir le portail →</a></p></div>"
        f"<div class='card'><h3>Forfaits vedettes (TRAVELSAVERS)</h3><p>{ads_state}</p>"
        "<p class='muted'>Les cartes s'affichent automatiquement dans le portail dès que l'API en renvoie "
        "(vérification toutes les 10 minutes).</p></div>"
        "</div>"
        + _portal_banner_form(bool(saved), "/admin/moteur")
    )
    return _shell(body, "moteur")


# --------------------------------------------------------------------------- #
# Member file card
# --------------------------------------------------------------------------- #
def member_searches_card(client) -> str:
    with SessionLocal() as db:
        rows = _rows(db, 365, client_id=client.id, limit=50)
    if not rows:
        return ("<div class='card'><h3>🔎 Recherches dans le moteur</h3>"
                "<p class='muted'>Aucune recherche enregistrée pour ce membre.</p></div>")
    agg = _aggregate(rows)
    today = datetime.now(TZ).date()
    upcoming = [_date(_fields(s).get("date_dep", "")) for s in rows]
    upcoming = sorted(d for d in upcoming if d and d >= today)
    dests = ", ".join(agg["dlabels"].get(k, k) for k, _ in agg["dests"].most_common(3))
    period = (f"{_MONTHS[upcoming[0].month - 1]} {upcoming[0].year}" if upcoming else "—")
    party = agg["party"].most_common(1)[0][0] if agg["party"] else "—"
    budget = agg["budgets"].most_common(1)[0][0] if agg["budgets"] else "—"
    msgr = escape(settings.MESSENGER_URL)
    summary = ("<div class='ae-sum'>"
               f"<div><b>{escape(dests)}</b><span>Destinations</span></div>"
               f"<div><b>{escape(period)}</b><span>Prochain départ visé</span></div>"
               f"<div><b>{escape(party)}</b><span>Voyageurs</span></div>"
               f"<div><b>{escape(budget)}</b><span>Budget</span></div></div>")
    trs = []
    for s in rows[:20]:
        f = _fields(s)
        d = _date(f.get("date_dep", ""))
        past = bool(d and d < today)
        trs.append(
            f"<tr{' style=opacity:.55' if past else ''}><td>{_when(s.created_at)}</td>"
            f"<td><b>{_summary(f)}</b><br><span class='muted'>{escape(' · '.join(t for t, _ in _fr(_chips, f)))}</span></td>"
            f"<td class='ae-links'><a href=\"{escape(_relaunch_url(s))}\" target='_blank'>↻ Ouvrir</a></td></tr>")
    last = _when(rows[0].created_at)
    return (f"<div class='card' style='margin-bottom:16px'>{_CSS}<h3>🔎 Recherches dans le moteur</h3>"
            f"<div class='sub'>{len(rows)} recherche(s) · dernière : {escape(last)} · "
            f"<a href='{msgr}' target='_blank'>💬 Messenger</a></div>"
            + summary + "<table class='ae-tbl'>" + "".join(trs) + "</table></div>")
