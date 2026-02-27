"""
room_rules.py
─────────────
Hong Kong GFA / NOFA classification rules engine.
Based on PNAP APP-2, APP-151 (Rev. July 2025), and B(P)R 23.

Usage:
    from room_rules import ROOM_RULES, classify_room
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
    FULL         = "full"          # 100% of area counted
    HALF         = "half"          # 50% of area counted (e.g. balcony GFA rule)
    EXCLUDED     = "excluded"      # 0% — not counted
    CONDITIONAL  = "conditional"   # Requires BD/BEAM Plus approval


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class AreaClassification:
    """Result of classifying a single room/space."""
    room_type:       str
    area_m2:         float
    building_type:   BuildingType

    # GFA
    gfa_rule:        InclusionRule
    gfa_multiplier:  float          # 0.0, 0.5, or 1.0
    gfa_area_m2:     float          # effective GFA contribution
    gfa_note:        str = ""

    # APP-151 concession
    is_concession:       bool  = False
    concession_item:     str   = ""   # e.g. "APP-151 item 5"
    subject_to_cap:      bool  = False
    requires_beam_plus:  bool  = False

    # NOFA
    nofa_rule:       InclusionRule = InclusionRule.EXCLUDED
    nofa_multiplier: float         = 0.0
    nofa_area_m2:    float         = 0.0
    nofa_note:       str           = ""


@dataclass
class RoomRule:
    """
    Defines how a room type is treated under GFA and NOFA rules.
    One rule can apply to all building types, or be overridden per type.
    """
    label:               str
    keywords:            list[str]   # matched against room labels / DWG layer names

    # GFA defaults
    gfa_rule:            InclusionRule = InclusionRule.FULL
    gfa_multiplier:      float         = 1.0
    gfa_note:            str           = ""

    # APP-151 concession info
    is_concession:       bool  = False
    concession_item:     str   = ""
    subject_to_cap:      bool  = False
    requires_beam_plus:  bool  = False

    # NOFA defaults
    nofa_rule:           InclusionRule = InclusionRule.EXCLUDED
    nofa_multiplier:     float         = 0.0
    nofa_note:           str           = ""

    # Per-building-type overrides: {BuildingType: {field: value}}
    overrides:           dict = field(default_factory=dict)


# ─── Rule table ───────────────────────────────────────────────────────────────
# Sources:
#   GFA rules  → PNAP APP-2, B(P)R 23(3)
#   Concessions → PNAP APP-151 Appendix A (Rev. July 2025)
#   NOFA       → HK industry convention (no single BD practice note)

ROOM_RULES: list[RoomRule] = [

    # ── Habitable rooms ──────────────────────────────────────────────────────
    RoomRule(
        label="Bedroom",
        keywords=["bedroom", "bed room", "master bed", "bed", "mbr", "br"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
        nofa_note="Primary habitable space — included in NOFA.",
    ),
    RoomRule(
        label="Living Room",
        keywords=["living", "lounge", "sitting", "reception", "lr"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),
    RoomRule(
        label="Dining Room",
        keywords=["dining", "dinner", "dr"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),
    RoomRule(
        label="Kitchen",
        keywords=["kitchen", "kit", "kitch", "cooking", "kitchenette"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),
    RoomRule(
        label="Study / Home Office",
        keywords=["study", "office", "home office", "work room", "library"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.FULL, nofa_multiplier=1.0,
    ),

    # ── Wet / service rooms ──────────────────────────────────────────────────
    RoomRule(
        label="Bathroom / Toilet / WC",
        keywords=["bathroom", "toilet", "wc", "bath", "lavatory", "washroom",
                  "ensuite", "en-suite", "powder room"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Wet rooms excluded from NOFA per HK industry convention.",
    ),

    # ── Circulation ──────────────────────────────────────────────────────────
    RoomRule(
        label="Internal Corridor / Hallway",
        keywords=["corridor", "hallway", "hall", "passage", "lobby",
                  "entrance hall", "foyer", "circulation"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Circulation spaces excluded from NOFA.",
    ),
    RoomRule(
        label="Lift Lobby (Common)",
        keywords=["lift lobby", "elevator lobby", "common lobby", "lift hall"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),
    RoomRule(
        label="Staircase / Stairwell",
        keywords=["stair", "staircase", "stairwell", "fire stair", "escape stair"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Storage ──────────────────────────────────────────────────────────────
    RoomRule(
        label="Storage / Utility Room",
        keywords=["store", "storage", "storeroom", "utility room", "utility",
                  "store room", "cls", "cloak"],
        gfa_rule=InclusionRule.FULL, gfa_multiplier=1.0,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Storage excluded from NOFA per HK industry convention.",
    ),

    # ── Balcony / Verandah / Terrace (APP-151 item 5) ────────────────────────
    # QS review Q2.2: answered 'Other' (not 'Correct') — the 50% rule applies
    # specifically when the developer is CLAIMING the APP-151 item 5 concession.
    # It is not automatically applied; BD submission intent must be confirmed.
    # System flags as CONDITIONAL and notes that multiplier applies on concession claim.
    RoomRule(
        label="Balcony / Verandah / Terrace",
        keywords=["balcony", "verandah", "veranda", "terrace", "balc"],
        gfa_rule=InclusionRule.CONDITIONAL,
        gfa_multiplier=0.5,
        gfa_note="50% GFA multiplier applies ONLY when claiming APP-151 item 5 concession. Confirm with BD submission intent. (QS review Q2.2: flagged as conditional, not automatic.)",
        is_concession=True,
        concession_item="APP-151 item 5",
        subject_to_cap=False,
        requires_beam_plus=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
        nofa_note="Balconies excluded from NOFA.",
    ),

    # ── Utility Platform (APP-151 item 12) ───────────────────────────────────
    RoomRule(
        label="Utility Platform",
        keywords=["utility platform", "a/c platform", "ac platform",
                  "air con platform", "service platform"],
        gfa_rule=InclusionRule.HALF,
        gfa_multiplier=0.5,
        gfa_note="Treated same as balcony — 50% toward GFA per APP-151 item 12.",
        is_concession=True,
        concession_item="APP-151 item 12",
        subject_to_cap=False,
        requires_beam_plus=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Mandatory Plant Rooms (APP-151 items 2.1, 2.2) ───────────────────────
    RoomRule(
        label="Mandatory Plant Room",
        keywords=["mandatory plant", "essential plant", "pump room",
                  "electrical room", "substation", "transformer room",
                  "hv room", "lv room", "genset", "generator room",
                  "lift machine room", "lift motor room"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Mandatory plant rooms exempt from GFA per APP-151 items 2.1 & 2.2.",
        is_concession=True,
        concession_item="APP-151 items 2.1, 2.2",
        subject_to_cap=False,
        requires_beam_plus=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Non-Mandatory Plant Rooms (APP-151 items 2.3, 20) ────────────────────
    RoomRule(
        label="Non-Mandatory Plant Room",
        keywords=["plant room", "mechanical room", "m&e room", "bms room",
                  "building services", "air handling", "ahu room",
                  "chiller room", "cooling tower"],
        gfa_rule=InclusionRule.CONDITIONAL,
        gfa_multiplier=0.0,
        gfa_note="Non-mandatory plant room — subject to 10% cap and prerequisites per APP-151 items 2.3, 20.",
        is_concession=True,
        concession_item="APP-151 items 2.3, 20",
        subject_to_cap=True,
        requires_beam_plus=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Carpark (APP-151 item 1) ──────────────────────────────────────────────
    # ⚠️ QS review Q3.3: NOT confirmed as cap-exempt. Flagged pending further review.
    RoomRule(
        label="Carpark / Parking",
        keywords=["carpark", "car park", "parking", "loading bay",
                  "unloading", "loading/unloading", "vehicle"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Carpark and loading/unloading exempt per APP-151 item 1. ⚠️ Cap-exempt status pending full AP/QS confirmation (Q3.3 not confirmed by reviewer).",
        is_concession=True,
        concession_item="APP-151 item 1",
        subject_to_cap=False,
        requires_beam_plus=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Refuse / MRC (APP-151 item 17) ───────────────────────────────────────
    RoomRule(
        label="Refuse / Material Recovery Chamber",
        keywords=["refuse", "mrc", "material recovery", "waste room",
                  "recycling room", "rubbish room"],
        gfa_rule=InclusionRule.CONDITIONAL,
        gfa_multiplier=0.0,
        gfa_note="Subject to 10% cap and prerequisites per APP-151 item 17.",
        is_concession=True,
        concession_item="APP-151 item 17",
        subject_to_cap=True,
        requires_beam_plus=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Communal Sky Garden (APP-151 item 7) ─────────────────────────────────
    # ⚠️ QS review Q3.3: NOT confirmed as cap-exempt. Pending further review.
    RoomRule(
        label="Communal Sky Garden",
        keywords=["sky garden", "communal garden", "roof garden", "sky terrace"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Exempt from GFA per APP-151 item 7. ⚠️ Cap-exempt status pending full AP/QS confirmation (Q3.3 not confirmed by reviewer).",
        is_concession=True,
        concession_item="APP-151 item 7",
        subject_to_cap=False,
        requires_beam_plus=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Refuge Floor (APP-151 item 30) ───────────────────────────────────────
    RoomRule(
        label="Refuge Floor",
        keywords=["refuge floor", "refuge", "fireman lift lobby"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Refuge floors exempt per APP-151 item 30. No cap.",
        is_concession=True,
        concession_item="APP-151 item 30",
        subject_to_cap=False,
        requires_beam_plus=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Facade / Curtain Wall Architectural Features ─────────────────────────
    # CONFIRMED by QS review (Q2.1): facade architectural features and curtain
    # wall projections are GFA-exempted items per BD practice.
    RoomRule(
        label="Facade / Curtain Wall Feature",
        keywords=["facade", "curtain wall", "cladding", "spandrel", "fin",
                  "architectural feature", "arch feature", "canopy",
                  "sun shading", "sunshade", "louver", "louvre",
                  "projecting feature", "overhang"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Facade architectural features and curtain wall are GFA-exempt per BD practice. Confirmed by QS review Q2.1.",
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Lift Shaft / Duct (structural — always excluded) ─────────────────────
    RoomRule(
        label="Lift Shaft / Pipe Duct",
        keywords=["lift shaft", "elevator shaft", "duct", "pipe duct",
                  "riser", "services riser", "shaft"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Structural void — excluded from GFA per B(P)R 23.",
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── Structural Walls / Columns ────────────────────────────────────────────
    RoomRule(
        label="Structural Wall / Column",
        keywords=["structural wall", "column", "shear wall", "core wall",
                  "rc wall", "concrete wall"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="Structural elements excluded from GFA measurement.",
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── FSI / Fire Service Room ───────────────────────────────────────────────
    RoomRule(
        label="FSI / Fire Service Room",
        keywords=["fsi", "fire service", "fire services", "fs room",
                  "sprinkler", "hose reel", "wet riser"],
        gfa_rule=InclusionRule.CONDITIONAL,
        gfa_multiplier=0.0,
        gfa_note="Subject to prerequisites and 10% cap per APP-151 item 2.3.",
        is_concession=True,
        concession_item="APP-151 item 2.3",
        subject_to_cap=True,
        requires_beam_plus=True,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),

    # ── MiC (Modular Integrated Construction) (APP-151 item 38) ──────────────
    # ⚠️ QS review Q3.3: NOT confirmed as cap-exempt. Pending further review.
    RoomRule(
        label="MiC Module",
        keywords=["mic", "modular integrated", "modular construction",
                  "prefab module"],
        gfa_rule=InclusionRule.EXCLUDED,
        gfa_multiplier=0.0,
        gfa_note="MiC modules exempt per APP-151 item 38. ⚠️ Cap-exempt status pending full AP/QS confirmation (Q3.3 not confirmed by reviewer).",
        is_concession=True,
        concession_item="APP-151 item 38",
        subject_to_cap=False,
        requires_beam_plus=False,
        nofa_rule=InclusionRule.EXCLUDED, nofa_multiplier=0.0,
    ),
]


# ─── Lookup helpers ───────────────────────────────────────────────────────────

def _find_rule(room_label: str) -> Optional[RoomRule]:
    """
    Find the best matching RoomRule for a given room label.
    Matching is case-insensitive keyword search (longest match wins).
    """
    label_lower = room_label.lower().strip()
    best_rule:    Optional[RoomRule] = None
    best_length:  int = -1

    for rule in ROOM_RULES:
        for kw in rule.keywords:
            if kw in label_lower and len(kw) > best_length:
                best_rule   = rule
                best_length = len(kw)

    return best_rule


def _apply_overrides(rule: RoomRule, building_type: BuildingType) -> dict:
    """Return a dict of field overrides for the given building type."""
    return rule.overrides.get(building_type, {})


# ─── Main classify function ───────────────────────────────────────────────────

def classify_room(
    room_label:    str,
    area_m2:       float,
    building_type: BuildingType | str = BuildingType.RESIDENTIAL,
) -> AreaClassification:
    """
    Classify a room and return its GFA and NOFA contributions.

    Args:
        room_label:    Human-readable room name or DWG layer label.
        area_m2:       Gross polygon area in m² (before any multiplier).
        building_type: BuildingType enum or string value.

    Returns:
        AreaClassification with full GFA / NOFA breakdown.
    """
    if isinstance(building_type, str):
        building_type = BuildingType(building_type)

    rule = _find_rule(room_label)

    if rule is None:
        # Unknown room type — default to FULL GFA, EXCLUDED NOFA, flag for review
        return AreaClassification(
            room_type=room_label,
            area_m2=area_m2,
            building_type=building_type,
            gfa_rule=InclusionRule.FULL,
            gfa_multiplier=1.0,
            gfa_area_m2=area_m2,
            gfa_note="⚠️ Unrecognised room type — defaulted to full GFA. Manual review required.",
            nofa_rule=InclusionRule.EXCLUDED,
            nofa_multiplier=0.0,
            nofa_area_m2=0.0,
            nofa_note="⚠️ Unrecognised room type — excluded from NOFA pending review.",
        )

    overrides = _apply_overrides(rule, building_type)

    gfa_rule       = overrides.get("gfa_rule",       rule.gfa_rule)
    gfa_multiplier = overrides.get("gfa_multiplier", rule.gfa_multiplier)
    gfa_note       = overrides.get("gfa_note",       rule.gfa_note)
    nofa_rule      = overrides.get("nofa_rule",      rule.nofa_rule)
    nofa_multiplier= overrides.get("nofa_multiplier",rule.nofa_multiplier)
    nofa_note      = overrides.get("nofa_note",      rule.nofa_note)

    return AreaClassification(
        room_type=room_label,
        area_m2=area_m2,
        building_type=building_type,
        gfa_rule=gfa_rule,
        gfa_multiplier=gfa_multiplier,
        gfa_area_m2=round(area_m2 * gfa_multiplier, 4),
        gfa_note=gfa_note,
        is_concession=rule.is_concession,
        concession_item=rule.concession_item,
        subject_to_cap=rule.subject_to_cap,
        requires_beam_plus=rule.requires_beam_plus,
        nofa_rule=nofa_rule,
        nofa_multiplier=nofa_multiplier,
        nofa_area_m2=round(area_m2 * nofa_multiplier, 4),
        nofa_note=nofa_note,
    )
