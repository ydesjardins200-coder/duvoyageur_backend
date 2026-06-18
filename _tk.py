import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./tk.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="",PUBLIC_BASE_URL="https://test.example.com",SECURE_COOKIES="")
if os.path.exists("tk.db"): os.remove("tk.db")
import logging; logging.disable(logging.CRITICAL)
import db; db.init_db()
from db import SessionLocal, Case, Client, resolve_or_create_client
import main, portal
ok=[]
def ck(n,c): ok.append(c); print(("PASS" if c else "FAIL"),"|",n)
with SessionLocal() as s:
    cl=resolve_or_create_client(s, messenger_psid="P", name="Yan", channel="messenger"); s.commit()
    s.add(Case(client_id=cl.id,channel="messenger",kind="trip",status="quoted",sender_ref="P",
           trip={"customer_name":"Yan","destination":"Punta Cana"},quote_url="https://q/x",savings="195 $",
           needs_clarification=[],screenshots=[],messages=[])); s.commit()
    cid=cl.id
url=portal.build_portal_login_url(cid); token=url.split("token=",1)[1]
from starlette.testclient import TestClient
c=TestClient(main.app); c.post("/portail/login", data={"token":token})
c.post("/portail/profil", data={
  "legal_first_name":"Yan","legal_last_name":"Tremblay","date_of_birth":"1990-05-12",
  "phone":"514-555-0199","email":"yan@example.com","address":"12 rue Principale",
  "city":"Granby","province":"QC","postal_code":"J2G 1A1","country":"Canada",
  "passport_number":"HJ123456","passport_expiry":"2030-01-01"})
with SessionLocal() as s:
    st=portal.kyc_status(s.get(Client,cid)); ck("complétion: plus aucun requis manquant", st[2]==[] and st[0]==st[1])
ck("dashboard: bannière disparue une fois complet", "Complète ton identité" not in c.get("/portail").text)
ck("profil: note 'enregistré' après save", "enregistré" in c.get("/portail/profil?saved=1").text.lower())
# render both pages
import re
with SessionLocal() as s:
    cl2=resolve_or_create_client(s, messenger_psid="Q", name="", channel="messenger"); s.commit()
    s.add(Case(client_id=cl2.id,channel="messenger",kind="trip",status="quoted",sender_ref="Q",
           trip={"customer_name":"Marie","destination":"Cancún","hotel_name_raw":"Riu","num_adults":2},
           quote_url="https://q/y",savings="240 $",needs_clarification=[],screenshots=[],messages=[])); s.commit()
    cid2=cl2.id
url2=portal.build_portal_login_url(cid2); t2=url2.split("token=",1)[1]
c2=TestClient(main.app); c2.post("/portail/login", data={"token":t2})
open("/tmp/p_dash.html","w").write(c2.get("/portail").text)
open("/tmp/p_prof.html","w").write(c2.get("/portail/profil").text)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
