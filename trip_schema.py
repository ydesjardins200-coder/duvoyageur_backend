"""
trip_schema.py
================
The canonical structured representation of a customer's selected trip.

This is the object the whole pipeline revolves around:
  Messenger message (text or screenshot)  ->  TripRequest  ->  Softvoyage search

Design notes
------------
* EVERYTHING is optional. Customers send partial, messy info. The parser must
  never invent data; if a field is unknown it stays null and gets listed in
  `needs_clarification` so a human (or an auto-reply) knows what to ask for.
* `needs_clarification` + `parse_confidence` are what drive the human-in-the-loop
  "gate" before any booking action. They are as important as the trip fields.
* The minimum set required to even attempt a Softvoyage search is:
  origin + (hotel OR destination) + departure window + passengers.
  The validator below computes whether that minimum is met.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class PriceBasis(str, Enum):
    per_person = "per_person"
    total = "total"
    unknown = "unknown"


class BoardType(str, Enum):
    all_inclusive = "all_inclusive"
    breakfast = "breakfast"
    half_board = "half_board"
    full_board = "full_board"
    room_only = "room_only"
    other = "other"
    unknown = "unknown"


# --------------------------------------------------------------------------- #
# Sub-objects
# --------------------------------------------------------------------------- #
class Passenger(BaseModel):
    """One traveller. Operators price by exact age, so capture it precisely."""
    age: Optional[int] = Field(
        None, description="Age in years AT TIME OF TRAVEL if stated or derivable."
    )
    date_of_birth: Optional[str] = Field(
        None, description="ISO date YYYY-MM-DD if the customer gave a birth date."
    )
    is_child: Optional[bool] = Field(
        None, description="True if clearly a child/infant, else null. Do not guess."
    )


class PriceSeen(BaseModel):
    """The price the customer found themselves — the number we must beat/match."""
    amount: Optional[float] = Field(None, description="Numeric amount only, no symbols.")
    currency: str = Field("CAD", description="ISO currency code. Default CAD.")
    basis: PriceBasis = Field(
        PriceBasis.unknown,
        description="Is the amount per person or the total for the whole party?",
    )
    taxes_included: Optional[bool] = Field(
        None, description="True/False if stated, else null."
    )
    raw: Optional[str] = Field(
        None, description="The price exactly as written, e.g. '2 400$ pp tx inc'."
    )


# --------------------------------------------------------------------------- #
# Root object
# --------------------------------------------------------------------------- #
class TripRequest(BaseModel):
    # --- routing / locations ---
    origin_city: Optional[str] = Field(
        None, description="Departure city as written, e.g. 'Montréal', 'Québec'."
    )
    origin_airport_iata: Optional[str] = Field(
        None,
        description=(
            "3-letter IATA code for the departure airport ONLY if confident "
            "(Montréal=YUL, Québec=YQB, Ottawa=YOW, Toronto=YYZ). Else null."
        ),
    )
    destination: Optional[str] = Field(
        None,
        description="Destination region/city/country, e.g. 'Cancún', 'Punta Cana'. "
        "May be implied by the hotel.",
    )

    # --- hotel ---
    hotel_name_raw: Optional[str] = Field(
        None, description="Hotel name exactly as the customer wrote it."
    )
    hotel_name_normalized: Optional[str] = Field(
        None,
        description="Your best-guess cleaned/canonical hotel name for matching. "
        "Keep the brand + property, drop noise. Null if unsure.",
    )

    # --- dates ---
    departure_date: Optional[str] = Field(
        None, description="ISO date YYYY-MM-DD of outbound departure if known."
    )
    return_date: Optional[str] = Field(
        None, description="ISO date YYYY-MM-DD of return if known."
    )
    nights: Optional[int] = Field(
        None, description="Number of nights if stated or derivable from the dates."
    )
    dates_raw: Optional[str] = Field(
        None, description="Dates exactly as written, e.g. 'semaine du 15 fév', '12 au 19 déc'."
    )

    # --- party ---
    passengers: List[Passenger] = Field(
        default_factory=list, description="One entry per traveller with their age."
    )
    num_adults: Optional[int] = Field(None, description="Count of adults if stated.")
    num_children: Optional[int] = Field(None, description="Count of children if stated.")
    num_rooms: Optional[int] = Field(None, description="Number of rooms if stated.")
    room_type: Optional[str] = Field(
        None, description="Room category if stated, e.g. 'Junior Suite Swim-up'."
    )
    board: BoardType = Field(
        BoardType.unknown, description="Meal plan. Most ITC packages are all_inclusive."
    )

    # --- supplier signal (critical for an exact match) ---
    operator: Optional[str] = Field(
        None,
        description="Tour operator / transporteur if visible: Transat, Sunwing, "
        "Air Canada Vacations, WestJet Vacations, etc. Null if not shown.",
    )

    # --- the price they found ---
    price_seen: Optional[PriceSeen] = Field(
        None, description="The price the customer reported finding."
    )
    source: Optional[str] = Field(
        None, description="Where they found it: a URL, site name, or 'screenshot'."
    )

    # --- contact ---
    customer_email: Optional[str] = Field(None, description="Email if provided.")
    customer_name: Optional[str] = Field(None, description="Name if provided.")

    # --- parser meta (drives the human gate) ---
    parse_confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Your overall confidence 0-1 that the extraction is correct.",
    )
    needs_clarification: List[str] = Field(
        default_factory=list,
        description="Plain-language list of what's MISSING or AMBIGUOUS and must be "
        "asked of the customer before a search, e.g. "
        "['âge exact des enfants', 'date de retour'].",
    )
    agent_notes: Optional[str] = Field(
        None, description="Short free-text note for the agent: caveats, assumptions made."
    )
    raw_message: Optional[str] = Field(
        None, description="The original customer text, copied verbatim for the record."
    )

    # ------------------------------------------------------------------ #
    # Convenience: is this enough to even try a Softvoyage search?
    # ------------------------------------------------------------------ #
    def is_searchable(self) -> bool:
        has_origin = bool(self.origin_airport_iata or self.origin_city)
        has_where = bool(self.hotel_name_raw or self.destination)
        has_when = bool(self.departure_date or self.dates_raw)
        has_who = bool(self.passengers or self.num_adults)
        return has_origin and has_where and has_when and has_who

    def missing_core_fields(self) -> List[str]:
        missing = []
        if not (self.origin_airport_iata or self.origin_city):
            missing.append("aéroport de départ")
        if not (self.hotel_name_raw or self.destination):
            missing.append("hôtel ou destination")
        if not (self.departure_date or self.dates_raw):
            missing.append("dates de voyage")
        if not (self.passengers or self.num_adults):
            missing.append("nombre et âge des voyageurs")
        return missing

    def remaining_fields(self) -> List[str]:
        """Everything still needed to produce a rebate quote, in priority order.

        Goes beyond the 4 core fields: a real quote also needs the price the
        customer found, whether it's per-person or total, the operator, the
        children's ages, and an email to send the rebate to.
        """
        rem: List[str] = []
        if not (self.origin_airport_iata or self.origin_city):
            rem.append("aéroport de départ")
        if not (self.hotel_name_raw or self.destination):
            rem.append("hôtel ou destination")
        if not (self.departure_date or self.dates_raw):
            rem.append("dates de voyage")
        if not (self.passengers or self.num_adults):
            rem.append("nombre de voyageurs")
        # Children's ages matter for pricing — only ask if there ARE children.
        if (self.num_children or 0) > 0 and not any(p.age is not None for p in self.passengers):
            rem.append("âge des enfants")
        # Rooms: number (and type) — a single question covers both.
        if not self.num_rooms:
            rem.append("nombre et type de chambre")
        # Price is the heart of the rebate.
        if not (self.price_seen and self.price_seen.amount is not None):
            rem.append("prix trouvé")
        elif self.price_seen.basis == PriceBasis.unknown:
            rem.append("prix par personne ou total")
        if not self.operator:
            rem.append("voyagiste / site du forfait")
        if not self.customer_email:
            rem.append("courriel pour le rabais")
        return rem

    def next_question(self) -> Optional[str]:
        """The single most relevant question to ask next, or None when ready."""
        rem = self.remaining_fields()
        return _NEXT_QUESTIONS.get(rem[0]) if rem else None


# One natural question per checklist item (customer-facing, in order of priority).
_NEXT_QUESTIONS = {
    "aéroport de départ": "De quel aéroport aimerais-tu partir (Montréal, Québec…) ?",
    "hôtel ou destination": "Quelle destination ou quel hôtel t'intéresse ?",
    "dates de voyage": "Pour quelles dates penses-tu partir (départ et retour) ?",
    "nombre de voyageurs": "Vous voyagez combien de personnes en tout (adultes et enfants) ?",
    "âge des enfants": "Quel sera l'âge des enfants au moment du voyage ?",
    "nombre et type de chambre": "Combien de chambres te faut-il, et un type de chambre en particulier ?",
    "prix trouvé": "Quel prix as-tu vu pour ce forfait ?",
    "prix par personne ou total": "Ce prix-là, c'est par personne ou pour tout le groupe ?",
    "voyagiste / site du forfait": "Sur quel site ou voyagiste as-tu trouvé ce prix (Transat, Sunwing…) ?",
    "courriel pour le rabais": "À quel courriel veux-tu recevoir ton rabais ?",
}


def merge_trip_requests(old: "TripRequest", new: "TripRequest") -> "TripRequest":
    """
    Additive merge for progressive profiling across a conversation.

    Rule: a new NON-EMPTY value overwrites (people correct themselves), but a new
    EMPTY value NEVER erases data the customer already gave. So a follow-up like
    "départ Montréal" fills the origin without wiping the adults/dates from before.
    """
    merged = old.model_copy(deep=True)

    # Fields handled explicitly below; everything else uses the simple rule.
    EXPLICIT = {"raw_message", "needs_clarification", "parse_confidence",
                "agent_notes", "passengers", "price_seen", "board"}
    for field in TripRequest.model_fields:
        if field in EXPLICIT:
            continue
        nv = getattr(new, field)
        if nv not in (None, "", []):          # new value present -> overwrite
            setattr(merged, field, nv)

    if new.passengers:                          # only replace if new actually lists them
        merged.passengers = [p.model_copy(deep=True) for p in new.passengers]
    if new.price_seen is not None:
        merged.price_seen = new.price_seen.model_copy(deep=True)
    if new.board and new.board != BoardType.unknown:
        merged.board = new.board

    merged.parse_confidence = max(old.parse_confidence, new.parse_confidence)

    notes = [n for n in (old.agent_notes, new.agent_notes) if n]
    merged.agent_notes = " · ".join(notes) if notes else None

    raws = [r for r in (old.raw_message, new.raw_message) if r]
    merged.raw_message = "\n---\n".join(raws) if raws else None

    # Recompute what's still needed — this shrinks as the customer answers,
    # and covers everything required to actually quote a rebate.
    merged.needs_clarification = merged.remaining_fields()
    return merged
