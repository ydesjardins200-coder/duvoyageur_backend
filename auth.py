"""
auth.py
=======
Dead-simple admin gate via HTTP Basic. It's only you two, but the admin reads
real customer data, so it must be a real login — not a secret URL.

Upgrade paths when you outgrow this: Supabase Auth (if you use Supabase), or a
session/JWT login. For two people, Basic over HTTPS is fine to start.
"""
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import settings

_security = HTTPBasic()


def require_admin(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    user_ok = secrets.compare_digest(creds.username, settings.ADMIN_USER)
    pass_ok = bool(settings.ADMIN_PASSWORD) and secrets.compare_digest(
        creds.password, settings.ADMIN_PASSWORD
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Identifiants invalides",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username
