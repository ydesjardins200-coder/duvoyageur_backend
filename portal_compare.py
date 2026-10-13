"""
portal_compare.py
=================
The catalogue comparator was retired: our own hotel picks, photos and ratings
are no longer a source (the assistant compares hotels from live wholesaler and
TripAdvisor pages instead). Old links redirect to the portal home.
"""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/portail/comparer")
def compare_hotels():
    return RedirectResponse("/portail", status_code=301)
