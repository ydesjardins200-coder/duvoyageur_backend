import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./tid.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="",ACK_DEBOUNCE_SECONDS="0")
if os.path.exists("tid.db"): os.remove("tid.db")
import logging; logging.disable(logging.ERROR)
import db; db.init_db()
from db import SessionLocal, Case, Client, ClientIdentity, resolve_or_create_client, log_activity
import main
ok=[]
def ck(n,c): ok.append(c); print(("PASS" if c else "FAIL"),"|",n)
with SessionLocal() as s:
    cl=resolve_or_create_client(s, messenger_psid="P1", name="Yan Perso", channel="messenger"); s.commit()
    cid=cl.id
    s.add(Case(client_id=cid,channel="messenger",kind="trip",status="new",parse_confidence=0.7,
               trip={"customer_name":"Yan"},needs_clarification=[],screenshots=[],messages=[])); s.commit()
    log_activity(s,cid,"request_created","Nouvelle demande via Messenger", None); s.commit()

from starlette.testclient import TestClient
with TestClient(main.app) as c:
    c.post("/admin/login",data={"username":"a","password":"p"})
    base=f"/admin/clients/{cid}"
    p=c.get(base)
    ck("layout idtab 50/50 present", "class='idtab'" in p.text)
    ck("bouton Éditer present", "idEdit(true)" in p.text and "Éditer" in p.text)
    ck("vue (idview) + form edit (idedit)", "id='idview'" in p.text and "id='idedit'" in p.text)
    ck("form edit a courriel/telephone/canal", all(x in p.text for x in ["name='primary_email'","name='primary_phone'","name='preferred_channel'"]))
    ck("activite scrollable (act-scroll)", "act-scroll" in p.text and "Activité" in p.text)
    ck("PAS de notes/tags", "name='notes'" not in p.text and "name='tags'" not in p.text)
    # voyage tab no longer shows the activity timeline
    v=c.get(f"{base}?tab=voyage")
    ck("onglet voyage: pas de timeline activite", "act-scroll" not in v.text)
    # update email + phone + channel
    c.post(f"{base}/update", data={"display_name":"Yan P.","primary_email":"YAN@TASOLUTION.CA ","primary_phone":"819 446 7733","preferred_channel":"email"})
    with SessionLocal() as s:
        u=s.get(Client,cid)
        ck("courriel mis a jour (normalise)", u.primary_email=="yan@tasolution.ca")
        ck("telephone mis a jour (normalise E.164)", u.primary_phone=="+18194467733")
        ck("canal prefere mis a jour", u.preferred_channel=="email")
        idv={i.kind:i.value for i in s.query(ClientIdentity).filter_by(client_id=cid).all()}
        ck("identifiant courriel ajoute", idv.get("email")=="yan@tasolution.ca")
        ck("identifiant telephone ajoute", idv.get("phone")=="+18194467733")
    open("/tmp/idtab.html","w").write(c.get(base).text)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
