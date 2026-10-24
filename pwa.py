"""Progressive Web App for the client portal: manifest, service worker, offline
page, and the head tags / install card used by the portal pages.

Scope is "/" so the Softvoyage results page (/results, reached by a top-level
form post) stays inside the installed app instead of opening a browser view.
The service worker only caches the app shell; the engine, results and any POST
are always fetched live.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from i18n_portal import L, pick

router = APIRouter()

SW_VERSION = "dv-pwa-v2"
NAVY = "#0F2A47"

HEAD_TAGS = (
    '<link rel="manifest" href="/manifest.webmanifest">'
    f'<meta name="theme-color" content="{NAVY}">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="apple-mobile-web-app-title" content="duvoyageur">'
    '<link rel="apple-touch-icon" href="/static/pwa/apple-touch-icon.png">'
)

# Registers the service worker on every portal / results page.
REGISTER_JS = ("<script>if('serviceWorker' in navigator){window.addEventListener('load',function(){"
               "navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){})})}</script>")


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest(request: Request):
    lang, _ = pick(request)
    en = lang == "en"
    data = {
        "id": "/portail",
        "name": "duvoyageur.ca — Espace client" if not en else "duvoyageur.ca — Member area",
        "short_name": "duvoyageur",
        "description": ("Trouve ton forfait et réserve avec ton Rabais Récompense déjà appliqué."
                        if not en else "Find your package and book with your Reward Discount already applied."),
        "lang": "en-CA" if en else "fr-CA",
        "start_url": "/portail?source=pwa" + ("&lang=en" if en else ""),
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": NAVY,
        "theme_color": NAVY,
        "icons": [
            {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/pwa/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Mes recherches" if not en else "My searches", "url": "/portail/recherches"},
            {"name": "Voyage de groupe" if not en else "Group trip", "url": "/portail?tab=groupe"},
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})


_SW = """
const V = '%s';
const SHELL = ['/offline', '/static/logo-capitaine.png', '/static/favicon-64.png',
               '/static/pwa/icon-192.png', '/static/pwa/apple-touch-icon.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'duvoyageur.ca', {
    body: d.body || '', icon: '/static/pwa/icon-192.png', badge: '/static/pwa/icon-192.png',
    tag: d.tag || 'dv', data: {url: d.url || '/portail'}
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/portail', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) { if (c.url.startsWith(self.location.origin) && 'focus' in c) { c.navigate(url); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;                       // never touch POSTs (search, forms)
  const u = new URL(r.url);
  if (u.origin !== location.origin) return;             // Softvoyage, fonts, CDNs: always live
  if (u.pathname.startsWith('/results') || u.pathname.startsWith('/admin')) return;
  if (r.mode === 'navigate') {                          // pages: network first, offline page if no network
    e.respondWith(fetch(r).catch(() => caches.match('/offline')));
    return;
  }
  if (u.pathname.startsWith('/static/')) {              // static assets: cache first
    e.respondWith(caches.match(r).then(hit => hit || fetch(r).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(V).then(c => c.put(r, copy)); }
      return res;
    })));
  }
});
""" % SW_VERSION


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    return Response(_SW, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@router.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline():
    return HTMLResponse(f"""<!doctype html><html lang="fr-CA"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>Hors ligne — duvoyageur.ca</title>{HEAD_TAGS}
<style>html,body{{margin:0;height:100%;background:{NAVY};color:#fff;font-family:Montserrat,system-ui,sans-serif}}
main{{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}}
img{{width:96px;height:96px;margin-bottom:18px}}h1{{margin:0 0 8px;font-size:1.4rem}}p{{margin:0 0 18px;color:rgba(255,255,255,.8);max-width:340px;line-height:1.5}}
button{{border:0;background:#F4C543;color:{NAVY};font:800 1rem Montserrat,system-ui,sans-serif;padding:.8rem 1.4rem;border-radius:99px}}
small{{display:block;margin-top:22px;color:rgba(255,255,255,.6)}}</style></head>
<body><main><img src="/static/logo-capitaine.png" alt=""><h1>Pas de connexion Internet</h1>
<p>Vérifie ta connexion, puis réessaie. Ton Rabais Récompense t'attend.</p>
<button onclick="location.reload()">Réessayer</button><small>No internet connection · Check your connection and try again.</small></main></body></html>""")


def install_card() -> str:
    """Install prompt for the portal sidebar: Android/desktop Chrome use the
    browser's install prompt; iPhone Safari gets the 2-step « Add to Home Screen »
    guide. Hidden when already installed or dismissed."""
    share = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:-3px">'
             '<path d="M12 3v12"/><path d="m8 7 4-4 4 4"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>')
    return f"""<style>
.pwa{{display:none;background:#fff;border:1px solid var(--line);border-radius:18px;padding:1rem 1.05rem;box-shadow:0 10px 30px -18px rgba(15,42,71,.3)}}
.pwa.show{{display:block}}
.pwa-h{{display:flex;gap:.7rem;align-items:center;margin-bottom:.6rem}}
.pwa-h img{{width:44px;height:44px;border-radius:12px;background:var(--navy);flex:none}}
.pwa-h b{{display:block;font:800 .95rem/1.2 Montserrat,sans-serif;color:var(--navy)}}
.pwa-h span{{font:500 .78rem Inter,sans-serif;color:var(--mist)}}
.pwa-btn{{display:block;width:100%;border:0;background:var(--navy);color:#fff;border-radius:99px;padding:.65rem;font:800 .86rem Montserrat,sans-serif;cursor:pointer;text-transform:none;letter-spacing:0;box-shadow:none}}
.pwa-ios{{display:none;list-style:none;margin:0 0 .5rem;padding:0;gap:.45rem}}
.pwa.ios .pwa-ios{{display:grid}}.pwa.ios .pwa-btn{{display:none}}
.pwa-ios li{{display:flex;gap:.55rem;align-items:center;font:500 .84rem/1.35 Inter,sans-serif;color:var(--navy)}}
.pwa-ios i{{font-style:normal;flex:none;width:22px;height:22px;border-radius:50%;background:var(--gold);color:var(--navy);display:grid;place-items:center;font:800 .74rem Montserrat,sans-serif}}
.pwa-x{{display:block;margin:.55rem auto 0;background:none;border:0;color:var(--mist);font:500 .76rem Inter,sans-serif;text-decoration:underline;cursor:pointer;padding:0;box-shadow:none;width:auto}}
</style>
<div class="pwa" id="pwaCard">
 <div class="pwa-h"><img src="/static/pwa/icon-192.png" alt=""><div><b>{L("Installe l'app duvoyageur.ca", "Install the duvoyageur.ca app")}</b>
 <span>{L("Ton moteur et tes recherches, à un toucher de l'écran d'accueil.", "Your engine and searches, one tap from your home screen.")}</span></div></div>
 <ol class="pwa-ios"><li><i>1</i><span>{L("Touche", "Tap")} {share} {L("Partager dans Safari", "Share in Safari")}</span></li>
 <li><i>2</i><span>{L("Choisis « Sur l'écran d'accueil »", "Choose “Add to Home Screen”")}</span></li></ol>
 <button class="pwa-btn" id="pwaBtn" type="button">📲 {L("Installer l'app", "Install the app")}</button>
 <button class="pwa-x" id="pwaX" type="button">{L("Plus tard", "Not now")}</button>
</div>
<script>(function(){{
 var c=document.getElementById('pwaCard');if(!c)return;
 var standalone=matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
 var dismissed=false;try{{dismissed=localStorage.getItem('dvPwaHide')==='1'}}catch(e){{}}
 if(standalone||dismissed)return;
 var ua=navigator.userAgent,ios=/iphone|ipad|ipod/i.test(ua)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
 var safari=ios&&!/crios|fxios|edgios/i.test(ua);
 if(safari){{c.classList.add('ios','show')}}
 var ev=null;window.addEventListener('beforeinstallprompt',function(e){{e.preventDefault();ev=e;c.classList.add('show')}});
 document.getElementById('pwaBtn').onclick=function(){{if(!ev)return;ev.prompt();ev.userChoice.finally(function(){{ev=null;c.classList.remove('show')}})}};
 window.addEventListener('appinstalled',function(){{c.classList.remove('show')}});
 document.getElementById('pwaX').onclick=function(){{c.classList.remove('show');try{{localStorage.setItem('dvPwaHide','1')}}catch(e){{}}}};
}})();</script>"""
