"""Portal home = « Rechercher un forfait »: the Softvoyage engine with a sidebar
explaining the Reward Discount, recent searches, the team and trust points;
a seasonal banner (edited in the admin) and a « resume your search » banner on
top; featured packages (TRAVELSAVERS ads API), guides and FAQ below.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import quote

from config import settings
from db import DmoSearch, SessionLocal, get_setting
from i18n_portal import L, LANG
from portal_searches import (GATEWAYS, TZ, _chips, _date, _dest, _fields, _fmt_date,
                             _relaunch_url)

BANNER_KEY = "portal_banner"
ADS_URL = "https://travelsavers.net3000.ca/ads/{alias}?languageId={lang}&pagesize=4"
_ADS_TTL = 600                      # seconds
_ads_cache: dict = {}               # lang -> (expires_at, list)


def _en() -> bool:
    return LANG.get() == "en"


def _site_page(fr_slug: str, en_slug: str) -> str:
    base = settings.SITE_BASE_URL.rstrip("/")
    return f"{base}/en/{en_slug}" if _en() else f"{base}/{fr_slug}"


def _asset(name: str) -> str:
    return f"{settings.SITE_BASE_URL.rstrip('/')}/assets/{name}"


def _first_name(client) -> str:
    kyc = client.kyc or {}
    n = (kyc.get("legal_first_name") or (client.display_name or "").split(" ")[0] or "").strip()
    return "" if n.lower() in ("", "client", "membre") else n[:40]


# --------------------------------------------------------------------------- #
# Banners
# --------------------------------------------------------------------------- #
def _season_banner() -> str:
    with SessionLocal() as db:
        b = get_setting(db, BANNER_KEY)
    if not b.get("active"):
        return ""
    until = b.get("until") or ""
    if until:
        try:
            if date.fromisoformat(until) < datetime.now(TZ).date():
                return ""
        except ValueError:
            pass
    text = (b.get("text_en") if _en() else b.get("text_fr")) or b.get("text_fr") or ""
    if not text.strip():
        return ""
    label = (b.get("link_label_en") if _en() else b.get("link_label_fr")) or ""
    url = (b.get("link_url") or "").strip()
    link = ""
    if label and url.startswith(("https://", "/")):
        link = f"<a href=\"{escape(url)}\">{escape(label)} →</a>"
    return f"<div class='sx-season'><span>🌴 {escape(text)}</span>{link}</div>"


def _resume_banner(client, last: DmoSearch | None) -> str:
    if not last:
        return ""
    f = _fields(last)
    gw = f.get("gateway_dep", "")
    origin = GATEWAYS.get(gw, gw)
    chips = " · ".join(t for t, _ in _chips(f)[:3])
    name = _first_name(client)
    hello = L(f"Bon retour, {name}", f"Welcome back, {name}") if name else L("Bon retour", "Welcome back")
    return (f"<div class='sx-banner' style=\"background-image:url('{_asset('pexels-12567844.webp')}')\"><div><div>"
            f"<small><span class='lg'>{escape(hello)}</span><span class='sm'>{L('Reprendre ta recherche', 'Resume your search')}</span></small>"
            f"<b><span class='lg'>{L('Reprendre ta recherche', 'Resume your search')} : </span>{escape(origin)} → {escape(_dest(f.get('dest_dep', '')))}</b>"
            f"<span>{escape(chips)}</span></div>"
            f"<a class='sx-btn' href=\"{escape(_relaunch_url(last))}\">{L('Voir les résultats →', 'See the results →')}</a>"
            f"</div></div>")


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def _steps_card() -> str:
    steps = [
        (L("Tu cherches", "You search"),
         L("Les mêmes forfaits que chez les grands voyagistes : même vol, même hôtel.",
           "The same packages as the big tour operators: same flight, same hotel.")),
        (L("Ton rabais est déjà appliqué", "Your discount is already applied"),
         L("On te remet une partie de notre commission, directement dans le prix affiché.",
           "We give you back part of our commission, right in the price shown.")),
        (L("Tu réserves en ligne", "You book online"),
         L("Directement dans le moteur, avec paiement sécurisé chez le voyagiste.",
           "Right in the engine, with secure payment through the tour operator.")),
        (L("On reste là pour toi", "We're here for you"),
         L("Nos conseillers certifiés t'accompagnent avant, pendant et après ton voyage.",
           "Our certified advisors support you before, during and after your trip.")),
    ]
    items = "".join(f"<li><span class='sx-n'>{i}</span><div><b>{escape(t)}</b><small>{escape(d)}</small></div></li>"
                    for i, (t, d) in enumerate(steps, 1))
    return (f"<details class='sx-card sx-navy' id='sxSteps' open><summary><h3>{L('Comment fonctionne ton', 'How your')} "
            f"<span>{L('Rabais Récompense', 'Reward Discount works')}</span></h3></summary>"
            f"<ol class='sx-steps'>{items}</ol>"
            f"<p class='sx-fine'>{L('Le rabais varie selon le forfait et le voyagiste. Prix et disponibilités fournis par le voyagiste au moment de la recherche.', 'The discount varies by package and tour operator. Prices and availability are provided by the tour operator at the time of the search.')}</p></details>"
            "<script>if(window.matchMedia('(max-width:700px)').matches){var d=document.getElementById('sxSteps');d&&d.removeAttribute('open')}</script>")


def _recent_card(recent: list[DmoSearch]) -> str:
    if not recent:
        return ""
    rows = ""
    for s in recent:
        f = _fields(s)
        gw = f.get("gateway_dep", "")
        d = _date(f.get("date_dep", ""))
        past = bool(d and d < datetime.now(TZ).date())
        href = "/portail" if past else _relaunch_url(s)
        meta = " · ".join(t for t, _ in _chips(f)[:3])
        rows += (f"<a href=\"{escape(href)}\"><b>{escape(GATEWAYS.get(gw, gw))} → {escape(_dest(f.get('dest_dep', '')))}</b>"
                 f"<small>{escape(meta)}</small></a>")
    return (f"<div class='sx-card sx-recent'><h3>{L('Tes recherches récentes', 'Your recent searches')}</h3>{rows}"
            f"<a class='sx-more' href='/portail/recherches'>{L('Voir toutes mes recherches →', 'See all my searches →')}</a></div>")


def _help_card() -> str:
    imgs = "".join(f"<img src=\"{_asset(f'equipe-{n}.webp')}\" alt='' loading='lazy'>"
                   for n in ("paola", "serge", "eva"))
    return (f"<div class='sx-card sx-help'><h3>{L('Une question avant de réserver ?', 'A question before booking?')}</h3>"
            f"<div class='sx-team'>{imgs}</div>"
            f"<p>{L('Paola, Serge et Eva te répondent sur Messenger.', 'Paola, Serge and Eva answer you on Messenger.')}</p>"
            f"<a class='sx-btn' href=\"{escape(settings.MESSENGER_URL)}\" target='_blank' rel='noopener'>"
            f"{L('Écrire à un conseiller →', 'Message an advisor →')}</a></div>")


def _trust_card() -> str:
    pts = [L("Agence québécoise · permis 700495", "Québec agency · licence 700495"),
           L("Conseillers certifiés CCV", "CCV-certified advisors"),
           L("Paiement sécurisé chez le voyagiste", "Secure payment through the tour operator")]
    return "<div class='sx-card sx-trust'>" + "".join(f"<div><i>★</i> {escape(p)}</div>" for p in pts) + "</div>"


# --------------------------------------------------------------------------- #
# Featured packages (TRAVELSAVERS ads API) — placeholder until ads exist
# --------------------------------------------------------------------------- #
def _fetch_ads(lang: str) -> list[dict]:
    now = time.time()
    hit = _ads_cache.get(lang)
    if hit and hit[0] > now:
        return hit[1]
    ads: list[dict] = []
    try:
        url = ADS_URL.format(alias=quote(settings.SIRRVA_ALIAS or "big"), lang=lang)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "duvoyageur"}),
                                    timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if data.get("success") and isinstance(data.get("data"), dict):
            for a in data["data"].get("ads") or []:
                if isinstance(a, dict) and a.get("hiddenFields"):
                    ads.append(a)
    except Exception:  # noqa: BLE001 — no ads (404) or network issue: show the placeholder
        ads = []
    _ads_cache[lang] = (now + _ADS_TTL, ads[:4])
    return ads[:4]


def _https(u: str) -> str:
    u = (u or "").strip()
    return "https:" + u if u.startswith("//") else u


def _ad_card(a: dict, i: int) -> str:
    fields = "".join(f"<input type='hidden' name=\"{escape(str(f.get('key', '')))}\" value=\"{escape(str(f.get('value', '')))}\">"
                     for f in a.get("hiddenFields") or [] if f.get("key") != "sid")
    img = _https(a.get("hotelImage", ""))
    ph = f" style=\"background-image:url('{escape(img)}')\"" if img.startswith("https://") else ""
    return (f"<form class='sx-deal' method='post' action='/results'>{fields}"
            f"<div class='ph'{ph}></div><div class='bd'>"
            f"<h4>{escape(a.get('tripTitle', ''))}</h4><div class='ho'>{escape(a.get('tripDescription', ''))}</div>"
            f"<div class='dt'>{escape(a.get('tripDetails', ''))}</div>"
            f"<div class='pr'><b>{escape(a.get('price', ''))}</b><span>{escape(a.get('taxes', ''))}</span></div>"
            f"<button class='go' type='submit'>{L('Voir ce forfait →', 'See this package →')}</button></div></form>")


_PLACEHOLDER = [
    ("Punta Cana", "pexels-1030322.webp", "voyage-punta-cana.html", "punta-cana-all-inclusive.html"),
    ("Cancún", "pexels-14020875.webp", "voyage-cancun-riviera-maya.html", "cancun-riviera-maya-all-inclusive.html"),
    ("Varadero", "pexels-36949951.webp", "voyage-varadero-cuba.html", "varadero-cuba-all-inclusive.html"),
    ("Montego Bay", "pexels-35841080.webp", "voyage-montego-bay-jamaique.html", "montego-bay-jamaica-all-inclusive.html"),
]


def _placeholder_card(name: str, img: str, fr: str, en: str) -> str:
    return (f"<a class='sx-deal' href=\"{escape(_site_page(fr, en))}\" target='_blank' rel='noopener'>"
            f"<div class='ph' style=\"background-image:url('{_asset(img)}')\"></div><div class='bd'>"
            f"<h4>Montréal → {escape(name)}</h4><div class='ho'>{L('Tout inclus', 'All inclusive')}</div>"
            f"<div class='dt'>{L('Forfaits vedettes bientôt disponibles', 'Featured packages coming soon')}</div>"
            f"<span class='go'>{L('Découvrir la destination →', 'Discover the destination →')}</span></div></a>")


def _deals_section() -> str:
    ads = _fetch_ads("en" if _en() else "fr")
    if ads:
        cards = "".join(_ad_card(a, i) for i, a in enumerate(ads))
        fine = L("Prix fournis par le voyagiste au moment de la mise à jour, sujets à changement et selon disponibilité.",
                 "Prices provided by the tour operator at the time of the update, subject to change and availability.")
        lede = L("Une sélection de forfaits. Un clic t'amène directement aux résultats.",
                 "A selection of packages. One click takes you straight to the results.")
    else:
        cards = "".join(_placeholder_card(*p) for p in _PLACEHOLDER)
        fine = ""
        lede = L("Nos forfaits vedettes arrivent bientôt. En attendant, découvre nos destinations populaires.",
                 "Our featured packages are coming soon. Meanwhile, discover our popular destinations.")
    return (f"<section class='sx-sec'><h2>{L('Forfaits vedettes', 'Featured packages')}</h2><p>{lede}</p>"
            f"<div class='sx-deals'>{cards}</div>"
            + (f"<p class='sx-fine2'>{fine}</p>" if fine else "") + "</section>")


# --------------------------------------------------------------------------- #
# Guides + FAQ
# --------------------------------------------------------------------------- #
def _guides_section() -> str:
    g = [("🛂", L("Documents et passeport", "Documents & passport"),
          L("E-Ticket, formulaires d'entrée par destination.", "E-Ticket and entry forms by destination."),
          "guide-documents-passeport.html", "guide-travel-documents-passport.html"),
         ("🧳", L("Quoi apporter", "What to pack"), L("La checklist complète du tout-inclus.", "The complete all-inclusive checklist."),
          "guide-quoi-apporter.html", "guide-what-to-pack.html"),
         ("📅", L("Meilleure période", "Best time to go"), L("Météo, ouragans, prix, mois par mois.", "Weather, hurricanes, prices, month by month."),
          "guide-meilleure-periode.html", "guide-best-time-to-travel.html"),
         ("💸", L("Pourboires", "Tipping"), L("Combien donner, et à qui.", "How much to give, and to whom."),
          "guide-pourboires.html", "guide-tipping-all-inclusive.html"),
         ("🛃", L("Limites de douane", "Customs limits"), L("Ce que tu peux rapporter au Canada.", "What you can bring back to Canada."),
          "guide-limites-douane.html", "guide-canada-customs-limits.html")]
    cards = "".join(f"<a class='sx-guide' href=\"{escape(_site_page(fr, en))}\" target='_blank' rel='noopener'>"
                    f"<i>{ic}</i><b>{escape(t)}</b><small>{escape(d)}</small></a>" for ic, t, d, fr, en in g)
    return (f"<section class='sx-sec'><h2>{L('Bon à savoir avant de réserver', 'Good to know before you book')}</h2>"
            f"<p>{L('Nos guides gratuits pour partir l’esprit tranquille.', 'Our free guides so you can leave with peace of mind.')}</p>"
            f"<div class='sx-guides'>{cards}</div></section>")


def _faq_section() -> str:
    # Only answers we can stand behind. Cancellation / insurance / after-booking
    # answers are pending Serge's validation (tour-operator conditions).
    qa = [(L("Comment fonctionne mon Rabais Récompense ?", "How does my Reward Discount work?"),
           L("On te remet une partie de notre commission directement dans le prix affiché du moteur. Tu n'as rien à faire : aucun code, aucune demande. Le rabais varie selon le forfait et le voyagiste.",
             "We give you back part of our commission right in the price shown in the engine. Nothing to do on your side: no code, no request. The discount varies by package and tour operator.")),
          (L("Où est-ce que je paie ?", "Where do I pay?"),
           L("Directement dans le moteur, avec un paiement sécurisé chez le voyagiste.",
             "Right in the engine, with secure payment through the tour operator.")),
          (L("J'ai une question sur un forfait ou ma réservation.", "I have a question about a package or my booking."),
           L("Écris-nous sur Messenger : Paola, Serge ou Eva te répondent.",
             "Message us on Messenger: Paola, Serge or Eva will answer you."))]
    items = "".join(f"<details{' open' if i == 0 else ''}><summary>{escape(q)}</summary><p>{escape(a)}</p></details>"
                    for i, (q, a) in enumerate(qa))
    return (f"<section class='sx-sec'><h2>{L('Questions fréquentes', 'Frequently asked questions')}</h2>"
            f"<div class='sx-faq'>{items}</div></section>")


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
_CSS = """<style>
.wrap:has(.sx){max-width:1300px}
.sx .sm{display:none}
.sx{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:1.4rem;align-items:start}
.sx-head h2{margin:0}
.sx .engine iframe{height:800px}
@media(max-width:700px){.sx .engine iframe{height:1150px}}
.sx-head p{margin:.4rem 0 1rem;color:var(--mist)}
.sx-badge{display:inline-flex;align-items:center;gap:.45rem;background:rgba(244,197,67,.18);border:1px solid rgba(244,197,67,.55);color:#6B4E00;border-radius:99px;padding:.35rem .8rem;font:700 .82rem Montserrat,sans-serif;margin-bottom:.7rem}
.sx-side{display:grid;gap:1rem;position:sticky;top:1rem}
.sx-card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:1.1rem 1.15rem;box-shadow:0 10px 30px -18px rgba(15,42,71,.3)}
.sx-card h3{margin:0 0 .8rem;font:800 1rem/1.25 Montserrat,sans-serif;color:var(--navy)}
.sx-navy{background:var(--navy);border-color:var(--navy);color:#fff}
.sx-navy h3{color:#fff}.sx-navy h3 span{color:var(--gold)}
.sx-steps{list-style:none;margin:0;padding:0;display:grid;gap:.85rem}
.sx-steps li{display:grid;grid-template-columns:30px 1fr;gap:.7rem;align-items:start}
.sx-n{width:30px;height:30px;border-radius:50%;background:var(--gold);color:var(--navy);display:grid;place-items:center;font:800 .9rem Montserrat,sans-serif}
.sx-steps b{display:block;font:700 .9rem Montserrat,sans-serif;color:#fff}
.sx-steps small{display:block;color:rgba(255,255,255,.78);font:400 .82rem/1.45 Inter,sans-serif;margin-top:.15rem}
.sx-fine{margin:.9rem 0 0;padding-top:.8rem;border-top:1px solid rgba(255,255,255,.15);font:400 .74rem/1.45 Inter,sans-serif;color:rgba(255,255,255,.65)}
.sx-recent a{display:block;text-decoration:none;padding:.6rem .7rem;border-radius:12px;background:var(--warm);margin-bottom:.45rem;border:1px solid rgba(15,42,71,.08)}
.sx-recent a:hover{border-color:var(--gold);text-decoration:none}
.sx-recent b{display:block;font:700 .86rem Montserrat,sans-serif;color:var(--navy)}
.sx-recent small{color:var(--mist);font:500 .76rem Inter,sans-serif}
.sx-recent a.sx-more{display:inline-block;background:none;border:0;padding:.2rem 0 0;margin:0;font:600 .82rem Inter,sans-serif;color:var(--surf)}
.sx-team{display:flex;margin:.1rem 0 .7rem}
.sx-team img{width:48px;height:48px;border-radius:50%;border:3px solid #fff;margin-right:-10px;box-shadow:0 4px 10px rgba(15,42,71,.2);background:var(--gold)}
.sx-help p{margin:0 0 .8rem;color:var(--mist);font:400 .85rem/1.5 Inter,sans-serif}
.sx-btn{display:block;text-align:center;background:var(--gold);color:var(--navy);border-radius:99px;padding:.7rem 1rem;font:800 .88rem Montserrat,sans-serif;text-decoration:none;box-shadow:0 4px 0 #C9A227}
.sx-btn:hover{text-decoration:none;filter:brightness(1.03)}
.sx-trust{display:grid;gap:.45rem;font:500 .8rem Inter,sans-serif;color:var(--navy)}
.sx-trust div{display:flex;gap:.5rem;align-items:center}.sx-trust i{font-style:normal;color:#C9A227}
.sx-banner{position:relative;border-radius:20px;overflow:hidden;margin-bottom:1.1rem;background-color:var(--navy);background-position:center 55%;background-size:cover;color:#fff;box-shadow:0 14px 34px -18px rgba(15,42,71,.6)}
.sx-banner::before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(15,42,71,.94) 0%,rgba(15,42,71,.78) 48%,rgba(15,42,71,.25) 100%)}
.sx-banner>div{position:relative;display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1.15rem 1.4rem;flex-wrap:wrap}
.sx-banner small{display:block;font:700 .72rem Montserrat,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--gold)}
.sx-banner b{display:block;font:800 1.2rem/1.25 Montserrat,sans-serif;margin:.2rem 0}
.sx-banner>div>div>span{font:400 .88rem Inter,sans-serif;color:rgba(255,255,255,.85)}
.sx-navy>summary{list-style:none;cursor:pointer}.sx-navy>summary::-webkit-details-marker{display:none}
.sx-banner .sx-btn{min-width:220px}
.sx-season{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;background:var(--gold);color:var(--navy);border-radius:12px;padding:.55rem .9rem;margin-bottom:1rem;font:600 .86rem Inter,sans-serif}
.sx-season a{margin-left:auto;color:var(--navy);font-weight:700}
.sx-bottom{margin-top:2.2rem;display:grid;grid-template-columns:minmax(0,1fr);gap:2rem}
.sx-bottom>*,.sx>*{min-width:0}
.sx-sec h2{margin:0 0 .25rem;font:800 1.35rem Montserrat,sans-serif;color:var(--navy)}
.sx-sec>p{margin:0 0 1rem;color:var(--mist)}
.sx-deals{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}
.sx-deal{display:block;margin:0;background:#fff;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 10px 30px -18px rgba(15,42,71,.35);text-decoration:none;color:inherit}
.sx-deal:hover{text-decoration:none;border-color:var(--gold)}
.sx-deal .ph{height:150px;background:#dfe6ee center/cover no-repeat}
.sx-deal .bd{padding:.85rem .95rem 1rem}
.sx-deal h4{margin:0;font:800 .98rem/1.25 Montserrat,sans-serif;color:var(--navy)}
.sx-deal .ho{color:var(--mist);font:500 .82rem Inter,sans-serif;margin:.2rem 0 .5rem}
.sx-deal .dt{font:500 .78rem Inter,sans-serif;color:var(--navy);background:var(--warm);border-radius:8px;padding:.35rem .5rem}
.sx-deal .pr{display:flex;align-items:baseline;justify-content:space-between;margin-top:.7rem}
.sx-deal .pr b{font:800 1.2rem Montserrat,sans-serif;color:var(--navy)}
.sx-deal .pr span{font:500 .72rem Inter,sans-serif;color:#8592A0}
.sx-deal .go{display:block;width:100%;margin-top:.7rem;text-align:center;background:var(--gold);border:0;border-radius:99px;padding:.55rem;font:800 .84rem Montserrat,sans-serif;color:var(--navy);cursor:pointer}
.sx-fine2{margin:.7rem 0 0;font:400 .76rem Inter,sans-serif;color:#8592A0}
.sx-guides{display:grid;grid-template-columns:repeat(5,1fr);gap:.8rem}
.sx-guide{display:block;background:#fff;border:1px solid var(--line);border-radius:16px;padding:1rem;text-decoration:none}
.sx-guide:hover{text-decoration:none;border-color:var(--gold)}
.sx-guide i{font-style:normal;font-size:1.6rem}
.sx-guide b{display:block;margin:.45rem 0 .2rem;font:800 .92rem/1.25 Montserrat,sans-serif;color:var(--navy)}
.sx-guide small{color:var(--mist);font:400 .8rem/1.4 Inter,sans-serif}
.sx-faq{display:grid;gap:.6rem}
.sx-faq details{background:#fff;border:1px solid var(--line);border-radius:14px;padding:.9rem 1.1rem}
.sx-faq summary{cursor:pointer;font:700 .95rem Montserrat,sans-serif;color:var(--navy);list-style:none;display:flex;justify-content:space-between;gap:1rem}
.sx-faq summary::-webkit-details-marker{display:none}
.sx-faq summary::after{content:"+";color:#C9A227;font-size:1.2rem}
.sx-faq details[open] summary::after{content:"–"}
.sx-faq p{margin:.6rem 0 0;color:var(--mist);font:400 .9rem/1.55 Inter,sans-serif}
@media(max-width:700px){
.sx>div:first-child{display:flex;flex-direction:column}
.sx-season{order:9;margin:.8rem 0 0;font-size:.78rem;padding:.45rem .7rem}
.sx-season a{display:none}
.sx-banner{margin-bottom:.6rem;border-radius:14px}
.sx-banner>div{padding:.6rem .8rem;flex-wrap:nowrap;gap:.6rem}
.sx-banner small{font-size:.62rem;letter-spacing:.08em}
.sx-banner b{font-size:.9rem;margin:.1rem 0 0}
.sx-banner>div>div>span{display:none}
.sx-banner>div>div{flex:1;min-width:0}
.sx-banner .sx-btn{min-width:0!important;width:auto!important;flex:none;padding:.5rem .8rem;font-size:.78rem;box-shadow:0 3px 0 #C9A227}
.sx .lg{display:none}.sx .sm{display:inline}
.sx-banner b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sx-head{display:flex;align-items:center;justify-content:space-between;gap:.5rem;flex-wrap:nowrap;margin-bottom:.45rem}
.sx-head h2{font-size:1.2rem}
.sx-head p{display:none}
.sx-badge{order:2;margin:0;padding:.2rem .55rem;font-size:.68rem}
.sx-side{gap:.7rem;margin-top:.9rem}
.sx-card{padding:.85rem .9rem;border-radius:14px}
.sx-steps{gap:.55rem}.sx-steps small{font-size:.78rem}.sx-fine{display:none}
.sx-navy>summary{list-style:none;cursor:pointer}
.sx-navy>summary::-webkit-details-marker{display:none}
.sx-navy:not([open]) h3{margin:0}
.sx-navy:not([open])>summary h3::after{content:" ▾";color:var(--gold)}
.sx-help p{display:none}.sx-team img{width:36px;height:36px}
.sx-trust{font-size:.74rem}
.sx-bottom{margin-top:1.4rem;gap:1.4rem}
.sx-sec h2{font-size:1.1rem}.sx-sec>p{font-size:.85rem;margin-bottom:.7rem}
.sx-deals,.sx-guides{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;gap:.7rem;margin:0;padding:0 0 6px;scrollbar-width:none}
.sx-deals::-webkit-scrollbar,.sx-guides::-webkit-scrollbar{display:none}
.sx-deal{flex:0 0 76%;scroll-snap-align:start}.sx-deal .ph{height:120px}
.sx-guide{flex:0 0 58%;scroll-snap-align:start}
}
@media(max-width:1000px){.sx{grid-template-columns:1fr}.sx-side{position:static}.sx-deals{grid-template-columns:repeat(2,1fr)}.sx-guides{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.sx-deals{grid-template-columns:1fr}.sx-banner .sx-btn{min-width:0;width:100%}}
</style>"""


def search_page(client) -> str:
    """The full « Rechercher un forfait » home for a logged-in member."""
    from portal_assistant import _engine_url   # lazy: avoids an import cycle
    today = datetime.now(TZ).date()
    with SessionLocal() as db:
        cutoff = datetime.utcnow() - timedelta(days=365)
        rows = (db.query(DmoSearch).filter(DmoSearch.client_id == client.id, DmoSearch.created_at >= cutoff)
                .order_by(DmoSearch.created_at.desc()).limit(12).all())
        db.expunge_all()
    upcoming = [s for s in rows if (d := _date(_fields(s).get("date_dep", ""))) and d >= today]
    resume = upcoming[0] if upcoming else None
    head = (f"<div class='sx-head'><span class='sx-badge'>✓ <span class='lg'>{L('Rabais Récompense activé sur ton compte', 'Reward Discount active on your account')}</span><span class='sm'>{L('Rabais inclus', 'Discount included')}</span></span>"
            f"<h2>{L('Rechercher un forfait', 'Find a package')}</h2>"
            f"<p>{L('Les prix affichés dans le moteur incluent déjà ton rabais. Aucun code, rien à demander.', 'Prices shown in the engine already include your discount. No code, nothing to ask for.')}</p></div>")
    engine = (f"<div class='engine'><iframe src=\"{escape(_engine_url({}))}\" "
              f"title=\"{L('Moteur de recherche de forfaits', 'Package search engine')}\" loading='eager'></iframe></div>")
    main = _season_banner() + _resume_banner(client, resume) + head + engine
    side = _steps_card() + _recent_card(rows[:3]) + _help_card() + _trust_card()
    bottom = "<div class='sx-bottom'>" + _deals_section() + _guides_section() + _faq_section() + "</div>"
    return _CSS + f"<div class='sx'><div>{main}</div><aside class='sx-side'>{side}</aside></div>" + bottom
