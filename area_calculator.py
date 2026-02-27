"""
area_calculator.py
──────────────────
Aggregates room-level classifications into floor / building totals,
enforces the APP-151 10% concession cap, and returns a structured report.

Usage:
    from area_calculator import AreaCalculator, RoomInput

    rooms = [
        RoomInput(label="Master Bedroom", area_m2=14.2, floor="3/F"),
        RoomInput(label="Balcony",        area_m2=4.5,  floor="3/F"),
        RoomInput(label="Bathroom",       area_m2=5.0,  floor="3/F"),
        RoomInput(label="Lift Shaft",     area_m2=3.0,  floor="3/F"),
    ]
    calc   = AreaCalculator(building_type="residential")
    report = calc.calculate(rooms)
    print(report.summary())
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from room_rules import classify_room, AreaClassification, BuildingType, InclusionRule


# ─── Input / output types ─────────────────────────────────────────────────────

@dataclass
class RoomInput:
    label:         str
    area_m2:       float
    floor:         str  = "—"
    room_id:       str  = ""   # optional unique identifier from DWG


@dataclass
class RoomResult:
    input:          RoomInput
    classification: AreaClassification

    @property
    def gfa_area_m2(self)  -> float: return self.classification.gfa_area_m2
    @property
    def nofa_area_m2(self) -> float: return self.classification.nofa_area_m2
    @property
    def area_m2(self)      -> float: return self.input.area_m2


@dataclass
class ConcessionSummary:
    item:             str
    description:      str
    total_area_m2:    float
    effective_gfa_m2: float   # after multiplier
    subject_to_cap:   bool
    requires_beam_plus: bool
    cap_warning:      bool = False


@dataclass
class BuildingReport:
    building_type:    BuildingType
    rooms:            list[RoomResult]

    # Totals
    total_polygon_m2: float   # raw sum of all polygon areas
    total_gfa_m2:     float   # effective GFA after all rules
    total_nofa_m2:    float

    # Concessions
    concessions:           list[ConcessionSummary]
    capped_total_m2:       float   # sum of cap-subject concessions
    cap_limit_m2:          float   # 10% of total_gfa_m2
    cap_exceeded:          bool
    cap_utilisation_pct:   float   # capped_total / cap_limit * 100

    # Ratios
    nofa_gfa_ratio:   float   # NOFA / GFA

    # Warnings
    warnings:         list[str] = field(default_factory=list)

    # ── Formatted summary ────────────────────────────────────────────────────
    def summary(self) -> str:
        lines = [
            "═" * 60,
            f"  AREA SCHEDULE — {self.building_type.value.upper()}",
            "═" * 60,
            f"  Total polygon area : {self.total_polygon_m2:>10.2f} m²",
            f"  Total GFA          : {self.total_gfa_m2:>10.2f} m²",
            f"  Total NOFA         : {self.total_nofa_m2:>10.2f} m²",
            f"  NOFA / GFA ratio   : {self.nofa_gfa_ratio:>9.1%}",
            "─" * 60,
            "  APP-151 CONCESSIONS",
            "─" * 60,
        ]
        for c in self.concessions:
            cap_flag = "  ← SUBJECT TO CAP" if c.subject_to_cap else ""
            bp_flag  = "  [BEAM Plus req'd]" if c.requires_beam_plus else ""
            lines.append(
                f"  {c.item:<22}  {c.effective_gfa_m2:>8.2f} m²{cap_flag}{bp_flag}"
            )
        lines += [
            "─" * 60,
            f"  Capped concessions : {self.capped_total_m2:>10.2f} m²",
            f"  10% cap limit      : {self.cap_limit_m2:>10.2f} m²",
            f"  Cap utilisation    : {self.cap_utilisation_pct:>9.1f}%"
            + ("  ⚠️  CAP EXCEEDED" if self.cap_exceeded else ""),
        ]
        if self.warnings:
            lines += ["─" * 60, "  WARNINGS"]
            for w in self.warnings: lines.append(f"  ⚠️  {w}")
        lines.append("═" * 60)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialise to a plain dict (e.g. for JSON API response)."""
        return {
            "building_type":       self.building_type.value,
            "total_polygon_m2":    round(self.total_polygon_m2, 4),
            "total_gfa_m2":        round(self.total_gfa_m2, 4),
            "total_nofa_m2":       round(self.total_nofa_m2, 4),
            "nofa_gfa_ratio":      round(self.nofa_gfa_ratio, 4),
            "cap_limit_m2":        round(self.cap_limit_m2, 4),
            "capped_total_m2":     round(self.capped_total_m2, 4),
            "cap_utilisation_pct": round(self.cap_utilisation_pct, 2),
            "cap_exceeded":        self.cap_exceeded,
            "warnings":            self.warnings,
            "rooms": [
                {
                    "id":           r.input.room_id,
                    "label":        r.input.label,
                    "floor":        r.input.floor,
                    "polygon_m2":   round(r.area_m2, 4),
                    "gfa_m2":       round(r.gfa_area_m2, 4),
                    "nofa_m2":      round(r.nofa_area_m2, 4),
                    "gfa_rule":     r.classification.gfa_rule.value,
                    "nofa_rule":    r.classification.nofa_rule.value,
                    "concession":   r.classification.concession_item,
                    "subject_to_cap": r.classification.subject_to_cap,
                    "gfa_note":     r.classification.gfa_note,
                    "nofa_note":    r.classification.nofa_note,
                }
                for r in self.rooms
            ],
            "concessions": [
                {
                    "item":             c.item,
                    "description":      c.description,
                    "total_area_m2":    round(c.total_area_m2, 4),
                    "effective_gfa_m2": round(c.effective_gfa_m2, 4),
                    "subject_to_cap":   c.subject_to_cap,
                    "requires_beam_plus": c.requires_beam_plus,
                    "cap_warning":      c.cap_warning,
                }
                for c in self.concessions
            ],
        }


# ─── Calculator ───────────────────────────────────────────────────────────────

class AreaCalculator:
    """
    Classifies a list of rooms and produces a BuildingReport.

    For COMPOSITE buildings, pass building_type="composite" and use
    the `domestic_floors` / `non_domestic_floors` filters when calling
    calculate() to get separate cap calculations per part.
    """

    CAP_RATE = 0.10  # APP-151: 10% of GFA

    def __init__(self, building_type: BuildingType | str = BuildingType.RESIDENTIAL):
        self.building_type = (
            BuildingType(building_type) if isinstance(building_type, str)
            else building_type
        )

    def calculate(self, rooms: list[RoomInput]) -> BuildingReport:
        warnings: list[str] = []

        # ── Classify each room ───────────────────────────────────────────────
        results: list[RoomResult] = []
        for rm in rooms:
            cls = classify_room(rm.label, rm.area_m2, self.building_type)
            results.append(RoomResult(input=rm, classification=cls))
            if "⚠️" in cls.gfa_note:
                warnings.append(f"Room '{rm.label}' (floor {rm.floor}): {cls.gfa_note}")

        # ── Aggregate totals ─────────────────────────────────────────────────
        total_polygon = sum(r.area_m2      for r in results)
        total_gfa     = sum(r.gfa_area_m2  for r in results)
        total_nofa    = sum(r.nofa_area_m2 for r in results)

        # ── Aggregate concessions ────────────────────────────────────────────
        concession_map: dict[str, dict] = {}
        for r in results:
            c = r.classification
            if not c.is_concession or not c.concession_item:
                continue
            key = c.concession_item
            if key not in concession_map:
                concession_map[key] = {
                    "item":             key,
                    "description":      c.gfa_note,
                    "total_area_m2":    0.0,
                    "effective_gfa_m2": 0.0,
                    "subject_to_cap":   c.subject_to_cap,
                    "requires_beam_plus": c.requires_beam_plus,
                }
            concession_map[key]["total_area_m2"]    += r.area_m2
            concession_map[key]["effective_gfa_m2"] += r.gfa_area_m2

        # ── 10% cap ──────────────────────────────────────────────────────────
        cap_limit     = total_gfa * self.CAP_RATE
        capped_total  = sum(
            v["effective_gfa_m2"]
            for v in concession_map.values()
            if v["subject_to_cap"]
        )
        cap_exceeded  = capped_total > cap_limit

        concession_list = []
        for v in concession_map.values():
            cap_warn = v["subject_to_cap"] and cap_exceeded
            concession_list.append(ConcessionSummary(
                item=v["item"],
                description=v["description"],
                total_area_m2=v["total_area_m2"],
                effective_gfa_m2=v["effective_gfa_m2"],
                subject_to_cap=v["subject_to_cap"],
                requires_beam_plus=v["requires_beam_plus"],
                cap_warning=cap_warn,
            ))

        if cap_exceeded:
            warnings.append(
                f"APP-151 10% cap exceeded: capped concessions = "
                f"{capped_total:.2f} m² vs limit {cap_limit:.2f} m². "
                "BD approval required before submission."
            )

        # Warn when approaching cap (>80%)
        elif cap_limit > 0 and (capped_total / cap_limit) >= 0.80:
            warnings.append(
                f"Approaching APP-151 10% cap: "
                f"{(capped_total/cap_limit)*100:.1f}% utilised "
                f"({capped_total:.2f} m² of {cap_limit:.2f} m²)."
            )

        nofa_gfa_ratio   = (total_nofa / total_gfa) if total_gfa > 0 else 0.0
        cap_utilisation  = (capped_total / cap_limit * 100) if cap_limit > 0 else 0.0

        return BuildingReport(
            building_type=self.building_type,
            rooms=results,
            total_polygon_m2=round(total_polygon, 4),
            total_gfa_m2=round(total_gfa, 4),
            total_nofa_m2=round(total_nofa, 4),
            concessions=concession_list,
            capped_total_m2=round(capped_total, 4),
            cap_limit_m2=round(cap_limit, 4),
            cap_exceeded=cap_exceeded,
            cap_utilisation_pct=round(cap_utilisation, 2),
            nofa_gfa_ratio=round(nofa_gfa_ratio, 4),
            warnings=warnings,
        )
