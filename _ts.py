import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./ts.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="",PUBLIC_BASE_URL="https://test.example.com",SECURE_COOKIES="")
if os.path.exists("ts.db"): os.remove("ts.db")
import logging; logging.disable(logging.CRITICAL)
import db; db.init_db()
from db import SessionLocal, Client, ClientIdentity
import main, portal
from starlette.testclient import TestClient
ok=[]
def ck(n,c): ok.append(c); print(("PASS" if c else "FAIL"),"|",n)
FULL=dict(legal_first_name="Marie",legal_last_name="Gagnon",date_of_birth="1992-03-08",
  phone="514-555-0142",email="marie@example.com",address="50 rue des Lilas",city="Sherbrooke",
  province="QC",postal_code="J1H 2K3",country="Canada")
c=TestClient(main.app)
# form renders with all KYC fields
f=c.get("/portail/inscription").text
ck("inscription: formulaire avec tous les champs KYC", all((f"name='{k}'" in f) for k in FULL) and "Créer mon compte" in f)
ck("connexion: lien vers l'inscription", "/portail/inscription" in c.get("/portail/connexion").text)
# missing field -> error, no creation
r=c.post("/portail/inscription", data={**FULL,"city":""}, follow_redirects=False)
ck("champ manquant -> erreur, pas de redirect", r.status_code==200 and "champs requis" in r.text)
# full signup -> creates + logs in
r2=c.post("/portail/inscription", data=FULL, follow_redirects=False)
ck("inscription complète -> redirige /portail + cookie", r2.status_code==303 and r2.headers.get("location")=="/portail" and "dv_portal" in r2.headers.get("set-cookie",""))
with SessionLocal() as s:
    cl=s.query(Client).filter_by(primary_email="marie@example.com").first()
    ck("client créé avec KYC complet", cl is not None and portal.kyc_complete(cl))
    ck("display_name = nom légal", cl.display_name=="Marie Gagnon")
    ck("identités courriel + téléphone", sorted((i.kind,i.value) for i in s.query(ClientIdentity).filter_by(client_id=cl.id).all())==[("email","marie@example.com"),("phone","+15145550142")])
# logged in -> Accueil (KYC complete, not gated to profile)
ck("connecté direct sur Accueil (200)", c.get("/portail",follow_redirects=False).status_code==200)
ck("Accueil affiché (identité)", "Mon identité" in c.get("/portail").text)
# duplicate email signup blocked
c2=TestClient(main.app)
dup=c2.post("/portail/inscription", data=FULL, follow_redirects=False)
ck("courriel déjà utilisé -> bloqué", dup.status_code==200 and "déjà un compte" in dup.text)
# already logged in -> redirect
ck("déjà connecté -> /portail/inscription redirige", c.get("/portail/inscription",follow_redirects=False).status_code==303)
open("/tmp/signup.html","w").write(f)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
