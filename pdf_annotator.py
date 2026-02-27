"""
pdf_annotator.py
─────────────────
Overlays GFA / NOFA area annotations onto the original floor plan PDF.

For each room in the BuildingReport, draws:
  • A semi-transparent colour-coded fill (green=full GFA, amber=half, red=excluded)
  • The room label (original text)
  • The calculated area  e.g. "14.20 m²"
  • The GFA rule badge   e.g. "[FULL]"

Uses:
  pdfplumber  — read original PDF geometry & locate room label positions
  reportlab   — draw annotation overlay onto a blank canvas
  pypdf       — merge original PDF page with annotation layer

Usage:
    from pdf_annotator import annotate_pdf
    out_path = annotate_pdf(
        pdf_path    = "floor_plan.pdf",
        report      = building_report,
        output_path = "annotated.pdf",
        project_name= "Tower A",
    )
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from area_calculator import BuildingReport, RoomResult

logger = logging.getLogger(__name__)


# ─── Colour scheme (R,G,B  0–1 floats) ───────────────────────────────────────

_COLOURS = {
    "full":     (0.17, 0.53, 0.25),   # green
    "half":     (0.72, 0.45, 0.00),   # amber
    "excluded": (0.69, 0.13, 0.13),   # red
    "conditional": (0.22, 0.40, 0.60), # blue
}
_FILL_ALPHA   = 0.18   # semi-transparent fill
_BADGE_ALPHA  = 0.85
_LABEL_COLOUR = (0.05, 0.05, 0.15)    # near-black


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _rule_colour(gfa_rule: str):
    return _COLOURS.get(gfa_rule.lower(), _COLOURS["conditional"])


def _match_rooms_to_positions(
    page_words: list[dict],
    report_rooms: list[RoomResult],
) -> list[tuple[RoomResult, float, float, float, float]]:
    """
    Attempt to match each RoomResult to a bounding box on the PDF page
    by fuzzy-matching the room label against extracted word groups.

    Returns list of (RoomResult, x0, y0, x1, y1) in PDF points
    where y0 is measured from bottom (reportlab convention).
    """
    import re

    # Build phrase list from page words grouped by proximity
    phrases: list[dict] = []
    used = [False] * len(page_words)
    for i, w in enumerate(page_words):
        if used[i]:
            continue
        group = [w]
        used[i] = True
        for j, w2 in enumerate(page_words):
            if used[j] or i == j:
                continue
            if (abs(w2["top"] - w["top"]) < 6 and
                    abs(w2["x0"] - w["x1"]) < 50):
                group.append(w2)
                used[j] = True
        text = " ".join(g["text"] for g in sorted(group, key=lambda g: g["x0"]))
        phrases.append({
            "text": text,
            "x0":   min(g["x0"]     for g in group),
            "top":  min(g["top"]    for g in group),
            "x1":   max(g["x1"]     for g in group),
            "bottom": max(g["bottom"] for g in group),
        })

    def _norm(s: str) -> str:
        return re.sub(r'\s+', ' ', s.lower().strip())

    matched: list[tuple[RoomResult, float, float, float, float]] = []
    used_phrases: set[int] = set()

    for room in report_rooms:
        label_norm = _norm(room.input.label)
        best_idx   = None
        best_score = 0.0

        for i, phrase in enumerate(phrases):
            if i in used_phrases:
                continue
            phrase_norm = _norm(phrase["text"])

            # Exact match
            if label_norm == phrase_norm:
                best_idx, best_score = i, 1.0
                break

            # Substring match
            if label_norm in phrase_norm or phrase_norm in label_norm:
                score = len(label_norm) / max(len(phrase_norm), 1)
                if score > best_score:
                    best_idx, best_score = i, score

            # Word overlap
            label_words   = set(label_norm.split())
            phrase_words  = set(phrase_norm.split())
            overlap = len(label_words & phrase_words)
            if overlap > 0:
                score = overlap / max(len(label_words), 1) * 0.8
                if score > best_score:
                    best_idx, best_score = i, score

        if best_idx is not None and best_score >= 0.4:
            p = phrases[best_idx]
            used_phrases.add(best_idx)
            # Expand bounding box slightly for the annotation box
            pad = 6
            matched.append((room, p["x0"] - pad, p["top"] - pad,
                             p["x1"] + pad, p["bottom"] + pad))
        else:
            logger.debug(f"No position found for room '{room.input.label}'")

    return matched


def _build_overlay(
    page_width:  float,
    page_height: float,
    matches: list[tuple[RoomResult, float, float, float, float]],
    show_gfa_rule: bool = True,
) -> bytes:
    """
    Build a reportlab PDF page (same size as original) with annotations.
    Returns raw PDF bytes.
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.colors import Color
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.units import pt

    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))

    # pdfplumber uses top-left origin; reportlab uses bottom-left
    # conversion: rl_y = page_height - pdf_y
    def _rl_y(pdf_y: float) -> float:
        return page_height - pdf_y

    for room, x0, top, x1, bottom in matches:
        gfa_rule = room.classification.gfa_rule.value
        colour   = _rule_colour(gfa_rule)
        rl_col   = Color(*colour, alpha=_FILL_ALPHA)
        border_col = Color(*colour, alpha=0.7)

        # Convert to reportlab coords
        rl_y0 = _rl_y(bottom)   # bottom of box in rl coords
        rl_y1 = _rl_y(top)      # top of box in rl coords
        box_w = x1 - x0
        box_h = rl_y1 - rl_y0

        # ── Semi-transparent fill ────────────────────────────────────────────
        c.saveState()
        c.setFillColor(rl_col)
        c.setStrokeColor(border_col)
        c.setLineWidth(0.6)
        c.roundRect(x0, rl_y0, box_w, box_h, radius=2, fill=1, stroke=1)
        c.restoreState()

        # ── Area label ───────────────────────────────────────────────────────
        area_text = f"{room.gfa_area_m2:.2f} m²"
        cx        = x0 + box_w / 2
        text_y    = rl_y0 + box_h / 2 - 4  # vertically centred

        # Background pill for the area text
        label_w  = len(area_text) * 5.5 + 8
        label_h  = 12
        pill_x   = cx - label_w / 2
        pill_y   = text_y - 1

        c.saveState()
        c.setFillColor(Color(*colour, alpha=_BADGE_ALPHA))
        c.roundRect(pill_x, pill_y, label_w, label_h, radius=2, fill=1, stroke=0)

        c.setFillColor(Color(1, 1, 1, alpha=1))  # white text
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(cx, pill_y + 3, area_text)
        c.restoreState()

        # GFA rule badge (small, above area)
        if show_gfa_rule:
            badge_text = gfa_rule.upper()
            badge_y    = text_y + 11
            c.saveState()
            c.setFillColor(Color(*colour, alpha=0.6))
            badge_w = len(badge_text) * 4.5 + 6
            c.roundRect(cx - badge_w/2, badge_y, badge_w, 9,
                        radius=1, fill=1, stroke=0)
            c.setFillColor(Color(1, 1, 1, alpha=1))
            c.setFont("Helvetica-Bold", 5.5)
            c.drawCentredString(cx, badge_y + 2, badge_text)
            c.restoreState()

    c.save()
    buf.seek(0)
    return buf.read()


def _build_legend(page_width: float, project_name: str,
                  total_gfa: float, total_nofa: float,
                  cap_pct: float, cap_exceeded: bool) -> bytes:
    """Build a legend/summary box to overlay in the bottom-left corner."""
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.colors import Color, white, black

    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(page_width, 200))  # tall enough

    # Legend box dimensions
    lx, ly = 16, 16
    lw, lh = 200, 130

    # Background
    c.saveState()
    c.setFillColor(Color(0.98, 0.97, 0.94, alpha=0.93))
    c.setStrokeColor(Color(0.05, 0.06, 0.10, alpha=0.9))
    c.setLineWidth(1.0)
    c.roundRect(lx, ly, lw, lh, radius=3, fill=1, stroke=1)
    c.restoreState()

    # Title bar
    c.saveState()
    c.setFillColor(Color(0.05, 0.06, 0.10, alpha=1))
    c.roundRect(lx, ly + lh - 20, lw, 20, radius=3, fill=1, stroke=0)
    c.rect(lx, ly + lh - 20, lw, 12, fill=1, stroke=0)  # square bottom
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(lx + 8, ly + lh - 13, f"AreaCalc HK  ·  {project_name}")
    c.restoreState()

    # Colour key
    items = [
        ("full",       "Full GFA"),
        ("half",       "Half GFA (50%)"),
        ("excluded",   "Excluded"),
        ("conditional","Conditional"),
    ]
    ky = ly + lh - 32
    for rule, label in items:
        col = _rule_colour(rule)
        c.saveState()
        c.setFillColor(Color(*col, alpha=0.85))
        c.rect(lx + 8, ky - 1, 10, 8, fill=1, stroke=0)
        c.setFillColor(black)
        c.setFont("Helvetica", 7)
        c.drawString(lx + 22, ky + 1, label)
        c.restoreState()
        ky -= 13

    # Summary figures
    ky -= 4
    c.saveState()
    c.setStrokeColor(Color(0.8, 0.78, 0.72))
    c.setLineWidth(0.5)
    c.line(lx + 8, ky + 8, lx + lw - 8, ky + 8)
    c.restoreState()

    ky -= 4
    summary = [
        (f"GFA:  {total_gfa:.1f} m²",   (0.10, 0.36, 0.55)),
        (f"NOFA: {total_nofa:.1f} m²",  (0.55, 0.20, 0.04)),
        (f"Cap:  {cap_pct:.1f}%{'  ⚠' if cap_exceeded else ''}",
         (0.69, 0.13, 0.13) if cap_exceeded else (0.17, 0.42, 0.23)),
    ]
    for text, col in summary:
        c.saveState()
        c.setFillColor(Color(*col))
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(lx + 8, ky, text)
        c.restoreState()
        ky -= 12

    c.save()
    buf.seek(0)
    return buf.read()


# ─── Public API ───────────────────────────────────────────────────────────────

def annotate_pdf(
    pdf_path:     str,
    report:       BuildingReport,
    output_path:  str,
    project_name: str = "Floor Plan",
    show_gfa_rule: bool = True,
    show_legend:   bool = True,
) -> str:
    """
    Overlay GFA/NOFA area annotations onto the original floor plan PDF.

    Args:
        pdf_path:      Path to the original floor plan PDF.
        report:        BuildingReport from AreaCalculator.calculate().
        output_path:   Path to write the annotated PDF.
        project_name:  Shown in the legend box.
        show_gfa_rule: Whether to show the GFA rule badge on each room.
        show_legend:   Whether to add the legend/summary box.

    Returns:
        Absolute path to the saved annotated PDF.

    Raises:
        ImportError: If reportlab or pypdf is missing.
        FileNotFoundError: If pdf_path does not exist.
    """
    try:
        import pdfplumber
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as _  # noqa — just check import
    except ImportError as e:
        raise ImportError(
            f"Missing dependency for PDF annotation: {e}\n"
            "Install with: pip install reportlab pypdf pdfplumber"
        )

    if not Path(pdf_path).exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Index report rooms by floor
    rooms_by_floor: dict[str, list[RoomResult]] = {}
    for r in report.rooms:
        rooms_by_floor.setdefault(r.input.floor, []).append(r)

    reader  = PdfReader(pdf_path)
    writer  = PdfWriter()

    with pdfplumber.open(pdf_path) as plumb_pdf:
        for page_num, (pdf_page, plumb_page) in enumerate(
                zip(reader.pages, plumb_pdf.pages)):

            page_w = float(pdf_page.mediabox.width)
            page_h = float(pdf_page.mediabox.height)

            # Extract words for position matching
            words = plumb_page.extract_words(
                x_tolerance=3, y_tolerance=3,
                keep_blank_chars=False,
            )

            # Use all rooms (multi-floor drawings will share one PDF page)
            all_rooms = [r for rooms in rooms_by_floor.values() for r in rooms]
            matches   = _match_rooms_to_positions(words, all_rooms)

            logger.info(
                f"Page {page_num+1}: matched {len(matches)}/{len(all_rooms)} rooms"
            )

            # Build annotation overlay
            overlay_bytes = _build_overlay(page_w, page_h, matches, show_gfa_rule)

            # Merge overlay onto original page
            overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
            overlay_page   = overlay_reader.pages[0]
            pdf_page.merge_page(overlay_page)

            # Add legend overlay (bottom-left corner, page 1 only)
            if show_legend and page_num == 0:
                legend_bytes  = _build_legend(
                    page_w, project_name,
                    report.total_gfa_m2, report.total_nofa_m2,
                    report.cap_utilisation_pct, report.cap_exceeded,
                )
                legend_reader = PdfReader(io.BytesIO(legend_bytes))
                legend_page   = legend_reader.pages[0]
                pdf_page.merge_page(legend_page)

            writer.add_page(pdf_page)

    # Add metadata
    writer.add_metadata({
        "/Title":   f"Annotated Floor Plan — {project_name}",
        "/Subject": "GFA / NOFA Area Annotations — PNAP APP-2 & APP-151",
        "/Creator": "AreaCalc HK v1.0",
    })

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        writer.write(f)

    logger.info(f"Annotated PDF saved: {out}")
    return str(out)
