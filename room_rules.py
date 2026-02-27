"""
room_rules.py
─────────────
Hong Kong GFA / NOFA classification rules engine.

Sources:
  GFA rules   → PNAP APP-2, B(P)R 23(3) & 23A(3)
  Concessions → PNAP APP-151 Appendix A (Rev. July 2025), Items 1–38
  NOFA        → HK industry convention
  Site Coverage → B(P)R 20–22

Each concession item now carries:
  • item_no        — numeric APP-151 Appendix A item number (e.g. 5, 12, 2.1)
  • pnap_ref       — relevant PNAP / JPN reference string
  • domestic       — does it apply to domestic GFA?
  • non_domestic   — does it apply to non-domestic GFA?
  • subject_to_cap — counted toward the overall 10% APP-151 cap?
  • requires_beam_plus / requires_prereq — pre-requisite conditions

Usage:
    from room_rules import ROOM_RULES, classify_room, BuildingType
    result = classify_room("balcony", area_m2=4.5, building_type="residential")
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─── Enums ────────────────────────────────────────────────────────────────────

class BuildingType(str, Enum):
    RESIDENTIAL  = "residential"
    NON_DOMESTIC = "non_domestic"
    COMPOSITE    = "composite"
    HOTEL        = "hotel"


class InclusionRule(str, Enum):
    FULL        = "full"        # 100% counted
    HALF        = "half"        # 50% counted
    EXCLUDED    = "excluded"    # 0% — disregarded / exempt
    CONDITIONAL = "conditional" # Requires BD/BEAM Plus approval


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class AreaClassification:
    """Result of classifying a single room / space."""
    room_type:      str
    area_m2:        float
    building_type:  BuildingType

    # GFA
    gfa_rule:       InclusionRule
    gfa_multiplier: float
    gfa_area_m2:    float
    gfa_note:       str = ""

    # APP-151 concession metadata
    is_concession:      bool  = False
    item_no:            str   = ""   # e.g. "5", "12", "2.1", "38"
    concession_item:    str   = ""   # human-readable label
    pnap_ref:           str   = ""   # e.g. "JPN1", "PNAP APP-2 & APP-42"
    subject_to_cap:     bool  = False
    requires_beam_plus: bool  = False
    requires_prereq:    bool  = False  # P in "pre-requisite" column
    domestic:           bool  = True
    non_domestic:       bool  = False

    # NOFA
    nofa_rule:       InclusionRule = InclusionRule.EXCLUDED
    nofa_multiplier: float         = 0.0
    nofa_area_m2:    float         = 0.0
    nofa_note:       str           = ""


@dataclass
class RoomRule:
    """Defines how a room type is treated under GFA / NOFA rules."""
    label:    str
    keywords: list[str]

    # GFA defaults
    gfa_rule:       InclusionRule = InclusionRule.FULL
    gfa_multiplier: float         = 1.0
    gfa_note:       str           = ""

    # APP-151 concession metadata
    is_concession:      bool  = False
    item_no:            str   = ""
    concession_item:    str   = ""
    pnap_ref:           str   = ""
    subject_to_cap:     bool  = False
    requires_beam_plus: bool  = False
    requires_prereq:    bool  = False
    domestic:           bool  = True
    non_domestic:       bool  = False

    # NOFA defaults
    nofa_rule:       InclusionRule = InclusionRule.EXCLUDED
    nofa_multiplier: float         = 0.0
    nofa_note:       str           = ""

    # Per-building-type overrides: {BuildingType: {field: value}}
    overrides: dict = field(default_factory=dict)


# ─── Rule table ───────────────────────────────────────────────────────────────
#
# Organised by APP-151 Appendix A section:
#   A. Disregarded GFA under B(P)R 23(3)(b)           Items 1, 2.1, 2.2, 2.3
#   B. Disregarded GFA under B(P)R 23A(3)              Items 3, 4
#   C. Green Features under JPNs                       Items 5–13
#   D. Amenity Features                                Items 14–29
#   E. Other Items                                     Items 30–36
#   F. Bonus GFA                                       Item 37
#   G. Additional Green Features (MiC)                 Item 38
#   H. Habitable / service rooms (no concession)
#
# ─────────────────────────────────────────────────────────────────────────────

ROOM_RULES: list[RoomRule] = [

    # ══════════════════════════════════════════════════════════════════════════
    # A. DISREGARDED GFA UNDER B(P)R 23(3)(b)
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 1: Carpark / Loading & Unloading (excl. PTT) ────────────────────
    RoomRule(
        label="Carpark / Loading & Unloading Area",
        keywords=[
            "carpark", "car park", "car-park", "parking", "parking space",
            "loading", "unloading", "loading/unloading", "loading bay",
            "loading dock", "car lift", "car lift shaft",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="100% disregarded under B(P)R 23(3)(b) — Item 1. "
                 "Excludes Public Transport Terminus.",
        is_concession=True,
        item_no="1",
        concession_item="Carpark and Loading/Unloading Area excl. PTT",
        pnap_ref="PNAP APP-2 & APP-111",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 2.1: Mandatory Plant Room — area limited by PNAP / Reg ──────────
    RoomRule(
        label="Mandatory Plant Room (PNAP-limited)",
        keywords=[
            "lift machine room", "lift motor room", "tbe room",
            "refuse storage", "material recovery chamber", "mrc",
            "refuse chamber", "waste chamber",
            "fire services pump room", "fs pump room",
            "sprinkler control valve", "svc cabinet",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Mandatory plant room with area limited by PNAP/Reg — "
                 "exempt under Item 2.1. No cap.",
        is_concession=True,
        item_no="2.1",
        concession_item="Mandatory Plant Room (PNAP-limited area)",
        pnap_ref="PNAP APP-35 & APP-84",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 2.2: Mandatory Plant Room — area NOT limited ────────────────────
    RoomRule(
        label="Mandatory Plant Room (not PNAP-limited)",
        keywords=[
            "transformer room", "main switch room", "msr",
            "electrical switch room", "hv room", "lv room", "hv/lv room",
            "water meter cabinet", "meter room", "meter cabinet",
            "water tank", "potable water tank", "flushing water tank",
            "pump room", "booster pump", "water pump",
            "substation", "sub-station",
            "rs & mrc", "rs&mrc",
            "pd",  # pipe duct (mandatory)
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Mandatory plant room (area not limited by PNAP/Reg) — "
                 "exempt under Item 2.2. No cap.",
        is_concession=True,
        item_no="2.2",
        concession_item="Mandatory Plant Room (not PNAP-limited)",
        pnap_ref="PNAP APP-2 & APP-42",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 2.3: Non-mandatory / Non-essential Plant Room ───────────────────
    RoomRule(
        label="Non-Mandatory Plant Room",
        keywords=[
            "plant room", "a/c plant", "ac plant", "ahu room",
            "air handling unit", "chiller room", "cooling tower",
            "mechanical room", "m&e room", "bms room", "building services",
            "boiler room", "smatv room", "telecom room",
            "electrical room", "genset", "generator room",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Non-mandatory plant room — subject to 10% overall cap "
                 "AND prerequisites (P) under Item 2.3.",
        is_concession=True,
        item_no="2.3",
        concession_item="Non-Mandatory / Non-Essential Plant Room",
        pnap_ref="PNAP APP-2 & APP-42",
        subject_to_cap=True, requires_beam_plus=False, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # B. DISREGARDED GFA UNDER B(P)R 23A(3) — Hotel only
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 3: Hotel Pick-up / Set-down Area ────────────────────────────────
    RoomRule(
        label="Hotel Pick-up / Set-down Area",
        keywords=["hotel pickup", "hotel drop-off", "hotel set down",
                  "hotel porte cochere", "porte-cochère"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Hotel pick-up/set-down area — exempt under Item 3 "
                 "(B(P)R 23A(3)). Hotel buildings only.",
        is_concession=True,
        item_no="3",
        concession_item="Hotel Pick-up / Set-down Area",
        pnap_ref="PNAP APP-40",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        overrides={
            BuildingType.HOTEL: {},
            # Non-hotel buildings: treat as full GFA
            BuildingType.RESIDENTIAL:  {"gfa_rule": InclusionRule.FULL, "gfa_multiplier": 1.0, "is_concession": False},
            BuildingType.NON_DOMESTIC: {"gfa_rule": InclusionRule.FULL, "gfa_multiplier": 1.0, "is_concession": False},
            BuildingType.COMPOSITE:    {"gfa_rule": InclusionRule.FULL, "gfa_multiplier": 1.0, "is_concession": False},
        },
    ),

    # ── Item 4: Hotel Supporting Facilities ──────────────────────────────────
    RoomRule(
        label="Hotel Supporting Facilities",
        keywords=["hotel laundry", "hotel linen", "hotel back of house",
                  "hotel boh", "hotel housekeeping"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Hotel supporting facilities — exempt under Item 4. Hotel only.",
        is_concession=True,
        item_no="4",
        concession_item="Hotel Supporting Facilities",
        pnap_ref="PNAP APP-40",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # C. GREEN FEATURES UNDER JOINT PRACTICE NOTES (JPNs)
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 5: Balcony (Residential) ────────────────────────────────────────
    # Max exemption = 10% of UFS of flat, counted toward 10% overall cap
    RoomRule(
        label="Balcony",
        keywords=["balcony", "balc", "verandah", "veranda"],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.5,
        gfa_note="Balcony — 50% GFA multiplier when claiming Item 5 concession. "
                 "Max exemption = 10% of UFS of flat. Subject to overall 10% cap. "
                 "Prerequisites (P) apply. JPN1.",
        is_concession=True,
        item_no="5",
        concession_item="Balcony for Residential Buildings",
        pnap_ref="JPN1",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Balconies excluded from NOFA.",
    ),

    # ── Item 6: Wider Common Corridor / Lift Lobby ───────────────────────────
    RoomRule(
        label="Wider Common Corridor / Lift Lobby (JPN1)",
        keywords=["wider corridor", "wider common corridor",
                  "wider lift lobby", "enlarged corridor"],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Wider common corridor / lift lobby — Item 6 concession. "
                 "Subject to 10% cap. Prerequisites (P) apply. JPN1.",
        is_concession=True,
        item_no="6",
        concession_item="Wider Common Corridor and Lift Lobby",
        pnap_ref="JPN1",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 7: Communal Sky Garden ───────────────────────────────────────────
    RoomRule(
        label="Communal Sky Garden",
        keywords=["sky garden", "communal sky garden", "sky terrace",
                  "communal roof garden", "communal terrace"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Communal sky garden — exempt (not subject to 10% cap) under Item 7. "
                 "Prerequisites (P) apply. JPN1 & JPN2.",
        is_concession=True,
        item_no="7",
        concession_item="Communal Sky Garden",
        pnap_ref="JPN1 & JPN2, PNAP APP-122",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 8: Communal Podium Garden (Non-Residential) ─────────────────────
    RoomRule(
        label="Communal Podium Garden",
        keywords=["podium garden", "communal podium", "podium landscape",
                  "communal garden", "landscaped area", "play area",
                  "communal landscaped"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Communal podium garden (non-residential) — exempt under Item 8. "
                 "Prerequisites (P) apply. JPN1.",
        is_concession=True,
        item_no="8",
        concession_item="Communal Podium Garden for Non-Residential Buildings",
        pnap_ref="JPN1",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 9: Acoustic Fin ──────────────────────────────────────────────────
    RoomRule(
        label="Acoustic Fin",
        keywords=["acoustic fin", "noise fin", "acoustic screen",
                  "acoustic barrier fin"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Acoustic fin — exempt under Item 9. Prerequisites (P) apply. JPN1.",
        is_concession=True,
        item_no="9",
        concession_item="Acoustic Fin",
        pnap_ref="JPN1",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 10: Wing Wall / Wind Catcher / Funnel ───────────────────────────
    RoomRule(
        label="Wing Wall / Wind Catcher / Funnel",
        keywords=["wing wall", "wind catcher", "wind funnel",
                  "wind scoop", "venturi", "air funnel"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Wing wall / wind catcher / funnel — exempt under Item 10. "
                 "Prerequisites (P) apply. JPN1.",
        is_concession=True,
        item_no="10",
        concession_item="Wing Wall, Wind Catcher and Funnel",
        pnap_ref="JPN1",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 11: Non-Structural Prefabricated External Wall ───────────────────
    RoomRule(
        label="Non-Structural Prefabricated External Wall",
        keywords=["precasted facade", "precast facade", "non-structural prefab",
                  "prefabricated external wall", "pc facade", "dc"],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Non-structural prefabricated external wall — subject to 10% cap "
                 "under Item 11. Prerequisites (P) apply. JPN2.",
        is_concession=True,
        item_no="11",
        concession_item="Non-Structural Prefabricated External Wall",
        pnap_ref="JPN2",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 12: Utility Platform ─────────────────────────────────────────────
    # Max exemption = 50% of UP area, capped at 1.5m² per flat
    RoomRule(
        label="Utility Platform",
        keywords=["utility platform", "u/p", "up ", " up",
                  "service platform", "u1", "u2", "u3", "u4"],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.5,
        gfa_note="Utility platform — 50% GFA multiplier under Item 12. "
                 "Max exemption = 50% of area (cap at 1.5m² per flat). "
                 "Subject to overall 10% cap. Prerequisites (P) apply. JPN2.",
        is_concession=True,
        item_no="12",
        concession_item="Utility Platform",
        pnap_ref="JPN2",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Utility platform excluded from NOFA.",
    ),

    # ── Item 13: Noise Barrier ────────────────────────────────────────────────
    RoomRule(
        label="Noise Barrier",
        keywords=["noise barrier", "sound barrier", "acoustic wall",
                  "noise screen"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Noise barrier — exempt under Item 13. "
                 "Prerequisites (P) apply. JPN2.",
        is_concession=True,
        item_no="13",
        concession_item="Noise Barrier",
        pnap_ref="JPN2",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # D. AMENITY FEATURES
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 14: Caretaker / Management Facilities ───────────────────────────
    RoomRule(
        label="Caretaker / Management Facilities",
        keywords=[
            "caretaker", "care taker", "caretaker room", "caretaker office",
            "management office", "owners committee", "owners corporation",
            "guard room", "security office", "watchman",
            "ndc",  # non-domestic common area code used in sample
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Caretaker / management facilities — subject to 10% cap "
                 "under Item 14. Prerequisites (P) apply. PNAP APP-42.",
        is_concession=True,
        item_no="14",
        concession_item="Caretakers' Quarters, Management Office, Owners' Corporation",
        pnap_ref="PNAP APP-42",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 15: Residential Recreational Facilities ──────────────────────────
    # Max exemption per APP-104: 5% of domestic GFA of building
    RoomRule(
        label="Residential Recreational Facility",
        keywords=[
            "recreational", "recreation room", "gym", "gymnasium",
            "swimming pool", "pool deck", "function room", "clubhouse",
            "multi-purpose room", "social activity room", "ndc4", "ndc5", "ndc6",
            "covered walkway", "recreational facilities",
            "changing room", "locker room",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Residential recreational facilities — subject to 10% cap "
                 "under Item 15. Max = 5% domestic GFA (APP-104). "
                 "Prerequisites (P) apply.",
        is_concession=True,
        item_no="15",
        concession_item="Residential Recreational Facilities",
        pnap_ref="PNAP APP-2, APP-42 & APP-104",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 16: Covered Landscaped / Play Area ───────────────────────────────
    RoomRule(
        label="Covered Landscaped / Play Area",
        keywords=[
            "covered landscape", "covered landscaped", "covered play area",
            "landscape area", "covered green", "amenity deck",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Covered landscaped and play area — exempt (not subject to 10% cap) "
                 "under Item 16. Prerequisites (P) apply. PNAP APP-42.",
        is_concession=True,
        item_no="16",
        concession_item="Covered Landscaped and Play Area",
        pnap_ref="PNAP APP-42",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 17: Trellis / Horizontal Screen / Covered Walkway ───────────────
    RoomRule(
        label="Trellis / Covered Walkway / Horizontal Screen",
        keywords=[
            "trellis", "covered walkway", "horizontal screen",
            "pergola", "louvered canopy", "screen roof",
            "dc2", "dc3",  # trellis IDs used in sample
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Trellis / covered walkway / horizontal screen — exempt under "
                 "Item 17. Subject to 5% of roof area cap (max 20m²). "
                 "Prerequisites (P) apply. PNAP APP-42.",
        is_concession=True,
        item_no="17",
        concession_item="Horizontal Screen, Covered Walkway and Trellis",
        pnap_ref="PNAP APP-42",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 18: Larger Lift Shaft ────────────────────────────────────────────
    # Max exemption = 3.5% of total GFA of building; min accountable = 2.5%
    RoomRule(
        label="Lift Shaft",
        keywords=[
            "lift shaft", "elevator shaft", "lift core", "l1", "l2", "l3",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Larger lift shaft — exempt (excess over 2.5% of GFA) under "
                 "Item 18. Max exemption = 3.5% of total GFA. "
                 "Prerequisites (P) apply. PNAP APP-89.",
        is_concession=True,
        item_no="18",
        concession_item="Larger Lift Shaft",
        pnap_ref="PNAP APP-89",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 19: Chimney Shaft ────────────────────────────────────────────────
    RoomRule(
        label="Chimney Shaft",
        keywords=["chimney", "chimney shaft", "flue shaft", "exhaust shaft"],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Chimney shaft — subject to 10% cap under Item 19. "
                 "Prerequisites (P) apply. PNAP APP-2.",
        is_concession=True,
        item_no="19",
        concession_item="Chimney Shaft",
        pnap_ref="PNAP APP-2",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 20: Other Non-Mandatory Plant Room ───────────────────────────────
    RoomRule(
        label="Other Non-Mandatory Plant Room",
        keywords=[
            "boiler room", "smatv", "iptv room", "it room",
            "server room", "data room", "comm room", "communication room",
            "security room", "cctv room", "bms", "ibms",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Other non-mandatory plant room — subject to 10% cap under "
                 "Item 20. Prerequisites (P) apply. PNAP APP-2.",
        is_concession=True,
        item_no="20",
        concession_item="Other Non-Mandatory Plant Room (Boiler, SMATV etc.)",
        pnap_ref="PNAP APP-2",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 21: Pipe Duct / Air Duct for Mandatory Feature ───────────────────
    RoomRule(
        label="Pipe Duct / Air Duct (Mandatory)",
        keywords=[
            "pipe duct", "air duct", "riser duct", "services duct",
            "duct room", "vertical duct", "riser shaft",
            "fsi duct", "fire services duct",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Pipe / air duct for mandatory feature — exempt under Item 21. "
                 "No cap. PNAP APP-2 & APP-93.",
        is_concession=True,
        item_no="21",
        concession_item="Pipe Duct / Air Duct for Mandatory Feature",
        pnap_ref="PNAP APP-2 & APP-93",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 22: Pipe Duct / Air Duct for Non-Mandatory Feature ──────────────
    RoomRule(
        label="Pipe Duct / Air Duct (Non-Mandatory)",
        keywords=[
            "a/c duct", "ac duct", "hvac duct", "condensate duct",
            "drainage duct", "chilled water duct",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Pipe / air duct for non-mandatory feature — subject to 10% cap "
                 "under Item 22. Prerequisites (P) apply. PNAP APP-2.",
        is_concession=True,
        item_no="22",
        concession_item="Pipe Duct / Air Duct for Non-Mandatory Feature",
        pnap_ref="PNAP APP-2",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 23: Plant Room / Duct for Environmentally Friendly System ────────
    RoomRule(
        label="Plant Room / Duct for EF System",
        keywords=[
            "solar panel room", "renewable energy room", "green energy plant",
            "ev charging room", "photovoltaic plant", "pv room",
            "wind turbine room", "ef plant", "environmental plant",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Plant room / duct for environmentally friendly system — "
                 "exempt under Item 23. Prerequisites (P) apply. PNAP APP-2.",
        is_concession=True,
        item_no="23",
        concession_item="Plant Room / Duct for Environmentally Friendly System",
        pnap_ref="PNAP APP-2",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 24: High Headroom Void in Non-Domestic Development ──────────────
    RoomRule(
        label="High Headroom Void (Non-Domestic)",
        keywords=[
            "high headroom", "void over cinema", "void over arcade",
            "atrium void", "high bay void", "double height void",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="High headroom / void in non-domestic development — "
                 "exempt under Item 24. Prerequisites (P) apply. PNAP APP-2.",
        is_concession=True,
        item_no="24",
        concession_item="High Headroom and Void (Non-Domestic)",
        pnap_ref="PNAP APP-2",
        subject_to_cap=False, requires_beam_plus=True, requires_prereq=True,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 25: Void over Main Common Entrance (Non-Domestic) ───────────────
    RoomRule(
        label="Void over Main Entrance (Non-Domestic)",
        keywords=[
            "prestige entrance", "void over entrance", "main entrance void",
            "entrance atrium", "entrance canopy void",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Void over main common entrance (non-domestic) — subject to "
                 "10% cap under Item 25. Prerequisites (P) apply. PNAP APP-2 & APP-42.",
        is_concession=True,
        item_no="25",
        concession_item="Void over Main Common Entrance (Non-Domestic)",
        pnap_ref="PNAP APP-2 & APP-42",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 26: Void in Duplex Domestic Flat ─────────────────────────────────
    # Max = 10% of UFS of flat; overall 0.5% of total domestic GFA
    RoomRule(
        label="Void in Duplex Flat",
        keywords=[
            "duplex void", "void in duplex", "internal void",
            "double height flat", "v1", "v2",
        ],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Void in duplex domestic flat — subject to 10% cap under Item 26. "
                 "Max = 10% of UFS; overall cap 0.5% of total domestic GFA. "
                 "Prerequisites (P) apply. PNAP APP-2.",
        is_concession=True,
        item_no="26",
        concession_item="Void in Duplex Domestic Flat and House",
        pnap_ref="PNAP APP-2",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 27: Sunshade / Reflector ─────────────────────────────────────────
    RoomRule(
        label="Sunshade / Reflector",
        keywords=[
            "sunshade", "sun shade", "solar reflector", "light shelf",
            "solar shading", "external blind", "external shading",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Sunshade / reflector — exempt under Item 27. No cap. "
                 "PNAP APP-19, APP-67 & APP-156.",
        is_concession=True,
        item_no="27",
        concession_item="Sunshade and Reflector",
        pnap_ref="PNAP APP-19, APP-67 & APP-156",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 28: Minor Projection (A/C Box, Window Cill, etc.) ───────────────
    RoomRule(
        label="Minor Projection (A/C Box / Window Cill)",
        keywords=[
            "ac box", "a/c box", "air con box", "air conditioning box",
            "a/c platform", "ac platform", "ac1", "ac2",
            "window cill", "window sill", "projecting window",
            "minor projection",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Minor projection (A/C box, A/C platform, window cill, "
                 "projecting window) — exempt under Item 28. No cap. "
                 "PNAP APP-19 & APP-42.",
        is_concession=True,
        item_no="28",
        concession_item="Minor Projection (A/C Box, A/C Platform, Window Cill)",
        pnap_ref="PNAP APP-19 & APP-42",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 29: Other Projection (not covered by Item 28) ───────────────────
    RoomRule(
        label="Other Projection",
        keywords=["other projection", "large projection", "bay window"],
        gfa_rule=InclusionRule.CONDITIONAL, gfa_multiplier=0.0,
        gfa_note="Other projection not covered by Item 28 — subject to 10% cap "
                 "under Item 29. Prerequisites (P) apply. PNAP APP-19.",
        is_concession=True,
        item_no="29",
        concession_item="Other Projection (not under Item 28)",
        pnap_ref="PNAP APP-19",
        subject_to_cap=True, requires_beam_plus=True, requires_prereq=True,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # E. OTHER ITEMS
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 30: Refuge Floor ─────────────────────────────────────────────────
    RoomRule(
        label="Refuge Floor",
        keywords=[
            "refuge floor", "refuge level", "fireman lift lobby",
            "refuge cum sky garden", "refuge sky garden",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Refuge floor (incl. cum sky garden) — exempt under Item 30. "
                 "No cap. PNAP APP-2 & APP-122.",
        is_concession=True,
        item_no="30",
        concession_item="Refuge Floor incl. Refuge Floor cum Sky Garden",
        pnap_ref="PNAP APP-2 & APP-122",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 31: Covered Area under Large Projecting Feature ─────────────────
    RoomRule(
        label="Covered Area under Large Projection",
        keywords=[
            "covered area under projection", "covered under overhang",
            "covered under canopy", "area under projection",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Covered area under large projecting / overhanging feature — "
                 "exempt under Item 31. No cap. PNAP APP-19.",
        is_concession=True,
        item_no="31",
        concession_item="Covered Area under Large Projecting/Overhanging Feature",
        pnap_ref="PNAP APP-19",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 32: Public Transport Terminus ────────────────────────────────────
    RoomRule(
        label="Public Transport Terminus",
        keywords=[
            "public transport terminus", "ptt", "bus terminus",
            "minibus terminus", "ferry terminus",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Public transport terminus — 100% disregarded under Item 32. "
                 "No cap. PNAP APP-2.",
        is_concession=True,
        item_no="32",
        concession_item="Public Transport Terminus (PTT)",
        pnap_ref="PNAP APP-2",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 33: Party Structure / Common Staircase ───────────────────────────
    RoomRule(
        label="Party Structure / Common Staircase",
        keywords=[
            "party structure", "party wall", "common staircase",
            "shared staircase", "party stair",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Party structure and common staircase — exempt under Item 33. "
                 "No cap. PNAP ADM-2.",
        is_concession=True,
        item_no="33",
        concession_item="Party Structure and Common Staircase",
        pnap_ref="PNAP ADM-2",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 34: Staircase / Lift Shaft / Vertical Duct (serving sole floor) ─
    RoomRule(
        label="Staircase / Vertical Duct (GFA-Excluded)",
        keywords=[
            "staircase", "stair", "stairwell", "fire stair",
            "escape stair", "vertical duct", "services shaft",
            "floor", "roof floor",  # "floor" structural slab area
            "c02", "c03",  # roof/floor codes from sample
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Horizontal area of staircase / lift shaft / vertical duct "
                 "accepted as non-accountable GFA under Item 34. No cap. PNAP APP-2.",
        is_concession=True,
        item_no="34",
        concession_item="Staircase, Lift Shaft and Vertical Duct (GFA-Excluded)",
        pnap_ref="PNAP APP-2",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 35: Public Passage ───────────────────────────────────────────────
    RoomRule(
        label="Public Passage",
        keywords=["public passage", "public walkway", "public arcade",
                  "through-site link", "public atrium"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Public passage — exempt under Item 35. No cap. PNAP APP-108.",
        is_concession=True,
        item_no="35",
        concession_item="Public Passage",
        pnap_ref="PNAP APP-108",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Item 36: Covered Set-Back Area ────────────────────────────────────────
    RoomRule(
        label="Covered Set-Back Area",
        keywords=["covered set back", "covered setback", "set-back area",
                  "set back area", "setback canopy"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Covered set-back area — exempt under Item 36. No cap. "
                 "PNAP APP-152.",
        is_concession=True,
        item_no="36",
        concession_item="Covered Set-Back Area",
        pnap_ref="PNAP APP-152",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # F. BONUS GFA
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 37: Bonus GFA ────────────────────────────────────────────────────
    RoomRule(
        label="Bonus GFA Feature",
        keywords=["bonus gfa", "public open space", "pos",
                  "ground level open space"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Bonus GFA — exempt under Item 37. PNAP APP-108.",
        is_concession=True,
        item_no="37",
        concession_item="Bonus GFA",
        pnap_ref="PNAP APP-108",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=False, non_domestic=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # G. ADDITIONAL GREEN FEATURES — MiC
    # ══════════════════════════════════════════════════════════════════════════

    # ── Item 38: Modular Integrated Construction (MiC) ────────────────────────
    # Max exemption = 10% of GFA of MiC floors
    RoomRule(
        label="MiC Module / MiC Floor Area",
        keywords=[
            "mic", "modular integrated construction",
            "modular integrated", "mic floor", "mic area",
            "prefab module", "modular unit",
        ],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Modular Integrated Construction (MiC) 10% floor area — "
                 "exempt (not subject to overall cap) under Item 38. "
                 "Max = 10% of GFA of the MiC floors. JPN8.",
        is_concession=True,
        item_no="38",
        concession_item="Buildings Adopting Modular Integrated Construction (MiC)",
        pnap_ref="JPN8",
        subject_to_cap=False, requires_beam_plus=False, requires_prereq=False,
        domestic=True, non_domestic=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # H. HABITABLE / SERVICE ROOMS (no concession)
    # ══════════════════════════════════════════════════════════════════════════

    RoomRule(
        label="Flat / Domestic Unit",
        keywords=[
            "flat", "flat a", "flat b", "flat c", "flat d",
            "unit", "apartment", "domestic unit", "residential unit",
            "ad",  # prefix used in sample for domestic areas
        ],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        gfa_note="Domestic flat / unit — full GFA per PNAP APP-2.",
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
        nofa_note="Domestic usable floor space — included in NOFA.",
    ),
    RoomRule(
        label="Bedroom",
        keywords=["bedroom", "bed room", "master bed", "master bedroom",
                  "mbr", "ensuite bedroom"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
        nofa_note="Habitable room — included in NOFA.",
    ),
    RoomRule(
        label="Living / Dining Room",
        keywords=["living", "lounge", "sitting", "reception", "dining",
                  "living/dining", "living room", "dining room"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),
    RoomRule(
        label="Kitchen",
        keywords=["kitchen", "kitch", "kitchenette", "cooking"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),
    RoomRule(
        label="Study / Home Office",
        keywords=["study", "home office", "work room", "library"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),
    RoomRule(
        label="Bathroom / Toilet / WC",
        keywords=["bathroom", "toilet", "wc", "lavatory", "washroom",
                  "ensuite", "en-suite", "powder room", "accessible toilet"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        gfa_note="Wet room — full GFA.",
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Wet rooms excluded from NOFA per HK convention.",
    ),
    RoomRule(
        label="Internal Corridor / Hallway",
        keywords=["corridor", "hallway", "hall", "passage",
                  "entrance hall", "foyer", "circulation"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Circulation spaces excluded from NOFA.",
    ),
    RoomRule(
        label="Common Lift Lobby / Entrance Lobby",
        keywords=["lift lobby", "elevator lobby", "common lobby",
                  "entrance lobby", "domestic entrance", "lobby"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),
    RoomRule(
        label="Storage / Utility Room",
        keywords=["store", "storage", "storeroom", "utility room", "cls", "cloak"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Storage excluded from NOFA.",
    ),
    RoomRule(
        label="F&B / Retail (Non-Domestic)",
        keywords=["f&b", "retail", "shop", "restaurant", "food and beverage",
                  "commercial unit", "and"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
        nofa_note="Non-domestic usable space — included in NOFA.",
        domestic=False, non_domestic=True,
    ),
    RoomRule(
        label="Structural Element / Facade",
        keywords=["structural element", "structure element",
                  "facade", "curtain wall", "cladding", "spandrel",
                  "overhang", "fin", "architectural feature",
                  "precasted facade", "precast facade"],
        gfa_rule=InclusionRule.EXCLUDED, gfa_multiplier=0.0,
        gfa_note="Structural / facade element — excluded from GFA.",
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),
]


# ─── Lookup helpers ───────────────────────────────────────────────────────────

def _find_rule(room_label: str) -> Optional[RoomRule]:
    """
    Find the best matching RoomRule for a given label.
    Case-insensitive; longest keyword match wins.
    """
    label_lower = room_label.lower().strip()
    best_rule:   Optional[RoomRule] = None
    best_length: int = -1

    for rule in ROOM_RULES:
        for kw in rule.keywords:
            if kw in label_lower and len(kw) > best_length:
                best_rule   = rule
                best_length = len(kw)

    return best_rule


def _apply_overrides(rule: RoomRule, building_type: BuildingType) -> dict:
    return rule.overrides.get(building_type, {})


# ─── Main classify function ───────────────────────────────────────────────────

def classify_room(
    room_label:    str,
    area_m2:       float,
    building_type: "BuildingType | str" = BuildingType.RESIDENTIAL,
) -> AreaClassification:
    """
    Classify a room and return its GFA and NOFA contributions.

    Args:
        room_label:    Room name, space label, or DWG layer name.
        area_m2:       Gross polygon area in m².
        building_type: BuildingType enum or string.

    Returns:
        AreaClassification with full GFA / NOFA / APP-151 breakdown.
    """
    if isinstance(building_type, str):
        building_type = BuildingType(building_type)

    rule = _find_rule(room_label)

    if rule is None:
        return AreaClassification(
            room_type=room_label,
            area_m2=area_m2,
            building_type=building_type,
            gfa_rule=InclusionRule.FULL,
            gfa_multiplier=1.0,
            gfa_area_m2=area_m2,
            gfa_note="⚠️ Unrecognised room type — defaulted to full GFA. "
                     "Manual review required.",
            nofa_rule=InclusionRule.EXCLUDED,
            nofa_multiplier=0.0,
            nofa_area_m2=0.0,
            nofa_note="⚠️ Unrecognised — excluded from NOFA pending review.",
        )

    overrides       = _apply_overrides(rule, building_type)
    gfa_rule        = overrides.get("gfa_rule",        rule.gfa_rule)
    gfa_multiplier  = overrides.get("gfa_multiplier",  rule.gfa_multiplier)
    gfa_note        = overrides.get("gfa_note",        rule.gfa_note)
    nofa_rule       = overrides.get("nofa_rule",       rule.nofa_rule)
    nofa_multiplier = overrides.get("nofa_multiplier", rule.nofa_multiplier)
    nofa_note       = overrides.get("nofa_note",       rule.nofa_note)
    is_concession   = overrides.get("is_concession",   rule.is_concession)

    return AreaClassification(
        room_type=room_label,
        area_m2=area_m2,
        building_type=building_type,
        gfa_rule=gfa_rule,
        gfa_multiplier=gfa_multiplier,
        gfa_area_m2=round(area_m2 * gfa_multiplier, 4),
        gfa_note=gfa_note,
        is_concession=is_concession,
        item_no=rule.item_no,
        concession_item=rule.concession_item,
        pnap_ref=rule.pnap_ref,
        subject_to_cap=rule.subject_to_cap,
        requires_beam_plus=rule.requires_beam_plus,
        requires_prereq=rule.requires_prereq,
        domestic=rule.domestic,
        non_domestic=rule.non_domestic,
        nofa_rule=nofa_rule,
        nofa_multiplier=nofa_multiplier,
        nofa_area_m2=round(area_m2 * nofa_multiplier, 4),
        nofa_note=nofa_note,
    )
