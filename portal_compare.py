"""
portal_compare.py
=================
« Comparer des tout-inclus » — members pick 2 to 4 hotels from our curated
catalogue (data/hotels.json) and see them side by side: our photo, TripAdvisor
rating and review count, categories, rooms / restaurants / pools, price tier,
our verdict, then a button to the search engine for current prices.

URL: /portail/comparer?h=<id>,<id>…  (the assistant's cards link here).
Everything is rendered client-side from an embedded, escaped JSON snapshot.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import hotel_catalog as hc
from db import Client, SessionLocal
from portal import _logged_out, _nav, _set_session_cookie, _shell, current_portal_client_id

router = APIRouter()
MAX = 4


def _snapshot() -> list:
    rows = []
    for h in hc.hotels():
        rows.append({
            "id": h["id"], "name": h["name"], "dest": h.get("destination", ""),
            "country": h.get("country", ""), "rating": h.get("rating"),
            "reviews": h.get("reviews"), "cats": [hc.CATEGORY_LABELS.get(c, c) for c in h.get("categories", [])],
            "rooms": h.get("chambres"), "restos": h.get("restos"), "pools": h.get("piscines"),
            "year": h.get("annee"), "tier": h.get("price_tier"),
            "cdc": bool(h.get("coup_de_coeur")), "photo": (h.get("photos") or [""])[0],
            "page": h.get("page") or "", "ta": h.get("tripadvisor_url") or "",
            "desc": h.get("description", ""), "q": round(hc.quality(h), 2),
        })
    return rows


_JS = r"""
(function(){
 var DATA=JSON.parse(document.getElementById('hdata').textContent),MAX=4;
 var byId={};DATA.forEach(function(h){byId[h.id]=h;});
 var sel=(new URLSearchParams(location.search).get('h')||'').split(',').filter(function(x){return byId[x];}).slice(0,MAX);
 var q=document.getElementById('cq'),res=document.getElementById('cres'),tbl=document.getElementById('ctbl'),
     chips=document.getElementById('csel');
 function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e;}
 function fmt(n){return n==null?'':String(n).replace(/\B(?=(\d{3})+(?!\d))/g,' ');}
 function norm(s){return (s||'').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g,'');}
 function sync(){var u=new URL(location.href);if(sel.length)u.searchParams.set('h',sel.join(','));else u.searchParams.delete('h');
   history.replaceState(null,'',u);}
 function renderSel(){chips.innerHTML='';
   sel.forEach(function(id){var b=el('button','chip csel',byId[id].name+'  ✕');b.type='button';
     b.onclick=function(){sel=sel.filter(function(x){return x!==id;});draw();};chips.appendChild(b);});
   var n=el('div','cnote',sel.length<2?'Choisis au moins 2 hôtels (maximum '+MAX+').':
     sel.length+' hôtels comparés'+(sel.length<MAX?' — tu peux en ajouter '+(MAX-sel.length)+'.':'.'));
   chips.appendChild(n);}
 function renderRes(){res.innerHTML='';var t=norm(q.value.trim());
   var list=DATA.filter(function(h){return sel.indexOf(h.id)<0&&(!t||norm(h.name+' '+h.dest+' '+h.country+' '+h.cats.join(' ')).indexOf(t)>=0);})
     .sort(function(a,b){return b.q-a.q;}).slice(0,t?12:8);
   if(!t)res.appendChild(el('div','cnote','Les mieux notés — ou tape une destination, un hôtel, « famille », « adultes »…'));
   list.forEach(function(h){var b=el('button','cpick');b.type='button';b.disabled=sel.length>=MAX;
     b.appendChild(el('b',null,h.name));
     b.appendChild(el('span',null,h.dest+(h.rating?' · ★ '+String(h.rating).replace('.',','):'')));
     b.onclick=function(){if(sel.length<MAX){sel.push(h.id);q.value='';draw();}};res.appendChild(b);});
   if(t&&!list.length)res.appendChild(el('div','cnote','Aucun hôtel trouvé dans notre sélection.'));}
 function renderTable(){tbl.innerHTML='';if(sel.length<2)return;
   var hs=sel.map(function(id){return byId[id];});
   var bestR=Math.max.apply(null,hs.map(function(h){return h.rating||0;}));
   var bestN=Math.max.apply(null,hs.map(function(h){return h.reviews||0;}));
   var t=el('table','ctable'),head=el('tr');head.appendChild(el('th'));
   hs.forEach(function(h){var th=el('th');
     if(h.photo){var im=el('img','cimg');im.src=h.photo;im.alt=h.name;im.loading='lazy';th.appendChild(im);}
     else th.appendChild(el('div','cimg cnoimg','Photo à venir'));
     th.appendChild(el('div','cname',h.name));if(h.cdc)th.appendChild(el('div','ccdc','⭐ Notre coup de cœur'));head.appendChild(th);});
   var thead=el('thead');thead.appendChild(head);t.appendChild(thead);
   var rows=[
     ['Destination',function(h){return h.dest+(h.country&&h.dest.indexOf(h.country.split(' · ')[0])<0?' — '+h.country:'');}],
     ['Note TripAdvisor',function(h){return h.rating?'★ '+h.rating.toFixed(1).replace('.',',')+' / 5':'Pas encore notée';},function(h){return h.rating&&h.rating===bestR;}],
     ['Nombre d’avis',function(h){return h.reviews?fmt(h.reviews):'—';},function(h){return h.reviews&&h.reviews===bestN;}],
     ['Pour qui',function(h){return h.cats.join(', ')||'—';}],
     ['Chambres',function(h){return h.rooms?fmt(h.rooms):'—';}],
     ['Restaurants',function(h){return h.restos||'—';}],
     ['Piscines',function(h){return h.pools||'—';}],
     ['Niveau de prix',function(h){return h.tier?'$$$'.slice(0,h.tier):'—';}],
     ['Notre avis',function(h){return h.desc||'—';}]];
   var tb=el('tbody');
   rows.forEach(function(r){var tr=el('tr');tr.appendChild(el('th',null,r[0]));
     hs.forEach(function(h){var td=el('td',r[2]&&r[2](h)?'best':null,r[1](h));tr.appendChild(td);});tb.appendChild(tr);});
   var tr=el('tr');tr.appendChild(el('th',null,''));
   hs.forEach(function(h){var td=el('td'),box=el('div','clinks');td.appendChild(box);
     var go=el('a','pc-go','Voir les prix →');go.href='/portail/recherche';box.appendChild(go);
     if(h.page){var a=el('a',null,'Notre fiche');a.href=h.page;a.target='_blank';a.rel='noopener';box.appendChild(a);}
     if(h.ta){var b=el('a',null,'Avis TripAdvisor ↗');b.href=h.ta;b.target='_blank';b.rel='noopener nofollow';box.appendChild(b);}
     tr.appendChild(td);});
   tb.appendChild(tr);t.appendChild(tb);
   var wrap=el('div','cwrap2');wrap.appendChild(t);tbl.appendChild(wrap);
   tbl.appendChild(el('p','cnote','Notes TripAdvisor relevées sur nos pages : vérifie les avis récents avant de réserver. Les prix, avec ton Rabais Récompense, sont dans notre moteur de recherche.'));}
 function draw(){sync();renderSel();renderRes();renderTable();}
 q.addEventListener('input',renderRes);draw();
})();
"""


@router.get("/portail/comparer", response_class=HTMLResponse)
def compare_hotels(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return RedirectResponse("/portail/connexion", status_code=303)
    with SessionLocal() as db:
        if not db.get(Client, cid):
            return _logged_out()
    data = json.dumps(_snapshot(), ensure_ascii=False).replace("</", "<\\/")
    body = (
        "<div class='hello'><h2>Comparer des tout-inclus</h2>"
        "<p class='lede'>Choisis 2 à 4 hôtels de notre sélection et compare-les côte à côte : "
        "notes, avis, pour qui, installations. Les prix sont dans notre moteur, avec ton "
        "Rabais Récompense.</p></div>"
        "<div class='fset'><input id='cq' class='cq' type='search' autocomplete='off' "
        "placeholder='Destination, hôtel, « famille », « adultes »…' aria-label='Chercher un hôtel'>"
        "<div id='csel' class='csels'></div><div id='cres' class='cres'></div></div>"
        "<div id='ctbl'></div>"
        "<style>"
        ".cq{width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:var(--field);"
        "color:var(--foam);font:inherit;font-size:16px;margin-bottom:12px}"
        ".csels{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}"
        ".csel{border-color:var(--pacific)!important}"
        ".cres{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}"
        ".cpick{text-align:left;padding:10px 12px;border-radius:12px;border:1px solid var(--line);"
        "background:rgba(3,18,27,.55);color:var(--foam);cursor:pointer;font:inherit}"
        ".cpick:hover{border-color:var(--pacific)}.cpick:disabled{opacity:.45;cursor:default}"
        ".cpick b{display:block;font-size:14px}.cpick span{font-size:12px;color:var(--mist)}"
        ".cnote{font-size:12.5px;color:var(--mist);margin:4px 0}"
        ".cwrap2{overflow-x:auto;border:1px solid var(--line);border-radius:16px;background:rgba(3,18,27,.55);margin-top:16px}"
        ".ctable{border-collapse:collapse;width:100%;min-width:560px;font-size:14px}"
        ".ctable th,.ctable td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}"
        ".ctable tbody th{color:var(--mist);font-weight:500;white-space:nowrap;width:1%}"
        ".ctable thead th{vertical-align:bottom}"
        ".ctable td.best{color:var(--lagoon);font-weight:600}"
        ".cimg{width:100%;max-width:240px;aspect-ratio:4/3;object-fit:cover;border-radius:10px;display:block;margin-bottom:8px}"
        ".cnoimg{display:flex;align-items:center;justify-content:center;background:rgba(155,246,236,.06);"
        "color:var(--mist);font-size:12px;font-weight:400}"
        ".cname{font-family:'Bricolage Grotesque',sans-serif;font-size:16px}"
        ".ccdc{font-size:12px;color:var(--gold);font-weight:500;margin-top:2px}"
        ".clinks{display:flex;flex-direction:column;gap:6px;align-items:flex-start}"
        ".clinks a:not(.pc-go){font-size:13px;color:var(--pacific)}"
        "</style>"
        f"<script type='application/json' id='hdata'>{data}</script>"
        f"<script>{_JS}</script>")
    resp = HTMLResponse(_shell("Comparer", body, logged_in=True, nav=_nav("comparer")))
    _set_session_cookie(resp, cid)
    return resp
