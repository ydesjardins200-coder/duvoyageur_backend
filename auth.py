"""
auth.py
=======
Admin auth backed by a real login FORM (see /admin/login in main.py), not the
browser's HTTP Basic popup. Credentials come from ADMIN_USER / ADMIN_PASSWORD;
the logged-in state is kept in a signed session cookie (SECRET_KEY), so the
admin stays logged in across pages until logout or expiry.

Upgrade paths when you outgrow this: Supabase Auth, or per-user accounts with
hashed passwords in the DB. For two people, a shared login over HTTPS is fine.
"""
import secrets

from fastapi import Request

from config import settings


class NotAuthenticated(Exception):
    """Raised by require_admin when there is no valid admin session.

    main.py registers an exception handler that turns this into a redirect to
    the login page, so protected pages bounce visitors to /admin/login.
    """


def check_credentials(username: str, password: str) -> bool:
    """Constant-time comparison of submitted credentials against settings.

    A blank ADMIN_PASSWORD never authenticates (fail closed in prod).
    """
    user_ok = secrets.compare_digest(username or "", settings.ADMIN_USER)
    pass_ok = bool(settings.ADMIN_PASSWORD) and secrets.compare_digest(
        password or "", settings.ADMIN_PASSWORD
    )
    return user_ok and pass_ok


def require_admin(request: Request) -> bool:
    """FastAPI dependency: allow only requests carrying a valid admin session."""
    if not request.session.get("admin"):
        raise NotAuthenticated()
    return True
