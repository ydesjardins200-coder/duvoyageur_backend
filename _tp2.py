import os
os.environ.update(ADMIN_USER="a",ADMIN_PASSWORD="p",SECRET_KEY="k"*40,DATABASE_URL="sqlite:///./tp2.db",
                  ANTHROPIC_API_KEY="",FB_PAGE_TOKEN="x",PUBLIC_BASE_URL="https://test.example.com",SECURE_COOKIES="")
if os.path.exists("tp2.db"): os.remove("tp2.db")
import logging; logging.disable(logging.CRITICAL)
import db; db.init_db()
from db import SessionLocal, Case, Client, resolve_or_create_client
import main, portal
with SessionLocal() as s:
    cl=resolve_or_create_client(s, messenger_psid="P", name="Yan", channel="messenger"); s.commit()
    s.add(Case(client_id=cl.id,channel="messenger",kind="trip",status="quoted",sender_ref="P",
           trip={"customer_name":"Yan","destination":"Punta Cana","hotel_name_raw":"Riu Bambu","num_adults":2},
           quote_url="https://q/x",savings="195 $",needs_clarification=[],screenshots=[],messages=[])); s.commit()
    # also a booked one to verify booking_ref + a new one
    s.add(Case(client_id=cl.id,channel="messenger",kind="trip",status="booked",sender_ref="P",booking_ref="TB-7788",
           trip={"customer_name":"Yan","destination":"Cancún","num_adults":2},savings="240 $",
           flight_depart="2026-02-10",flight_return="2026-02-17",needs_clarification=[],screenshots=[],messages=[])); s.commit()
    cid=cl.id
url=portal.build_portal_login_url(cid); token=url.split("token=",1)[1]
from starlette.testclient import TestClient
c=TestClient(main.app); c.get(f"/portail/login?token={token}")
d=c.get("/portail").text
ok=[]
def ck(n,v): ok.append(v); print(("PASS" if v else "FAIL"),"|",n)
ck("hôtel (titre) + destination affichés", "Riu Bambu" in d and "Punta Cana" in d)
ck("booked: n° de confirmation visible", "TB-7788" in d and "Cancún" in d)
ck("badges client-friendly", "Soumission prête" in d and "Réservé" in d)
open("/tmp/portal.html","w").write(d)
print("\nRESULT:", "OK" if all(ok) else "ECHEC")
