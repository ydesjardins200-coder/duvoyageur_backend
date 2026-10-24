"""Group trips (10+ travellers): the portal « Voyage de groupe » tab and its
admin side. Group requests do not go through the Softvoyage engine: the member
fills a form (children's ages included) and an advisor books manually.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import require_admin
from config import settings
from db import Client, GroupRequest, SessionLocal
from i18n_portal import L, LANG
from portal import current_portal_client_id
from portal_searches import GATEWAYS

log = logging.getLogger(__name__)
router = APIRouter()

MIN_GROUP = 10
MAX_CHILDREN = 30
MAX_ADULTS = 200
STATUSES = [("nouvelle", "Nouvelle"), ("en_cours", "En cours"), ("soumission", "Soumission envoyée"),
            ("reservee", "Réservée"), ("annulee", "Annulée")]
_STATUS_LBL = dict(STATUSES)
GROUP_TYPES = [("famille", "Famille / amis", "Family / friends"), ("mariage", "Mariage", "Wedding"),
               ("entreprise", "Entreprise / incitatif", "Corporate / incentive"),
               ("etudiants", "Étudiants / équipe sportive", "Students / sports team"), ("autre", "Autre", "Other")]
OCCUPANCY = [("", "Aucune préférence", "No preference"), ("simple", "Simple", "Single"), ("double", "Double", "Double"),
             ("triple", "Triple", "Triple"), ("quad", "Quadruple", "Quad")]
BUDGETS = [("", "Pas de limite", "No limit"), ("lt1500", "Moins de 1 500 $", "Under $1,500"),
           ("1500-2500", "1 500 $ à 2 500 $", "$1,500 to $2,500"), ("gt2500", "2 500 $ et +", "$2,500 and up")]
_FIRST = ["YUL", "YQB", "YOW", "YYZ"]


def _gateway_opts(selected: str, blank: str | None = None) -> str:
    codes = _FIRST + sorted((c for c in GATEWAYS if c not in _FIRST), key=lambda c: GATEWAYS[c])
    out = f"<option value=''>{escape(blank)}</option>" if blank is not None else ""
    return out + "".join(f"<option value='{c}'{' selected' if c == selected else ''}>{escape(GATEWAYS[c])} ({c})</option>"
                         for c in codes)


def _opts(items, selected: str) -> str:
    en = LANG.get() == "en"
    return "".join(f"<option value='{k}'{' selected' if k == selected else ''}>{escape(e if en else f)}</option>"
                   for k, f, e in items)


def _age_label(a: str) -> str:
    if a == "0":
        return L("< 2 ans", "< 2 yrs")
    return L(f"{a} ans", f"{a} yrs")


def _client_identity(client) -> tuple[str, str, str]:
    kyc = client.kyc or {}
    parts = (client.display_name or "").split(" ", 1)
    first = kyc.get("legal_first_name") or (parts[0] if parts else "")
    last = kyc.get("legal_last_name") or (parts[1] if len(parts) > 1 else "")
    return first or "", last or "", client.primary_email or ""


# --------------------------------------------------------------------------- #
# Portal form
# --------------------------------------------------------------------------- #
_CSS = """<style>
.gf{background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
.gf-intro{padding:1.1rem 1.3rem;background:linear-gradient(120deg,rgba(244,197,67,.16),rgba(244,197,67,.05));border-bottom:1px solid rgba(15,42,71,.08)}
.gf-intro b{display:block;font:800 1.1rem Montserrat,sans-serif;color:var(--navy)}
.gf-intro span{font:500 .88rem Inter,sans-serif;color:var(--mist)}
.gf-sec{padding:1.1rem 1.3rem;border-bottom:1px solid rgba(15,42,71,.08)}
.gf-sec h4{margin:0 0 .8rem;font:800 .95rem Montserrat,sans-serif;color:var(--navy);display:flex;align-items:center;gap:.5rem}
.gf-sec h4 i{font-style:normal;width:24px;height:24px;border-radius:50%;background:var(--navy);color:#fff;display:grid;place-items:center;font-size:.75rem}
.gf-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.75rem .9rem}
.gf-f{display:flex;flex-direction:column;gap:.3rem;min-width:0}
.gf-f.full{grid-column:1/-1}
.gf-f>label{font:700 .8rem Inter,sans-serif;color:var(--navy)}
.gf-f>label em,.gf-ages b em{color:#B42318;font-style:normal}
.gf-f input,.gf-f select,.gf-f textarea{width:100%;border:1.5px solid rgba(15,42,71,.18);border-radius:10px;padding:.62rem .7rem;font:500 .92rem Inter,sans-serif;color:var(--navy);background:#fff;margin:0}
.gf-f input[readonly]{background:var(--warm);color:var(--mist)}
.gf-f small{font:500 .74rem Inter,sans-serif;color:#8592A0}
.gf-step{display:flex;align-items:center;border:1.5px solid rgba(15,42,71,.18);border-radius:10px;overflow:hidden}
.gf-step button{width:42px;height:42px;flex:none;border:0;background:var(--warm);font:800 1.1rem Montserrat,sans-serif;color:var(--navy);cursor:pointer;padding:0;box-shadow:none}
.gf-step input{border:0!important;text-align:center;font:800 1rem Montserrat,sans-serif!important;border-radius:0!important}
.gf-ages{grid-column:1/-1;background:#FFFBEB;border:1px dashed #C9A227;border-radius:12px;padding:.8rem}
.gf-ages b{display:block;font:700 .82rem Inter,sans-serif;color:#6B4E00;margin-bottom:.5rem}
.gf-ages>div{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:.5rem}
.gf-ages label{font:600 .74rem Inter,sans-serif;color:var(--mist)}
.gf-ages select{width:100%;margin-top:.15rem;border:1.5px solid rgba(15,42,71,.18);border-radius:8px;padding:.45rem;font:600 .85rem Inter,sans-serif;background:#fff;color:var(--navy)}
.gf-total{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:.5rem;background:var(--navy);color:#fff;border-radius:12px;padding:.6rem .9rem;font:600 .86rem Inter,sans-serif}
.gf-total b{color:var(--gold);font:800 1.05rem Montserrat,sans-serif;white-space:nowrap}
.gf-total.warn b{color:#ff9f6b}
.gf-radio{display:flex;gap:.5rem}
.gf-radio label{flex:1;display:flex;align-items:center;justify-content:center;gap:.4rem;border:1.5px solid rgba(15,42,71,.18);border-radius:10px;padding:.55rem;font:600 .86rem Inter,sans-serif;cursor:pointer;color:var(--navy);margin:0}
.gf-radio input{width:auto;margin:0}
.gf-radio label:has(input:checked){border-color:var(--navy);background:var(--navy);color:#fff}
.gf-chk{display:flex;align-items:center;gap:.45rem;font:500 .8rem Inter,sans-serif;color:var(--mist);margin:0}
.gf-chk input{width:auto!important;margin:0}
.gf-count{text-align:right}
.gf-foot{padding:1.1rem 1.3rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;background:#FAFAF7}
.gf-foot p{margin:0;font:500 .8rem/1.45 Inter,sans-serif;color:var(--mist);max-width:460px}
.gf-submit{border:0;background:var(--gold);color:var(--navy);border-radius:99px;padding:.85rem 1.5rem;font:800 .95rem Montserrat,sans-serif;box-shadow:0 4px 0 #C9A227;cursor:pointer;width:auto;text-transform:none;letter-spacing:0}
.gf-err{margin:0 0 1rem;background:#FEF3F2;border:1px solid rgba(180,35,24,.25);color:#9B1C1C;border-radius:12px;padding:.8rem 1rem;font:500 .88rem Inter,sans-serif}
.gf-err ul{margin:.3rem 0 0;padding-left:1.1rem}
.gf-done{background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:2rem 1.4rem;text-align:center}
.gf-done .ic{width:64px;height:64px;margin:0 auto .8rem;border-radius:50%;background:var(--navy);color:var(--gold);display:grid;place-items:center;font-size:1.8rem}
.gf-done h3{margin:0 0 .4rem;font:800 1.3rem Montserrat,sans-serif;color:var(--navy)}
.gf-done p{margin:0 auto 1rem;max-width:480px;color:var(--mist)}
.gs{background:#fff;border:1px solid var(--line);border-radius:18px;padding:1.1rem 1.15rem;box-shadow:0 10px 30px -18px rgba(15,42,71,.3)}
.gs h3{margin:0 0 .7rem;font:800 1rem Montserrat,sans-serif;color:var(--navy)}
.gs .row{display:flex;justify-content:space-between;gap:.6rem;padding:.42rem 0;border-bottom:1px solid rgba(15,42,71,.07);font:500 .84rem Inter,sans-serif}
.gs .row span{color:var(--mist)}.gs .row b{color:var(--navy);text-align:right}
@media(max-width:700px){.gf-grid{grid-template-columns:1fr 1fr}.gf-f.wide-m{grid-column:1/-1}.gf-sec,.gf-intro,.gf-foot{padding:.9rem}.gf-submit{width:100%}}
</style>"""


def group_form(client, values: dict | None = None, errors: list[str] | None = None) -> str:
    v = values or {}
    first, last, email = _client_identity(client)
    g = lambda k, d="": escape(str(v.get(k, d)))  # noqa: E731
    lang = v.get("lang") or LANG.get()
    flexible = v.get("flexible", "oui")
    ages = v.get("ages") or []
    err = ""
    if errors:
        err = (f"<div class='gf-err'><b>{L('Il manque quelques informations :', 'A few details are missing:')}</b><ul>"
               + "".join(f"<li>{escape(e)}</li>" for e in errors) + "</ul></div>")
    ro = lambda val: "readonly" if val else ""  # noqa: E731
    age_opts = "".join(f"<option value='{a}'>{escape(_age_label(str(a)))}</option>" for a in range(0, 18))
    js_ages = "[" + ",".join(f"'{escape(str(a))}'" for a in ages) + "]"
    t = {
        "age": L("Âge", "Age"), "child": L("Enfant", "Child"),
        "total": L("Total du groupe", "Group total"), "min": L("minimum 10 voyageurs", "minimum 10 travellers"),
        "trav": L("voyageur", "traveller"), "travs": L("voyageurs", "travellers"),
    }
    return _CSS + err + f"""
<form class="gf" method="post" action="/portail/groupe" id="gForm" novalidate>
 <div class="gf-intro"><b>{L('Demande de soumission de groupe', 'Group quote request')}</b><span>{L('Mariage, famille, amis, équipe ou entreprise : un conseiller prépare ta soumission, Rabais Récompense inclus.', 'Wedding, family, friends, team or company: an advisor prepares your quote, Reward Discount included.')}</span></div>
 <div class="gf-sec"><h4><i>1</i>{L('Tes coordonnées', 'Your contact details')}</h4>
  <div class="gf-grid">
   <div class="gf-f"><label>{L('Prénom', 'First name')} <em>*</em></label><input name="first_name" value="{escape(v.get('first_name', first))}" {ro(first)} required maxlength="60"></div>
   <div class="gf-f"><label>{L('Nom', 'Last name')} <em>*</em></label><input name="last_name" value="{escape(v.get('last_name', last))}" {ro(last)} required maxlength="60"></div>
   <div class="gf-f wide-m"><label>{L('Courriel', 'Email')} <em>*</em></label><input type="email" name="email" value="{escape(v.get('email', email))}" {ro(email)} required maxlength="120">{f"<small>{L('Repris de ton profil.', 'From your profile.')}</small>" if email else ""}</div>
   <div class="gf-f wide-m"><label>{L('Langue de la soumission', 'Quote language')} <em>*</em></label><div class="gf-radio"><label><input type="radio" name="lang" value="fr"{' checked' if lang != 'en' else ''}>Français</label><label><input type="radio" name="lang" value="en"{' checked' if lang == 'en' else ''}>English</label></div></div>
  </div></div>
 <div class="gf-sec"><h4><i>2</i>{L('Ton groupe', 'Your group')}</h4>
  <div class="gf-grid">
   <div class="gf-f wide-m"><label>{L('Nom du groupe', 'Group name')} <em>*</em></label><input name="group_name" value="{g('group_name')}" placeholder="{L('Ex. : Mariage Julie et Marc', 'E.g. Julie and Marc’s wedding')}" required maxlength="80"></div>
   <div class="gf-f wide-m"><label>{L('Type de groupe', 'Group type')} <em>*</em></label><select name="group_type">{_opts(GROUP_TYPES, v.get('group_type', 'famille'))}</select></div>
   <div class="gf-f"><label>{L('Adultes (18 ans et +)', 'Adults (18+)')} <em>*</em></label><div class="gf-step"><button type="button" data-s="ad" data-d="-1" aria-label="−">−</button><input id="gAd" name="adults" value="{g('adults', '10')}" inputmode="numeric"><button type="button" data-s="ad" data-d="1" aria-label="+">+</button></div></div>
   <div class="gf-f"><label>{L('Enfants (17 ans et −)', 'Children (17 and under)')}</label><div class="gf-step"><button type="button" data-s="ch" data-d="-1" aria-label="−">−</button><input id="gCh" name="children" value="{g('children', '0')}" inputmode="numeric"><button type="button" data-s="ch" data-d="1" aria-label="+">+</button></div></div>
   <div class="gf-ages" id="gAges"><b>{L('Âge de chaque enfant au retour du voyage', 'Age of each child on the return date')} <em>*</em></b><div></div></div>
   <div class="gf-total" id="gTot"><span>{t['total']}</span><b></b></div>
  </div></div>
 <div class="gf-sec"><h4><i>3</i>{L('Le voyage', 'The trip')}</h4>
  <div class="gf-grid">
   <div class="gf-f"><label>{L('Date de départ', 'Departure date')} <em>*</em></label><input type="date" name="date_dep" value="{g('date_dep')}" required></div>
   <div class="gf-f"><label>{L('Date de retour', 'Return date')} <em>*</em></label><input type="date" name="date_ret" value="{g('date_ret')}" required></div>
   <div class="gf-f full"><label>{L('Tes dates sont-elles flexibles ?', 'Are your dates flexible?')} <em>*</em></label><div class="gf-radio"><label><input type="radio" name="flexible" value="oui"{' checked' if flexible != 'non' else ''}>{L('Oui, ± 3 jours', 'Yes, ± 3 days')}</label><label><input type="radio" name="flexible" value="non"{' checked' if flexible == 'non' else ''}>{L('Non, dates fixes', 'No, fixed dates')}</label></div></div>
   <div class="gf-f wide-m"><label>{L('Ville de départ', 'Departure city')} <em>*</em></label><select name="gateway">{_gateway_opts(v.get('gateway', 'YUL'))}</select></div>
   <div class="gf-f wide-m"><label>{L('2e choix de ville de départ', '2nd choice of departure city')}</label><select name="gateway2">{_gateway_opts(v.get('gateway2', ''), L('Aucune préférence', 'No preference'))}</select></div>
   <div class="gf-f wide-m"><label>{L('Destination', 'Destination')} <em>*</em></label><input name="destination" value="{g('destination')}" placeholder="{L('Ex. : Punta Cana, Cancún, Varadero…', 'E.g. Punta Cana, Cancún, Varadero…')}" required maxlength="80"></div>
   <div class="gf-f wide-m"><label>{L('Hôtel souhaité', 'Preferred hotel')}</label><input name="hotel" value="{g('hotel')}" placeholder="{L('Nom complet de l’hôtel', 'Full hotel name')}" maxlength="120"><label class="gf-chk"><input type="checkbox" name="hotel_advice" value="1"{' checked' if v.get('hotel_advice') else ''}> {L('Pas encore choisi : conseillez-moi', 'Not chosen yet: advise me')}</label></div>
   <div class="gf-f wide-m"><label>{L('2e choix d’hôtel', '2nd choice of hotel')}</label><input name="hotel2" value="{g('hotel2')}" placeholder="{L('Optionnel', 'Optional')}" maxlength="120"></div>
   <div class="gf-f wide-m"><label>{L('Occupation des chambres', 'Room occupancy')}</label><select name="occupancy">{_opts(OCCUPANCY, v.get('occupancy', ''))}</select></div>
   <div class="gf-f"><label>{L('Budget par personne', 'Budget per person')}</label><select name="budget">{_opts(BUDGETS, v.get('budget', ''))}</select></div>
   <div class="gf-f"><label>{L('Nombre de chambres (approx.)', 'Number of rooms (approx.)')}</label><input name="rooms" value="{g('rooms')}" inputmode="numeric" maxlength="3"></div>
  </div></div>
 <div class="gf-sec"><h4><i>4</i>{L('Demandes spéciales', 'Special requests')}</h4>
  <div class="gf-f full"><textarea rows="4" maxlength="2000" name="notes" id="gMsg" placeholder="{L('Cérémonie sur la plage, chambres voisines, besoins particuliers, allergies…', 'Beach ceremony, adjoining rooms, special needs, allergies…')}">{g('notes')}</textarea><small class="gf-count" id="gCnt"></small></div></div>
 <div class="gf-foot"><p>{L('Un conseiller te revient par courriel avec une soumission personnalisée. Les prix sont sujets à changement et à la disponibilité au moment de la réservation.', 'An advisor will email you a personalized quote. Prices are subject to change and availability at the time of booking.')}</p><button class="gf-submit" type="submit">{L('Envoyer ma demande de groupe →', 'Send my group request →')}</button></div>
</form>
<script>
(function(){{
 var ad=document.getElementById('gAd'),ch=document.getElementById('gCh'),box=document.getElementById('gAges'),grid=box.querySelector('div'),tot=document.getElementById('gTot');
 var preset={js_ages};var OPTS="<option value=''>{t['age']}</option>{age_opts}";
 function render(){{var n=Math.max(0,Math.min({MAX_CHILDREN},parseInt(ch.value)||0));ch.value=n;
  var cur=[].map.call(grid.querySelectorAll('select'),function(s){{return s.value}});if(!cur.length)cur=preset;
  box.style.display=n?'block':'none';var h='';for(var i=0;i<n;i++)h+="<label>{t['child']} "+(i+1)+"<select name='age_"+(i+1)+"' required>"+OPTS+"</select></label>";grid.innerHTML=h;
  grid.querySelectorAll('select').forEach(function(s,i){{if(cur[i]!==undefined)s.value=cur[i]}});
  var a=Math.max(1,Math.min({MAX_ADULTS},parseInt(ad.value)||1));ad.value=a;var t=a+n;tot.classList.toggle('warn',t<{MIN_GROUP});
  tot.querySelector('span').textContent="{t['total']}"+(t<{MIN_GROUP}?" · {t['min']}":"");tot.querySelector('b').textContent=t+' '+(t>1?"{t['travs']}":"{t['trav']}");
  var s=document.getElementById('gsTrav');if(s)s.textContent=tot.querySelector('b').textContent;}}
 document.querySelectorAll('[data-s]').forEach(function(b){{b.onclick=function(){{var i=b.dataset.s==='ad'?ad:ch;i.value=(parseInt(i.value)||0)+parseInt(b.dataset.d);render()}}}});
 ad.oninput=ch.oninput=render;render();
 var m=document.getElementById('gMsg'),c=document.getElementById('gCnt');function cnt(){{c.textContent=m.value.length+' / 2000'}}m.oninput=cnt;cnt();
}})();
</script>"""


def group_sidebar() -> str:
    steps = [
        (L("Tu envoies ta demande", "You send your request"),
         L("Dates, destination, nombre de voyageurs et âge des enfants.", "Dates, destination, number of travellers and children's ages.")),
        (L("On négocie pour ton groupe", "We negotiate for your group"),
         L("Un conseiller vérifie les disponibilités et les tarifs de groupe auprès des voyagistes.",
           "An advisor checks availability and group rates with the tour operators.")),
        (L("Tu reçois ta soumission", "You receive your quote"),
         L("Par courriel, avec ton Rabais Récompense inclus.", "By email, with your Reward Discount included.")),
        (L("On réserve pour tout le monde", "We book for everyone"),
         L("Et on reste là pour le groupe avant, pendant et après le voyage.", "And we're here for the group before, during and after the trip.")),
    ]
    items = "".join(f"<li><span class='sx-n'>{i}</span><div><b>{escape(a)}</b><small>{escape(b)}</small></div></li>"
                    for i, (a, b) in enumerate(steps, 1))
    return (f"<div class='sx-card sx-navy'><h3>{L('Comment se passe un', 'How a')} <span>{L('voyage de groupe', 'group trip works')}</span></h3>"
            f"<ol class='sx-steps'>{items}</ol></div>")


def group_summary_card() -> str:
    lbl = lambda f, e: escape(L(f, e))  # noqa: E731
    rows = [("gName", lbl("Groupe", "Group")), ("gsTrav", lbl("Voyageurs", "Travellers")),
            ("gsAges", lbl("Âges des enfants", "Children's ages")), ("gsDates", lbl("Dates", "Dates")),
            ("gsDep", lbl("Départ", "Departure")), ("gsDest", lbl("Destination", "Destination"))]
    html = "".join(f"<div class='row'><span>{t}</span><b id='{i}'>—</b></div>" for i, t in rows)
    return (f"<div class='gs'><h3>{L('Résumé de ta demande', 'Your request summary')}</h3>{html}</div>"
            "<script>(function(){var f=document.getElementById('gForm');if(!f)return;"
            "function v(n){var e=f.elements[n];return e?(e.value||'').trim():''}"
            "function sel(n){var e=f.elements[n];return e&&e.selectedIndex>=0?e.options[e.selectedIndex].text:''}"
            "function up(){document.getElementById('gName').textContent=v('group_name')||'—';"
            "var a=[].map.call(f.querySelectorAll('#gAges select'),function(s){return s.value===''?'?':s.options[s.selectedIndex].text});"
            "document.getElementById('gsAges').textContent=a.length?a.join(', '):'—';"
            "var d1=v('date_dep'),d2=v('date_ret');document.getElementById('gsDates').textContent=d1?(d1+(d2?' → '+d2:'')):'—';"
            "document.getElementById('gsDep').textContent=sel('gateway')||'—';document.getElementById('gsDest').textContent=v('destination')||'—'}"
            "f.addEventListener('input',up);f.addEventListener('change',up);f.addEventListener('click',function(){setTimeout(up,0)});up()})();</script>")


def group_done(req_id: int) -> str:
    return (_CSS + f"<div class='gf-done'><div class='ic'>✓</div><h3>{L('Demande de groupe envoyée !', 'Group request sent!')}</h3>"
            f"<p>{L('Merci ! Un conseiller étudie ta demande et te revient par courriel avec une soumission personnalisée.', 'Thank you! An advisor is reviewing your request and will email you a personalized quote.')}</p>"
            f"<p class='muted' style='font-size:.85rem'>{L('Numéro de demande', 'Request number')} : G-{req_id}</p>"
            f"<a class='sx-btn' style='display:inline-block;padding:.7rem 1.3rem' href=\"{escape(settings.MESSENGER_URL)}\" target='_blank' rel='noopener'>"
            f"{L('Une question ? Écris-nous sur Messenger →', 'A question? Message us on Messenger →')}</a></div>")


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


def _parse(form) -> tuple[dict, list[str]]:
    v: dict = {}
    errs: list[str] = []
    s = lambda k, n=200: (form.get(k) or "").strip()[:n]  # noqa: E731
    for k, n in (("first_name", 60), ("last_name", 60), ("email", 120), ("group_name", 80), ("destination", 80),
                 ("hotel", 120), ("hotel2", 120), ("notes", 2000)):
        v[k] = s(k, n)
    v["lang"] = "en" if form.get("lang") == "en" else "fr"
    v["group_type"] = form.get("group_type") if form.get("group_type") in {k for k, *_ in GROUP_TYPES} else "autre"
    v["occupancy"] = form.get("occupancy") if form.get("occupancy") in {k for k, *_ in OCCUPANCY} else ""
    v["budget"] = form.get("budget") if form.get("budget") in {k for k, *_ in BUDGETS} else ""
    v["gateway"] = form.get("gateway") if form.get("gateway") in GATEWAYS else "YUL"
    v["gateway2"] = form.get("gateway2") if form.get("gateway2") in GATEWAYS else ""
    v["flexible"] = "non" if form.get("flexible") == "non" else "oui"
    v["hotel_advice"] = form.get("hotel_advice") == "1"
    v["adults"] = _int(form.get("adults"), 1, MAX_ADULTS, 1)
    v["children"] = _int(form.get("children"), 0, MAX_CHILDREN, 0)
    v["rooms"] = s("rooms", 3) if s("rooms", 3).isdigit() else ""
    ages = []
    for i in range(1, v["children"] + 1):
        a = (form.get(f"age_{i}") or "").strip()
        ages.append(a if a.isdigit() and 0 <= int(a) <= 17 else "")
    v["ages"] = ages
    for k in ("date_dep", "date_ret"):
        try:
            v[k] = date.fromisoformat(s(k, 10)).isoformat()
        except ValueError:
            v[k] = ""
    if not v["first_name"] or not v["last_name"]:
        errs.append(L("ton prénom et ton nom", "your first and last name"))
    if "@" not in v["email"] or "." not in v["email"].split("@")[-1]:
        errs.append(L("un courriel valide", "a valid email"))
    if not v["group_name"]:
        errs.append(L("le nom du groupe", "the group name"))
    if any(a == "" for a in ages):
        errs.append(L("l'âge de chaque enfant", "each child's age"))
    if not v["date_dep"] or not v["date_ret"]:
        errs.append(L("les dates de départ et de retour", "the departure and return dates"))
    elif v["date_ret"] <= v["date_dep"]:
        errs.append(L("une date de retour après la date de départ", "a return date after the departure date"))
    elif date.fromisoformat(v["date_dep"]) < date.today():
        errs.append(L("une date de départ dans le futur", "a departure date in the future"))
    if not v["destination"]:
        errs.append(L("la destination", "the destination"))
    return v, errs


def notify_group(req: GroupRequest) -> None:
    """E-mail the team about a new group request. Not wired yet: no address /
    e-mail service configured (GROUP_NOTIFY_EMAIL). Logged meanwhile."""
    if not settings.GROUP_NOTIFY_EMAIL:
        log.info("Group request G-%s saved (no notification e-mail configured)", req.id)
        return
    log.warning("GROUP_NOTIFY_EMAIL is set but no e-mail sender is configured yet (G-%s)", req.id)


@router.post("/portail/groupe")
async def submit_group(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    form = await request.form()
    v, errs = _parse(form)
    if errs:
        from portal import _bell_panel, _nav, _shell, _set_session_cookie
        from portal_home import search_page
        with SessionLocal() as db:
            client = db.get(Client, cid)
            body = search_page(client, tab="groupe", group_values=v, group_errors=errs)
            bell = _bell_panel(client)
        resp = HTMLResponse(_shell(L("Voyage de groupe", "Group trip"), body, logged_in=True, nav=_nav("accueil"), bell=bell))
        _set_session_cookie(resp, cid)
        return resp
    with SessionLocal() as db:
        req = GroupRequest(client_id=cid, data=v, lang=v["lang"])
        db.add(req)
        db.commit()
        db.refresh(req)
        try:
            from db import log_activity
            log_activity(db, cid, "note", f"Demande de groupe G-{req.id} : {v['group_name']} "
                                          f"({v['adults'] + v['children']} voyageurs, {v['destination']})", None)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        notify_group(req)
        rid = req.id
    return RedirectResponse(f"/portail?tab=groupe&envoye={rid}", status_code=303)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def _summary_rows(d: dict) -> list[tuple[str, str]]:
    gt = {k: f for k, f, _ in GROUP_TYPES}
    occ = {k: f for k, f, _ in OCCUPANCY}
    bud = {k: f for k, f, _ in BUDGETS}
    ages = ", ".join("moins de 2 ans" if a == "0" else f"{a} ans" for a in d.get("ages") or [])
    gw = lambda c: f"{GATEWAYS.get(c, c)} ({c})" if c else "—"  # noqa: E731
    return [
        ("Groupe", f"{d.get('group_name', '')} · {gt.get(d.get('group_type'), '')}"),
        ("Voyageurs", f"{d.get('adults', 0) + d.get('children', 0)} ({d.get('adults', 0)} adultes, {d.get('children', 0)} enfants)"),
        ("Âges des enfants", ages or "—"),
        ("Dates", f"{d.get('date_dep', '')} → {d.get('date_ret', '')} · {'flexibles ± 3 j' if d.get('flexible') == 'oui' else 'fixes'}"),
        ("Départ", gw(d.get("gateway", "")) + (f" · 2e choix : {gw(d.get('gateway2'))}" if d.get("gateway2") else "")),
        ("Destination", d.get("destination", "")),
        ("Hôtel", (d.get("hotel") or "—") + (" · conseiller demandé" if d.get("hotel_advice") else "")
         + (f" · 2e choix : {d.get('hotel2')}" if d.get("hotel2") else "")),
        ("Occupation / chambres", f"{occ.get(d.get('occupancy', ''), '—')} · {d.get('rooms') or '?'} chambre(s)"),
        ("Budget / pers.", bud.get(d.get("budget", ""), "—")),
        ("Contact", f"{d.get('first_name', '')} {d.get('last_name', '')} · {d.get('email', '')} · soumission en {'anglais' if d.get('lang') == 'en' else 'français'}"),
        ("Demandes spéciales", d.get("notes") or "—"),
    ]


def _status_tag(s: str) -> str:
    colors = {"nouvelle": "#F4C543", "en_cours": "#7fd3ff", "soumission": "#c4b5fd", "reservee": "#4ade80", "annulee": "#93a4b8"}
    return (f"<span style='display:inline-block;border-radius:99px;padding:2px 9px;font-size:.76rem;font-weight:700;"
            f"background:rgba(255,255,255,.06);color:{colors.get(s, '#fff')};border:1px solid {colors.get(s, '#fff')}'>"
            f"{escape(_STATUS_LBL.get(s, s))}</span>")


@router.get("/admin/groupes", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_groups(statut: str = ""):
    from main import page_header, render_page
    with SessionLocal() as db:
        q = db.query(GroupRequest)
        if statut in _STATUS_LBL:
            q = q.filter(GroupRequest.status == statut)
        rows = q.order_by(GroupRequest.created_at.desc()).limit(300).all()
        names = {c.id: c.display_name or f"Membre #{c.id}" for c in
                 db.query(Client).filter(Client.id.in_({r.client_id for r in rows})).all()} if rows else {}
        counts = {k: db.query(GroupRequest).filter(GroupRequest.status == k).count() for k, _ in STATUSES}
    tabs = "".join(
        f"<a class='{'on' if statut == k else ''}' href='/admin/groupes?statut={k}'>{escape(lbl)} ({counts[k]})</a>"
        for k, lbl in STATUSES)
    filters = f"<div class='ae-filters'><a class='{'on' if not statut else ''}' href='/admin/groupes'>Toutes</a>{tabs}</div>"
    trs = "".join(
        f"<tr data-href='/admin/groupes/{r.id}'><td><a href='/admin/groupes/{r.id}'><b>G-{r.id}</b></a></td>"
        f"<td>{r.created_at:%Y-%m-%d %H:%M}</td>"
        f"<td><a href='/admin/clients/{r.client_id}'>{escape(names.get(r.client_id, str(r.client_id)))}</a></td>"
        f"<td><b>{escape(r.data.get('group_name', ''))}</b><br><span class='muted'>{escape(r.data.get('destination', ''))} · "
        f"{escape(r.data.get('date_dep', ''))}</span></td>"
        f"<td>{r.data.get('adults', 0) + r.data.get('children', 0)}"
        + (f" <span class='muted'>({r.data.get('children', 0)} enf.)</span>" if r.data.get('children') else "")
        + f"</td><td>{_status_tag(r.status)}</td></tr>"
        for r in rows) or "<tr><td colspan='6' class='muted'>Aucune demande de groupe.</td></tr>"
    from admin_engine import _CSS as AE
    body = (AE + page_header("Demandes de groupe", "/admin/groupes") + filters
            + "<table><tr><th>N°</th><th>Reçue</th><th>Membre</th><th>Groupe</th><th>Voyageurs</th><th>Statut</th></tr>"
            + trs + "</table>")
    return HTMLResponse(render_page(body, "groupes"))


@router.get("/admin/groupes/{gid}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_group_detail(gid: int, ok: int = 0):
    from main import page_header, render_page
    with SessionLocal() as db:
        r = db.get(GroupRequest, gid)
        if not r:
            return HTMLResponse(render_page("<p>Demande introuvable.</p>", "groupes"), status_code=404)
        c = db.get(Client, r.client_id)
        name = (c.display_name if c else None) or f"Membre #{r.client_id}"
        d = dict(r.data or {})
        status, note, created = r.status, r.note or "", r.created_at
    kv = "".join(f"<div class='kv'><span class='k'>{escape(k)}</span><span class='v' style='text-align:right;white-space:pre-wrap'>{escape(str(val))}</span></div>"
                 for k, val in _summary_rows(d))
    opts = "".join(f"<option value='{k}'{' selected' if k == status else ''}>{escape(l)}</option>" for k, l in STATUSES)
    mail = escape(d.get("email", ""))
    body = (
        "<p><a href='/admin/groupes'>&larr; Toutes les demandes de groupe</a></p>"
        + page_header(f"G-{gid} · {d.get('group_name', '')}")
        + ("<p style='color:#4ade80'>✓ Enregistré.</p>" if ok else "")
        + "<div class='grid2'>"
        f"<div class='card'><h3>Demande</h3><div class='sub'>Reçue le {created:%Y-%m-%d %H:%M} · "
        f"<a href='/admin/clients/{r.client_id}'>{escape(name)}</a> · {_status_tag(status)}</div>{kv}</div>"
        "<div class='card'><h3>Suivi</h3>"
        f"<form method='post' action='/admin/groupes/{gid}' style='display:grid;gap:10px'>"
        f"<label>Statut<select name='status'>{opts}</select></label>"
        f"<label>Notes internes<textarea name='note' rows='6' style='width:100%'>{escape(note)}</textarea></label>"
        "<div><button>Enregistrer</button></div></form>"
        f"<p style='margin-top:14px'><a href='mailto:{mail}?subject={escape('Votre demande de groupe G-' + str(gid))}'>✉️ Écrire au client</a> · "
        f"<a href='{escape(settings.MESSENGER_URL)}' target='_blank'>💬 Messenger</a></p></div>"
        "</div>")
    return HTMLResponse(render_page(body, "groupes"))


@router.post("/admin/groupes/{gid}", dependencies=[Depends(require_admin)])
async def admin_group_update(gid: int, request: Request):
    f = await request.form()
    with SessionLocal() as db:
        r = db.get(GroupRequest, gid)
        if r:
            new_status = f.get("status")
            if new_status in _STATUS_LBL:
                if new_status == "soumission" and r.status != "soumission":
                    try:
                        import push
                        push.notify_client(r.client_id, "Ta soumission de groupe est prête",
                                           f"G-{gid} · {r.data.get('group_name', '')} : vérifie tes courriels.",
                                           "/portail?tab=groupe", "Your group quote is ready",
                                           f"G-{gid} · {r.data.get('group_name', '')}: check your email.")
                    except Exception:  # noqa: BLE001 — a push failure never blocks the update
                        pass
                r.status = new_status
            r.note = (f.get("note") or "")[:5000]
            r.updated_at = datetime.utcnow()
            db.commit()
    return RedirectResponse(f"/admin/groupes/{gid}?ok=1", status_code=303)


def member_groups_card(client_id: int) -> str:
    with SessionLocal() as db:
        rows = (db.query(GroupRequest).filter(GroupRequest.client_id == client_id)
                .order_by(GroupRequest.created_at.desc()).limit(10).all())
    if not rows:
        return ""
    trs = "".join(
        f"<tr><td><a href='/admin/groupes/{r.id}'>G-{r.id}</a></td><td>{escape(r.data.get('group_name', ''))}<br>"
        f"<span class='muted'>{escape(r.data.get('destination', ''))} · {escape(r.data.get('date_dep', ''))} · "
        f"{r.data.get('adults', 0) + r.data.get('children', 0)} voyageurs</span></td><td>{_status_tag(r.status)}</td></tr>"
        for r in rows)
    return f"<div class='card' style='margin-bottom:16px'><h3>👥 Demandes de groupe</h3><table>{trs}</table></div>"


def new_groups_count() -> int:
    with SessionLocal() as db:
        return db.query(GroupRequest).filter(GroupRequest.status == "nouvelle").count()
