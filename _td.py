import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./td.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="",PUBLIC_BASE_URL="https://test.example.com",SECURE_COOKIES="")
if os.path.exists("td.db"): os.remove("td.db")
import logging; logging.disable(logging.CRITICAL)
import db; db.init_db()
from db import SessionLocal, Case, resolve_or_create_client
import main, portal
from starlette.testclient import TestClient
ok=[]
def ck(n,c): ok.append(c); print(("PASS" if c else "FAIL"),"|",n)
KYC=dict(legal_first_name="Yan",legal_last_name="Tremblay",date_of_birth="1990-05-12",address="12 rue",city="Granby",province="QC",postal_code="J2G1A1",country="Canada")
with SessionLocal() as s:
    cl=resolve_or_create_client(s, messenger_psid="P", name="Yan", email="y@x.com", phone="514", channel="messenger"); cl.kyc=KYC; s.commit(); cid=cl.id
    s.add(Case(client_id=cid,channel="portal",kind="support",status="open",awaiting_reply=True,
       raw_message="Sujet : Changement de dates\nJe veux partir une semaine plus tard.",
       trip={"customer_name":"Yan"},needs_clarification=[],screenshots=[],
       messages=[{"dir":"in","text":"Sujet : Changement de dates\nJe veux partir une semaine plus tard.","at":"2026-06-01T10:00:00"},
                 {"dir":"out","text":"Bonjour Yan, aucun problème ! Quelle date vises-tu ?","at":"2026-06-01T11:30:00"}])); s.commit()
    sid=s.query(Case).filter_by(kind="support").first().id
c=TestClient(main.app); c.post("/portail/login", data={"token":portal.build_portal_login_url(cid).split("token=",1)[1]})
aide=c.get("/portail/service").text
ck("Aide: formulaire toujours présent", "Ton message" in aide and "name='message'" in aide)
ck("Aide: section 'Tes demandes' + toggle", "Tes demandes" in aide and "<details class='thread'>" in aide)
ck("Aide: fil contient les 2 messages (in+out)", "plus tard" in aide and "Quelle date vises-tu" in aide)
ck("Aide: bulle 'Du Voyageur' pour la réponse admin", "Du Voyageur ·" in aide and "msg out" in aide)
ck("Aide: badge 'En cours'", "En cours" in aide)
# client adds another message -> appended to same thread
c.post("/portail/service", data={"message":"Le 17 février svp"})
aide2=c.get("/portail/service").text
ck("Aide: nouveau message client ajouté au fil", "17 février" in aide2)
open("/tmp/aide.html","w").write(aide2)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
