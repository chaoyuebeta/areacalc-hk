"""
batch_processor.py
──────────────────
Processes multiple floor plan files for an entire building in one pass.
Handles DWG auto-conversion, parallel parsing, building-level aggregation,
and produces a combined Excel area schedule.

Usage:
    from batch_processor import BatchProcessor, FloorSpec

    floors = [
        FloorSpec(path="B2.dxf",       floor="B2",  desc="Basement 2 — Carpark"),
        FloorSpec(path="B1.dxf",       floor="B1",  desc="Basement 1 — Services"),
        FloorSpec(path="GF.pdf",       floor="G/F", desc="Ground Floor — Lobby"),
        FloorSpec(path="typical.pdf",  floor="1/F", desc="Typical Floor",
                  repeat_for=["1/F","2/F","3/F","4/F","5/F"]),
        FloorSpec(path="roof.jpg",     floor="R/F", desc="Roof Plant"),
    ]

    processor = BatchProcessor(building_type="residential", project_name="Tower A")
    report    = processor.run(floors, output_excel="tower_a_schedule.xlsx")

    print(report.summary())
"""

from __future__ import annotations

import os
import logging
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

import sys
sys.path.insert(0, os.path.dirname(__file__))

from room_rules      import BuildingType
from area_calculator import AreaCalculator, RoomInput, BuildingReport
from floor_plan_parser import parse_floor_plan, rooms_from_extracted, ExtractedRoom
from excel_exporter  import export_to_excel
from dwg_converter   import convert_dwg, ConversionResult


# ─── Floor specification ──────────────────────────────────────────────────────

@dataclass
class FloorSpec:
    """
    Describes one floor (or a set of identical repeated floors) for batch processing.

    Args:
        path:        Path to the floor plan file (.dwg, .dxf, .pdf, .jpg, .png).
        floor:       Floor label, e.g. "3/F".
        desc:        Optional human description, e.g. "Typical Residential Floor".
        scale:       Drawing scale denominator (default 100 = 1:100).
                     Auto-detected from title block when possible.
        repeat_for:  If set, this floor plan is used for multiple identical floors.
                     E.g. repeat_for=["1/F","2/F","3/F"] replicates rooms across
                     all three floors with individual floor labels.
    """
    path:       str
    floor:      str
    desc:       str  = ""
    scale:      int  = 100
    repeat_for: list[str] = field(default_factory=list)


# ─── Per-floor parse result ───────────────────────────────────────────────────

@dataclass
class FloorParseResult:
    spec:          FloorSpec
    rooms:         list[RoomInput]
    extracted:     list[ExtractedRoom]
    success:       bool
    error:         str  = ""
    dwg_converted: bool = False
    dwg_backend:   str  = ""


# ─── Batch report ─────────────────────────────────────────────────────────────

@dataclass
class BatchReport:
    building_report:  BuildingReport
    floor_results:    list[FloorParseResult]
    excel_path:       Optional[str]
    project_name:     str
    building_type:    BuildingType

    @property
    def floors_ok(self)    -> int: return sum(1 for r in self.floor_results if r.success)
    @property
    def floors_failed(self)-> int: return sum(1 for r in self.floor_results if not r.success)
    @property
    def total_floors(self) -> int: return len(self.floor_results)

    def summary(self) -> str:
        b = self.building_report
        lines = [
            "═" * 64,
            f"  BATCH REPORT — {self.project_name}",
            f"  Building type: {self.building_type.value}",
            "═" * 64,
            f"  Floors processed : {self.floors_ok} / {self.total_floors}",
        ]
        if self.floors_failed:
            lines.append(f"  Floors failed    : {self.floors_failed}")
        lines += [
            "─" * 64,
            f"  Total GFA        : {b.total_gfa_m2:>10.2f} m²",
            f"  Total NOFA       : {b.total_nofa_m2:>10.2f} m²",
            f"  NOFA / GFA       : {b.nofa_gfa_ratio:>9.1%}",
            f"  10% Cap used     : {b.cap_utilisation_pct:>9.1f}%"
            + ("  ⚠️  EXCEEDED" if b.cap_exceeded else ""),
        ]
        if self.excel_path:
            lines.append(f"  Excel output     : {self.excel_path}")
        lines.append("─" * 64)

        # Per-floor breakdown
        lines.append("  FLOORS")
        lines.append("─" * 64)
        floor_totals: dict[str, dict] = {}
        for r in b.rooms:
            f = r.input.floor
            if f not in floor_totals:
                floor_totals[f] = {"gfa": 0.0, "nofa": 0.0, "rooms": 0}
            floor_totals[f]["gfa"]   += r.gfa_area_m2
            floor_totals[f]["nofa"]  += r.nofa_area_m2
            floor_totals[f]["rooms"] += 1

        for fr in self.floor_results:
            lbl = fr.spec.floor
            if fr.success and lbl in floor_totals:
                t = floor_totals[lbl]
                conv = f" [DWG→DXF:{fr.dwg_backend}]" if fr.dwg_converted else ""
                lines.append(
                    f"  {lbl:<8}  GFA {t['gfa']:>8.1f} m²  "
                    f"NOFA {t['nofa']:>8.1f} m²  "
                    f"{t['rooms']:>3} rooms{conv}"
                )
            else:
                lines.append(f"  {lbl:<8}  ❌ {fr.error or 'parse failed'}")

        if b.warnings:
            lines += ["─" * 64, "  WARNINGS"]
            for w in b.warnings:
                lines.append(f"  ⚠️  {w}")

        lines.append("═" * 64)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        d = self.building_report.to_dict()
        d.update({
            "project_name":   self.project_name,
            "building_type":  self.building_type.value,
            "floors_total":   self.total_floors,
            "floors_ok":      self.floors_ok,
            "floors_failed":  self.floors_failed,
            "excel_path":     self.excel_path,
            "floor_results":  [
                {
                    "floor":         fr.spec.floor,
                    "desc":          fr.spec.desc,
                    "file":          fr.spec.path,
                    "success":       fr.success,
                    "rooms_parsed":  len(fr.rooms),
                    "error":         fr.error,
                    "dwg_converted": fr.dwg_converted,
                    "dwg_backend":   fr.dwg_backend,
                }
                for fr in self.floor_results
            ],
        })
        return d


# ─── BatchProcessor ───────────────────────────────────────────────────────────

class BatchProcessor:
    """
    Orchestrates multi-floor building analysis.

    Steps per floor:
      1. DWG → DXF conversion (if needed)
      2. Floor plan parsing
      3. Room classification
    Then:
      4. Building-level aggregation (APP-151 cap across whole building)
      5. Excel export
    """

    def __init__(
        self,
        building_type:     BuildingType | str = BuildingType.RESIDENTIAL,
        project_name:      str               = "Floor Plan Area Calculator",
        max_parse_workers: int               = 4,
        dwg_output_dir:    Optional[str]     = None,
        on_progress:       Optional[Callable] = None,
    ):
        """
        Args:
            building_type:     Applies to the whole building.
            project_name:      Used in Excel headers.
            max_parse_workers: Parallel file parse threads.
            dwg_output_dir:    Where to put converted DXF files.
                               Defaults to same directory as source DWG.
            on_progress:       Callback(floor_label, status, detail).
        """
        self.building_type = (
            BuildingType(building_type) if isinstance(building_type, str)
            else building_type
        )
        self.project_name      = project_name
        self.max_parse_workers = max_parse_workers
        self.dwg_output_dir    = dwg_output_dir
        self.on_progress       = on_progress or (lambda *a: None)

    # ── DWG handling ──────────────────────────────────────────────────────────

    def _resolve_path(self, spec: FloorSpec) -> tuple[str, bool, str]:
        """
        Return (effective_path, was_converted, backend_used).
        Converts DWG → DXF if necessary.
        """
        path = Path(spec.path)
        if path.suffix.lower() != ".dwg":
            return str(path), False, ""

        self.on_progress(spec.floor, "converting", f"DWG → DXF: {path.name}")
        result: ConversionResult = convert_dwg(
            str(path),
            output_dir=self.dwg_output_dir,
        )
        if not result.success:
            raise RuntimeError(f"DWG conversion failed: {result.error}")
        for w in result.warnings:
            logger.warning(f"DWG conversion warning [{spec.floor}]: {w}")

        return result.output_path, True, result.backend_used

    # ── Single-floor parse ────────────────────────────────────────────────────

    def _parse_floor(self, spec: FloorSpec) -> FloorParseResult:
        try:
            eff_path, converted, backend = self._resolve_path(spec)
            self.on_progress(spec.floor, "parsing", eff_path)

            extracted   = parse_floor_plan(eff_path, floor=spec.floor, scale=spec.scale)
            room_inputs = rooms_from_extracted(extracted)

            self.on_progress(spec.floor, "done", f"{len(room_inputs)} rooms extracted")

            return FloorParseResult(
                spec=spec,
                rooms=room_inputs,
                extracted=extracted,
                success=True,
                dwg_converted=converted,
                dwg_backend=backend,
            )

        except Exception as e:
            logger.error(f"Floor '{spec.floor}' parse error: {e}", exc_info=True)
            self.on_progress(spec.floor, "error", str(e))
            return FloorParseResult(
                spec=spec,
                rooms=[],
                extracted=[],
                success=False,
                error=str(e),
            )

    # ── Repeat expansion ──────────────────────────────────────────────────────

    def _expand_repeats(self, specs: list[FloorSpec]) -> list[FloorSpec]:
        """
        Expand FloorSpec.repeat_for into individual FloorSpec objects.
        E.g. a typical floor plan with repeat_for=["2/F","3/F","4/F"]
        becomes three separate specs sharing the same source file.
        """
        expanded = []
        for spec in specs:
            if spec.repeat_for:
                for floor_label in spec.repeat_for:
                    new_spec = FloorSpec(
                        path=spec.path,
                        floor=floor_label,
                        desc=f"{spec.desc} (repeated)",
                        scale=spec.scale,
                    )
                    expanded.append(new_spec)
            else:
                expanded.append(spec)
        return expanded

    # ── Main entry point ──────────────────────────────────────────────────────

    def run(
        self,
        floors:           list[FloorSpec],
        output_excel:     Optional[str] = None,
        fail_fast:        bool          = False,
    ) -> BatchReport:
        """
        Process all floors and return a BatchReport.

        Args:
            floors:       List of FloorSpec objects.
            output_excel: If set, save Excel schedule to this path.
            fail_fast:    If True, abort on first parse error.
        """
        if not floors:
            raise ValueError("No floors provided.")

        expanded = self._expand_repeats(floors)
        logger.info(
            f"Starting batch: {len(expanded)} floor(s), "
            f"building_type={self.building_type.value}, "
            f"project='{self.project_name}'"
        )

        # ── Parse floors in parallel ─────────────────────────────────────────
        floor_results: list[FloorParseResult] = [None] * len(expanded)

        with ThreadPoolExecutor(max_workers=self.max_parse_workers) as pool:
            future_to_idx = {
                pool.submit(self._parse_floor, spec): i
                for i, spec in enumerate(expanded)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                result = future.result()
                floor_results[idx] = result

                if fail_fast and not result.success:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError(
                        f"Aborting batch: floor '{result.spec.floor}' failed — "
                        f"{result.error}"
                    )

        # ── Aggregate all rooms ──────────────────────────────────────────────
        all_rooms: list[RoomInput] = []
        for fr in floor_results:
            if fr and fr.success:
                all_rooms.extend(fr.rooms)

        if not all_rooms:
            raise RuntimeError(
                "No rooms could be extracted from any floor. "
                "Check that files contain readable text labels."
            )

        logger.info(f"Total rooms to classify: {len(all_rooms)}")

        # ── Run rules engine ─────────────────────────────────────────────────
        calc   = AreaCalculator(self.building_type)
        report = calc.calculate(all_rooms)

        # ── Excel export ─────────────────────────────────────────────────────
        excel_path = None
        if output_excel:
            try:
                export_to_excel(report, output_excel, project_name=self.project_name)
                excel_path = str(output_excel)
                logger.info(f"Excel saved: {excel_path}")
            except Exception as e:
                logger.error(f"Excel export failed: {e}", exc_info=True)

        return BatchReport(
            building_report=report,
            floor_results=floor_results,
            excel_path=excel_path,
            project_name=self.project_name,
            building_type=self.building_type,
        )
