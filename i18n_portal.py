"""
i18n_portal.py — langue du portail (fr par défaut, en).

La langue vient de ?lang=en|fr (les liens du site anglais ajoutent ?lang=en),
sinon du témoin dv_lang posé à la première visite. Le middleware (main.py)
la place dans un ContextVar pour la durée de la requête ; L(fr, en) choisit
le bon texte.
"""
from __future__ import annotations

from contextvars import ContextVar

LANG: ContextVar[str] = ContextVar("dv_lang", default="fr")
COOKIE = "dv_lang"


def lang() -> str:
    return LANG.get()


def is_en() -> bool:
    return LANG.get() == "en"


def L(fr: str, en: str) -> str:
    """Le texte dans la langue de la requête courante."""
    return en if LANG.get() == "en" else fr


def pick(request) -> tuple[str, bool]:
    """(langue, vient_de_la_query) pour une requête Starlette."""
    q = (request.query_params.get("lang") or "").lower()[:2]
    if q in ("fr", "en"):
        return q, True
    c = (request.cookies.get(COOKIE) or "").lower()[:2]
    return (c if c in ("fr", "en") else "fr"), False
