"""Portal + admin side of push notifications: subscribe/unsubscribe a device,
the « Notifications » card (profile and installed-app sidebar), and the admin
senders (one member, everyone)."""
from __future__ import annotations

import json
from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import require_admin
from db import Client, PushSubscription, SessionLocal
from i18n_portal import L, LANG
from portal import current_portal_client_id
from portal_passkeys import _device_label
import push

router = APIRouter()


@router.get("/portail/push/key")
def push_key():
    return JSONResponse({"key": push.public_key_b64()})


@router.post("/portail/push/subscribe")
async def push_subscribe(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        sub = json.loads(await request.body())
        endpoint = str(sub["endpoint"])[:1000]
        p256dh = str(sub["keys"]["p256dh"])[:200]
        auth = str(sub["keys"]["auth"])[:100]
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=400)
    if not endpoint.startswith("https://"):
        return JSONResponse({"ok": False}, status_code=400)
    with SessionLocal() as db:
        row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
        if row:
            row.client_id, row.p256dh, row.auth, row.lang = cid, p256dh, auth, LANG.get()
        else:
            db.add(PushSubscription(client_id=cid, endpoint=endpoint, p256dh=p256dh, auth=auth, lang=LANG.get(),
                                    label=_device_label(request.headers.get("user-agent", ""))))
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/portail/push/unsubscribe")
async def push_unsubscribe(request: Request):
    cid = current_portal_client_id(request)
    try:
        endpoint = str(json.loads(await request.body()).get("endpoint", ""))
    except Exception:  # noqa: BLE001
        endpoint = ""
    if cid and endpoint:
        with SessionLocal() as db:
            db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint,
                                              PushSubscription.client_id == cid).delete(synchronize_session=False)
            db.commit()
    return JSONResponse({"ok": True})


@router.post("/portail/push/test")
def push_test(request: Request):
    cid = current_portal_client_id(request)
    if not cid:
        return JSONResponse({"ok": False}, status_code=401)
    sent, failed = push.notify_client(cid, "duvoyageur.ca", "Les notifications sont activées ✓ On te préviendra ici.",
                                      "/portail", "duvoyageur.ca", "Notifications are on ✓ We'll let you know here.")
    return JSONResponse({"ok": sent > 0, "sent": sent, "failed": failed})


_CSS = """<style>
.nf{display:none;background:#fff;border:1px solid var(--line);border-radius:18px;padding:1rem 1.1rem;box-shadow:0 10px 30px -18px rgba(15,42,71,.3);margin:0 0 1.2rem}
.nf.show{display:block}
.nf h3{margin:0 0 .3rem;font:800 1rem Montserrat,sans-serif;color:var(--navy)}
.nf p{margin:0 0 .8rem;color:var(--mist);font:400 .88rem/1.5 Inter,sans-serif}
.nf-row{display:flex;gap:.5rem;flex-wrap:wrap}
.nf button{border:0;border-radius:99px;padding:.65rem 1.1rem;font:800 .86rem Montserrat,sans-serif;cursor:pointer;width:auto;text-transform:none;letter-spacing:0;box-shadow:none}
.nf .on{background:var(--navy);color:#fff}.nf .ghost{background:transparent;border:1px solid rgba(15,42,71,.2);color:var(--mist)}
.nf-st{margin:.6rem 0 0;font:600 .82rem Inter,sans-serif}
.nf-st.ok{color:#1E6B40}.nf-st.err{color:#9B1C1C}
</style>"""


def notif_card() -> str:
    """Enable / disable push on this device. Shown when the browser supports
    push; on iPhone only inside the installed app (Apple's rule)."""
    t = {k: json.dumps(v, ensure_ascii=False) for k, v in {
        "denied": L("Les notifications sont bloquées pour cette app. Active-les dans les Réglages de ton téléphone.",
                    "Notifications are blocked for this app. Turn them on in your phone's Settings."),
        "on": L("Notifications activées sur cet appareil ✓", "Notifications are on for this device ✓"),
        "off": L("Notifications désactivées sur cet appareil.", "Notifications are off for this device."),
        "fail": L("Impossible d'activer les notifications. Réessaie.", "Couldn't turn on notifications. Please try again."),
        "test": L("Notification test envoyée.", "Test notification sent."),
        "testfail": L("La notification test n'a pas pu être envoyée.", "The test notification couldn't be sent."),
    }.items()}
    return _CSS + f"""<div class="nf" id="nfCard"><h3>🔔 {L('Notifications', 'Notifications')}</h3>
<p>{L('Reçois sur ton téléphone la réponse à tes demandes de groupe et les messages importants de ton agence.', 'Get replies to your group requests and important messages from your agency on your phone.')}</p>
<div class="nf-row"><button class="on" id="nfOn" type="button">{L('Activer les notifications', 'Turn on notifications')}</button>
<button class="ghost" id="nfTest" type="button" hidden>{L('Envoyer un test', 'Send a test')}</button>
<button class="ghost" id="nfOff" type="button" hidden>{L('Désactiver', 'Turn off')}</button></div>
<p class="nf-st" id="nfSt" hidden></p></div>
<script>(function(){{
 var c=document.getElementById('nfCard');if(!c)return;
 var ios=/iphone|ipad|ipod/i.test(navigator.userAgent);
 var standalone=matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
 if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window))return;
 if(ios&&!standalone)return;
 c.classList.add('show');
 var on=document.getElementById('nfOn'),off=document.getElementById('nfOff'),test=document.getElementById('nfTest'),st=document.getElementById('nfSt');
 function say(t,ok){{st.textContent=t;st.className='nf-st '+(ok?'ok':'err');st.hidden=false}}
 function u2a(s){{s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';var r=atob(s),a=new Uint8Array(r.length);for(var i=0;i<r.length;i++)a[i]=r.charCodeAt(i);return a}}
 function post(u,b){{return fetch(u,{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b||{{}})}}).then(function(r){{return r.json()}})}}
 function show(subscribed){{on.hidden=subscribed;off.hidden=!subscribed;test.hidden=!subscribed}}
 navigator.serviceWorker.ready.then(function(reg){{return reg.pushManager.getSubscription()}}).then(function(s){{show(!!s);if(Notification.permission==='denied')say({t['denied']},false)}});
 on.onclick=function(){{on.disabled=true;Notification.requestPermission().then(function(p){{
   if(p!=='granted'){{on.disabled=false;say({t['denied']},false);return}}
   return Promise.all([navigator.serviceWorker.ready,fetch('/portail/push/key').then(function(r){{return r.json()}})]).then(function(a){{
     return a[0].pushManager.subscribe({{userVisibleOnly:true,applicationServerKey:u2a(a[1].key)}})}}).then(function(s){{
     return post('/portail/push/subscribe',s.toJSON())}}).then(function(r){{on.disabled=false;if(r&&r.ok){{show(true);say({t['on']},true)}}else say({t['fail']},false)}})
 }}).catch(function(){{on.disabled=false;say({t['fail']},false)}})}};
 off.onclick=function(){{navigator.serviceWorker.ready.then(function(reg){{return reg.pushManager.getSubscription()}}).then(function(s){{
   if(!s)return;var ep=s.endpoint;return s.unsubscribe().then(function(){{return post('/portail/push/unsubscribe',{{endpoint:ep}})}})}}).then(function(){{show(false);say({t['off']},true)}})}};
 test.onclick=function(){{post('/portail/push/test').then(function(r){{say(r&&r.ok?{t['test']}:{t['testfail']},!!(r&&r.ok))}})}};
}})();</script>"""


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def _form(action: str, back: str, button: str) -> str:
    return (f"<form method='post' action='{action}' style='display:grid;gap:8px;max-width:640px'>"
            f"<input type='hidden' name='next' value='{escape(back)}'>"
            "<label>Titre<input name='title' maxlength='60' required value='duvoyageur.ca'></label>"
            "<label>Message (français)<textarea name='body_fr' rows='2' maxlength='180' required></textarea></label>"
            "<label>Message (anglais, optionnel)<textarea name='body_en' rows='2' maxlength='180'></textarea></label>"
            "<label>Ouvre la page (optionnel)<select name='url'><option value='/portail'>Rechercher un forfait</option>"
            "<option value='/portail/recherches'>Mes recherches</option><option value='/portail?tab=groupe'>Voyage de groupe</option>"
            "<option value='/portail/profil'>Mon profil</option></select></label>"
            f"<div><button>{escape(button)}</button></div></form>")


def member_push_card(client_id: int, sent: str = "") -> str:
    with SessionLocal() as db:
        subs = db.query(PushSubscription).filter(PushSubscription.client_id == client_id).all()
    if not subs:
        return ("<div class='card' style='margin-bottom:16px'><h3>🔔 Notifications</h3>"
                "<p class='muted'>Ce membre n'a pas activé les notifications (app non installée ou refusées).</p></div>")
    devices = ", ".join(f"{escape(s.label or 'Appareil')} ({s.lang.upper()})" for s in subs)
    ok = f"<p style='color:#4ade80'>{escape(sent)}</p>" if sent else ""
    return (f"<div class='card' style='margin-bottom:16px'><h3>🔔 Notifications</h3><div class='sub'>{len(subs)} appareil(s) : {devices}</div>"
            + ok + _form(f"/admin/push/client/{client_id}", f"/admin/clients/{client_id}", "Envoyer à ce membre") + "</div>")


def broadcast_card(sent: str = "") -> str:
    with SessionLocal() as db:
        n_dev = db.query(PushSubscription).count()
        n_mem = len({c for (c,) in db.query(PushSubscription.client_id).all()})
    ok = f"<p style='color:#4ade80'>{escape(sent)}</p>" if sent else ""
    return (f"<div class='card' style='margin-top:16px'><h3>🔔 Notifications push</h3>"
            f"<div class='sub'>{n_mem} membre(s) abonné(s) · {n_dev} appareil(s). Seuls les membres qui ont installé l'app "
            "et accepté les notifications les reçoivent.</div>" + ok
            + (_form("/admin/push/all", "/admin/moteur", "Envoyer à tous les abonnés") if n_dev else "") + "</div>")


def _clean(f) -> dict:
    url = f.get("url") if f.get("url") in ("/portail", "/portail/recherches", "/portail?tab=groupe", "/portail/profil") else "/portail"
    return {"title": (f.get("title") or "duvoyageur.ca")[:60], "body_fr": (f.get("body_fr") or "")[:180],
            "body_en": (f.get("body_en") or "")[:180] or None, "url": url}


@router.post("/admin/push/client/{client_id}", dependencies=[Depends(require_admin)])
async def admin_push_client(client_id: int, request: Request):
    v = _clean(await request.form())
    sent, failed = push.notify_client(client_id, v["title"], v["body_fr"], v["url"], v["title"], v["body_en"])
    return RedirectResponse(f"/admin/clients/{client_id}?push={sent}-{failed}", status_code=303)


@router.post("/admin/push/all", dependencies=[Depends(require_admin)])
async def admin_push_all(request: Request):
    v = _clean(await request.form())
    sent, failed = push.notify_all(v["title"], v["body_fr"], v["url"], v["title"], v["body_en"])
    return RedirectResponse(f"/admin/moteur?push={sent}-{failed}", status_code=303)
