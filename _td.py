import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./td.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="",PUBLIC_BASE_URL="https://test.example.com",SECURE_COOKIES="")
if os.path.exists("td.db"): os.remove("td.db")
import logging; logging.disable(logging.CRITICAL)
import db; db.init_db()
from db import SessionLocal, Client, resolve_or_create_client
import main, portal
from starlette.testclient import TestClient
ok=[]
def ck(n,c): ok.append(c); print(("PASS" if c else "FAIL"),"|",n)
KYC=dict(legal_first_name="Yan",legal_last_name="Desjardins",date_of_birth="1981-06-28",
  address="116 rue",city="Granby",province="QC",postal_code="J0E2A0",country="Canada")
with SessionLocal() as s:
    cl=resolve_or_create_client(s, messenger_psid="P", name="Yan", email="yan@x.com", phone="514-555-0199", channel="messenger"); cl.kyc=KYC; s.commit(); cid=cl.id
c=TestClient(main.app); c.post("/portail/login", data={"token":portal.build_portal_login_url(cid).split("token=",1)[1]})
# profile: 3 dropdowns, pas de input type=date, valeurs pré-sélectionnées
pf=c.get("/portail/profil").text
ck("profil: 3 dropdowns DOB (dob_d/m/y)", "name='dob_d'" in pf and "name='dob_m'" in pf and "name='dob_y'" in pf)
ck("profil: plus d'input type=date", "type='date'" not in pf)
ck("profil: valeurs DOB pré-sélectionnées (1981/06/28)", "value='1981' selected" in pf and "value='06' selected" in pf and "value='28' selected" in pf)
# modifier le DOB via dropdowns
c.post("/portail/profil", data={"legal_first_name":"Yan","legal_last_name":"Desjardins","dob_d":"15","dob_m":"03","dob_y":"1990",
  "phone":"514-555-0199","email":"yan@x.com","address":"116 rue","city":"Granby","province":"QC","postal_code":"J0E2A0","country":"Canada"})
with SessionLocal() as s:
    ck("profil POST: date reconstruite -> 1990-03-15", (s.get(Client,cid).kyc or {}).get("date_of_birth")=="1990-03-15")
# login via dropdowns
c2=TestClient(main.app)
lf=c2.get("/portail/connexion").text
ck("connexion: 3 dropdowns (plus d'input date)", "name='dob_d'" in lf and "type='date'" not in lf)
r=c2.post("/portail/connexion", data={"email":"yan@x.com","dob_d":"15","dob_m":"03","dob_y":"1990"}, follow_redirects=False)
ck("connexion via dropdowns -> cookie + /portail", r.status_code==303 and "dv_portal" in r.headers.get("set-cookie",""))
# signup via dropdowns
c3=TestClient(main.app)
sf=c3.get("/portail/inscription").text
ck("inscription: 3 dropdowns DOB", "name='dob_d'" in sf and "name='dob_y'" in sf and "type='date'" not in sf)
c3.post("/portail/inscription", data={"legal_first_name":"Marie","legal_last_name":"Roy","dob_d":"08","dob_m":"11","dob_y":"1995",
  "phone":"514-555-0123","email":"marie2@x.com","address":"5 rue","city":"Laval","province":"QC","postal_code":"H7A1B2","country":"Canada"}, follow_redirects=False)
with SessionLocal() as s:
    m=s.query(Client).filter_by(primary_email="marie2@x.com").first()
    ck("inscription POST: DOB reconstruit -> 1995-11-08", m is not None and (m.kyc or {}).get("date_of_birth")=="1995-11-08")
open("/tmp/dob_signup.html","w").write(sf)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
