import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./t3.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="",PUBLIC_BASE_URL="https://test.example.com",SECURE_COOKIES="")
if os.path.exists("t3.db"): os.remove("t3.db")
import logging; logging.disable(logging.CRITICAL)
import db; db.init_db()
from db import SessionLocal, resolve_or_create_client
import main, portal
from starlette.testclient import TestClient
ok=[]
def ck(n,c): ok.append(c); print(("PASS" if c else "FAIL"),"|",n)
KYC=dict(legal_first_name="Yan",legal_last_name="T",date_of_birth="1990-05-12",address="a",city="Granby",province="QC",postal_code="J2G1A1",country="Canada")
with SessionLocal() as s:
    cl=resolve_or_create_client(s, messenger_psid="P", name="Yan", email="yan@x.com", channel="messenger"); cl.kyc=KYC; s.commit(); cid=cl.id
# GET login page
c=TestClient(main.app)
f=c.get("/portail/connexion").text
ck("connexion: formulaire courriel + date de naissance", "name='email'" in f and "name='dob'" in f and "Me connecter" in f)
# wrong DOB
r=c.post("/portail/connexion", data={"email":"yan@x.com","dob":"2000-01-01"}, follow_redirects=False)
ck("mauvais DOB -> reste sur connexion + erreur", r.status_code==200 and "invalide" in r.text)
ck("mauvais DOB -> pas de cookie session", "dv_portal" not in r.headers.get("set-cookie",""))
# correct
r2=c.post("/portail/connexion", data={"email":"YAN@x.com","dob":"1990-05-12"}, follow_redirects=False)
ck("bon courriel+DOB -> redirige /portail + cookie", r2.status_code==303 and r2.headers.get("location")=="/portail" and "dv_portal" in r2.headers.get("set-cookie",""))
# now logged in -> /portail accessible
ck("connecté: /portail = 200", c.get("/portail",follow_redirects=False).status_code==200)
# already logged in -> connexion redirects to /portail
ck("déjà connecté -> /portail/connexion redirige", c.get("/portail/connexion",follow_redirects=False).status_code==303)
# rate limiter: 6 failed attempts then lockout
c2=TestClient(main.app)
for i in range(6): c2.post("/portail/connexion", data={"email":"no@x.com","dob":"2000-01-01"})
lock=c2.post("/portail/connexion", data={"email":"yan@x.com","dob":"1990-05-12"}, follow_redirects=False)
ck("limiteur: lockout après 6 tentatives (même bonnes infos bloquées)", lock.status_code==200 and "Trop de tentatives" in lock.text)
open("/tmp/connexion.html","w").write(f)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
