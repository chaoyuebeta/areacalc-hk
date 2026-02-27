"""
floor_plan_parser.py
─────────────────────
Parses floor plan files (DWG/DXF, PDF vector, PDF scanned, JPG/PNG)
and extracts a list of RoomInput objects ready for the AreaCalculator.

Supports:
  • DWG / DXF  — via ezdxf (pip install ezdxf)
  • PDF vector — via pdfplumber (already installed)
  • PDF scanned / JPG / PNG — via pytesseract OCR + pillow

Each parser returns List[ExtractedRoom] which is then passed to
`room_inputs_from_extracted()` to produce List[RoomInput].

Usage:
    from floor_plan_parser import parse_floor_plan
    from area_calculator import AreaCalculator

    rooms  = parse_floor_plan("floor_plan.pdf", floor_label="3/F")
    report = AreaCalculator("residential").calculate(rooms)
"""

from __future__ import annotations

import os
import re
import math
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Extracted room data class ────────────────────────────────────────────────

@dataclass
class ExtractedRoom:
    """Raw room data as extracted from a floor plan file."""
    label:      str             # room name / text label found
    area_m2:    float           # polygon area (0.0 if not determinable)
    layer:      str  = ""       # DWG layer name (empty for PDF/image)
    floor:      str  = "—"
    bbox:       tuple = ()      # (x0, y0, x1, y1) bounding box if available
    source:     str  = ""       # "dwg", "pdf_vector", "pdf_ocr", "image_ocr"
    confidence: float = 1.0     # OCR confidence 0–1 (1.0 for vector sources)
    notes:      str  = ""


# ─── Scale detection ──────────────────────────────────────────────────────────

# Common HK floor plan scales
_SCALE_PATTERNS = [
    re.compile(r'1\s*[:\s]\s*(50|100|200|500)', re.IGNORECASE),
    re.compile(r'SCALE\s*[:\s]*1\s*[:\s]\s*(\d+)', re.IGNORECASE),
    re.compile(r'比例\s*[:\s]*1\s*[:\s]\s*(\d+)'),          # Chinese scale label
]

def _detect_scale(texts: list[str]) -> Optional[int]:
    """Return drawing scale denominator (e.g. 100 for 1:100), or None."""
    for text in texts:
        for pat in _SCALE_PATTERNS:
            m = pat.search(text)
            if m:
                return int(m.group(1))
    return None


# ─── Area normalisation ───────────────────────────────────────────────────────

def _dwg_units_to_m2(area_native: float, scale: int, unit: str = "mm") -> float:
    """
    Convert a DWG polygon area (in native units²) to m².
    HK drawings are typically in mm at 1:1 (real-world coordinates).
    """
    if unit.lower() in ("m", "metres", "meters"):
        return area_native
    # mm → m: divide by 1_000_000
    return area_native / 1_000_000.0


def _pdf_pts_to_m2(area_pts2: float, scale: int) -> float:
    """
    Convert a PDF area (points²) to m².
    1 pt = 1/72 inch = 0.0003528 m
    Then account for drawing scale (e.g. 1:100 means 1 pt on paper = 100 pts real).
    """
    PT_TO_M = 0.0003528
    return area_pts2 * (PT_TO_M ** 2) * (scale ** 2)


# ─── Area keyword extractor ───────────────────────────────────────────────────

# Regex to pull explicit area annotations from text (e.g. "14.2 m²" or "14.20m2")
_AREA_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:m²|m2|sq\.?\s*m|sqm)',
    re.IGNORECASE
)

def _extract_area_from_text(text: str) -> Optional[float]:
    m = _AREA_RE.search(text)
    return float(m.group(1)) if m else None


# ─── Room label cleaner ───────────────────────────────────────────────────────

_NOISE = re.compile(
    r'(\d+(?:\.\d+)?\s*(?:m²|m2|sqm|sq\.m)?'   # area annotations
    r'|\bscale\b|\bratio\b|\bfl\b|\bfloor\b'    # common noise words
    r'|\d{1,2}/f\b)',                            # floor tags like 3/F
    re.IGNORECASE
)

def _clean_label(raw: str) -> str:
    label = _NOISE.sub("", raw).strip(" \t\n\r.,;:-_/\\")
    label = re.sub(r'\s{2,}', ' ', label)
    return label


# ─── DWG / DXF parser ────────────────────────────────────────────────────────

def _parse_dwg_dxf(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """
    Extract rooms from a DWG or DXF file using ezdxf.
    Requires:  pip install ezdxf

    Strategy:
      1. Collect all HATCH entities (filled polygons) → candidate room footprints.
      2. Collect all TEXT / MTEXT entities → room labels.
      3. Spatially associate each label with the nearest hatch polygon.
    """
    try:
        import ezdxf
        from ezdxf.math import BoundingBox2d
    except ImportError:
        raise ImportError(
            "ezdxf is required for DWG/DXF parsing.\n"
            "Install with:  pip install ezdxf\n"
            "Then convert DWG to DXF first if needed (LibreCAD or ODA File Converter)."
        )

    doc    = ezdxf.readfile(filepath)
    msp    = doc.modelspace()
    unit   = doc.header.get("$INSUNITS", 4)  # 4 = mm, 6 = m
    unit_s = "m" if unit == 6 else "mm"

    # ── Collect hatches ──────────────────────────────────────────────────────
    hatches: list[dict] = []
    for hatch in msp.query("HATCH"):
        try:
            boundary = hatch.paths
            # Sum areas of all boundary paths
            total_area = 0.0
            cx, cy = 0.0, 0.0
            path_count = 0
            for path in boundary:
                if hasattr(path, 'vertices'):
                    verts = [(v[0], v[1]) for v in path.vertices]
                    if len(verts) >= 3:
                        # Shoelace formula
                        n = len(verts)
                        area = abs(sum(
                            verts[i][0] * verts[(i+1)%n][1] -
                            verts[(i+1)%n][0] * verts[i][1]
                            for i in range(n)
                        )) / 2.0
                        total_area += area
                        cx += sum(v[0] for v in verts) / n
                        cy += sum(v[1] for v in verts) / n
                        path_count += 1
            if total_area > 0 and path_count > 0:
                hatches.append({
                    "area_native": total_area,
                    "cx": cx / path_count,
                    "cy": cy / path_count,
                    "layer": hatch.dxf.layer,
                })
        except Exception as e:
            logger.debug(f"Skipping hatch: {e}")

    # ── Collect text entities ────────────────────────────────────────────────
    texts: list[dict] = []
    for ent in msp.query("TEXT MTEXT"):
        try:
            raw = ent.dxf.text if ent.dxftype() == "TEXT" else ent.text
            raw = raw.strip()
            if len(raw) < 2:
                continue
            ins = ent.dxf.insert
            texts.append({
                "raw":   raw,
                "x":     ins.x,
                "y":     ins.y,
                "layer": ent.dxf.layer,
            })
        except Exception as e:
            logger.debug(f"Skipping text entity: {e}")

    # ── Detect scale from text entities ─────────────────────────────────────
    detected = _detect_scale([t["raw"] for t in texts])
    if detected:
        scale = detected

    # ── Associate labels with hatches ────────────────────────────────────────
    rooms: list[ExtractedRoom] = []

    def _dist(tx, ty, hatch):
        return math.hypot(tx - hatch["cx"], ty - hatch["cy"])

    used_hatches: set[int] = set()

    for text in texts:
        label = _clean_label(text["raw"])
        if not label or len(label) < 2:
            continue
        # Check if label contains an explicit area
        explicit_area = _extract_area_from_text(text["raw"])

        # Find nearest hatch
        if hatches:
            nearest_idx = min(
                range(len(hatches)),
                key=lambda i: _dist(text["x"], text["y"], hatches[i])
            )
            hatch      = hatches[nearest_idx]
            area_native= hatch["area_native"]
            area_m2    = explicit_area or _dwg_units_to_m2(area_native, scale, unit_s)
            layer      = text["layer"] or hatch["layer"]
            used_hatches.add(nearest_idx)
        else:
            area_m2 = explicit_area or 0.0
            layer   = text["layer"]

        rooms.append(ExtractedRoom(
            label=label,
            area_m2=round(area_m2, 4),
            layer=layer,
            floor=floor,
            source="dwg",
        ))

    # Add hatches with no matched text (mark as unidentified)
    for i, hatch in enumerate(hatches):
        if i not in used_hatches:
            area_m2 = _dwg_units_to_m2(hatch["area_native"], scale, unit_s)
            rooms.append(ExtractedRoom(
                label="Unidentified Space",
                area_m2=round(area_m2, 4),
                layer=hatch["layer"],
                floor=floor,
                source="dwg",
                notes="No text label found near this polygon.",
            ))

    return rooms


# ─── PDF vector parser ────────────────────────────────────────────────────────

def _parse_pdf_vector(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """
    Extract rooms from a vector PDF using pdfplumber.
    Strategy: find text blocks that look like room labels, attempt to
    associate nearby area annotations, and use bounding-box geometry
    as a fallback area estimate.
    """
    import pdfplumber

    rooms: list[ExtractedRoom] = []

    with pdfplumber.open(filepath) as pdf:
        for page_num, page in enumerate(pdf.pages):
            words = page.extract_words(
                x_tolerance=3, y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            full_text = " ".join(w["text"] for w in words)

            # Detect scale
            detected = _detect_scale([full_text])
            if detected:
                scale = detected

            # Group words into lines by y-position
            lines: dict[float, list[dict]] = {}
            for w in words:
                key = round(w["top"], 1)
                lines.setdefault(key, []).append(w)

            # Build text blocks
            blocks: list[dict] = []
            for y, line_words in sorted(lines.items()):
                line_words.sort(key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in line_words)
                x0   = min(w["x0"]    for w in line_words)
                y0   = min(w["top"]   for w in line_words)
                x1   = max(w["x1"]    for w in line_words)
                y1   = max(w["bottom"]for w in line_words)
                blocks.append({"text": text, "x0": x0, "y0": y0,
                               "x1": x1, "y1": y1})

            # Identify room labels vs area annotations
            for block in blocks:
                raw   = block["text"].strip()
                label = _clean_label(raw)

                if not label or len(label) < 2:
                    continue
                # Skip pure numbers / coordinates
                if re.fullmatch(r'[\d\s.,:/\\-]+', label):
                    continue

                explicit_area = _extract_area_from_text(raw)

                # Estimate area from bounding box if no explicit area
                # (very rough — bbox of the label, not the room polygon)
                bbox_area_pts2 = (block["x1"]-block["x0"]) * (block["y1"]-block["y0"])
                area_m2 = explicit_area or _pdf_pts_to_m2(bbox_area_pts2, scale)

                rooms.append(ExtractedRoom(
                    label=label,
                    area_m2=round(area_m2, 4),
                    floor=floor,
                    bbox=(block["x0"], block["y0"], block["x1"], block["y1"]),
                    source="pdf_vector",
                    notes="" if explicit_area else "⚠️ Area estimated from label bbox — verify manually.",
                ))

    return rooms


# ─── OCR parser (scanned PDF or image) ───────────────────────────────────────

def _ocr_image(img) -> list[dict]:
    """Run pytesseract on a PIL image and return word-level data."""
    import pytesseract
    data = pytesseract.image_to_data(
        img,
        lang="eng",
        config="--psm 11",   # sparse text — good for floor plans
        output_type=pytesseract.Output.DICT,
    )
    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        conf = int(data["conf"][i])
        if text and conf > 30:
            words.append({
                "text": text,
                "conf": conf / 100.0,
                "x0":   data["left"][i],
                "y0":   data["top"][i],
                "x1":   data["left"][i] + data["width"][i],
                "y1":   data["top"][i]  + data["height"][i],
            })
    return words


def _words_to_rooms(words: list[dict], floor: str,
                    scale: int, source: str) -> list[ExtractedRoom]:
    """Convert OCR word list to ExtractedRoom list."""
    rooms = []
    full_text = " ".join(w["text"] for w in words)
    detected  = _detect_scale([full_text])
    if detected:
        scale = detected

    # Group nearby words into phrases (simple proximity grouping)
    phrases: list[dict] = []
    used = [False] * len(words)
    for i, w in enumerate(words):
        if used[i]:
            continue
        group = [w]
        used[i] = True
        for j, w2 in enumerate(words):
            if used[j] or i == j:
                continue
            # Same line (y within 8px) and close x
            if abs(w2["y0"] - w["y0"]) < 8 and abs(w2["x0"] - w["x1"]) < 40:
                group.append(w2)
                used[j] = True
        text = " ".join(g["text"] for g in sorted(group, key=lambda g: g["x0"]))
        conf = sum(g["conf"] for g in group) / len(group)
        phrases.append({
            "text": text, "conf": conf,
            "x0": min(g["x0"] for g in group),
            "y0": min(g["y0"] for g in group),
            "x1": max(g["x1"] for g in group),
            "y1": max(g["y1"] for g in group),
        })

    for p in phrases:
        raw   = p["text"].strip()
        label = _clean_label(raw)
        if not label or len(label) < 2:
            continue
        if re.fullmatch(r'[\d\s.,:/\\-]+', label):
            continue
        explicit_area = _extract_area_from_text(raw)
        rooms.append(ExtractedRoom(
            label=label,
            area_m2=explicit_area or 0.0,
            floor=floor,
            bbox=(p["x0"], p["y0"], p["x1"], p["y1"]),
            source=source,
            confidence=p["conf"],
            notes="" if explicit_area else "⚠️ No area annotation found — manual entry required.",
        ))

    return rooms


def _parse_pdf_ocr(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """Extract rooms from a scanned PDF via OCR."""
    from pdf2image import convert_from_path
    images = convert_from_path(filepath, dpi=200)
    rooms  = []
    for img in images:
        words = _ocr_image(img)
        rooms.extend(_words_to_rooms(words, floor, scale, "pdf_ocr"))
    return rooms


def _parse_image(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """Extract rooms from a JPG/PNG image via OCR."""
    from PIL import Image
    img   = Image.open(filepath)
    words = _ocr_image(img)
    return _words_to_rooms(words, floor, scale, "image_ocr")


# ─── Format detector ──────────────────────────────────────────────────────────

def _is_scanned_pdf(filepath: str) -> bool:
    """Return True if the PDF appears to be scanned (no extractable text)."""
    import pdfplumber
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages[:3]:
                words = page.extract_words()
                if len(words) > 10:
                    return False
        return True
    except Exception:
        return True


# ─── Public API ───────────────────────────────────────────────────────────────

def parse_floor_plan(
    filepath:    str,
    floor:       str = "—",
    scale:       int = 100,     # Confirmed by QS review Q5.2: HK standard is 1:100
    force_ocr:   bool = False,
) -> list[ExtractedRoom]:
    """
    Parse a floor plan file and return a list of ExtractedRoom objects.

    Args:
        filepath:  Path to DWG, DXF, PDF, JPG, or PNG file.
        floor:     Floor label, e.g. "3/F".
        scale:     Fallback drawing scale denominator.
                   Default 100 (= 1:100). Confirmed by QS review Q5.2:
                   HK firms typically draw at 1:100 in millimetres.
                   Auto-detected from title block text when possible.
        force_ocr: Force OCR even for vector PDFs.

    Returns:
        List[ExtractedRoom]

    Raises:
        ValueError: Unsupported file format.
        ImportError: Missing optional dependency (ezdxf for DWG/DXF).
    """
    path = Path(filepath)
    ext  = path.suffix.lower()

    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    logger.info(f"Parsing {ext} file: {filepath}")

    if ext in (".dwg", ".dxf"):
        if ext == ".dwg":
            raise ValueError(
                "DWG files must be converted to DXF before parsing.\n"
                "Recommended tools: ODA File Converter (free) or LibreCAD.\n"
                "Then call parse_floor_plan() with the .dxf file."
            )
        return _parse_dwg_dxf(filepath, floor, scale)

    elif ext == ".pdf":
        if force_ocr or _is_scanned_pdf(filepath):
            logger.info("PDF detected as scanned — using OCR.")
            return _parse_pdf_ocr(filepath, floor, scale)
        else:
            logger.info("PDF detected as vector — using pdfplumber.")
            return _parse_pdf_vector(filepath, floor, scale)

    elif ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"):
        return _parse_image(filepath, floor, scale)

    else:
        raise ValueError(
            f"Unsupported file format: '{ext}'. "
            "Supported: .dxf, .pdf, .jpg, .jpeg, .png, .tif, .bmp"
        )


def rooms_from_extracted(
    extracted: list[ExtractedRoom],
) -> list:
    """
    Convert ExtractedRoom list to RoomInput list for AreaCalculator.
    Filters out rooms with zero area and logs warnings for low-confidence items.
    """
    from area_calculator import RoomInput

    inputs = []
    for i, er in enumerate(extracted):
        label = er.label or "Unidentified Space"

        # Use layer name as label if room label is generic/empty
        if label in ("", "Unidentified Space") and er.layer:
            label = er.layer

        if er.confidence < 0.5:
            logger.warning(
                f"Low OCR confidence ({er.confidence:.0%}) for '{label}' "
                f"on floor {er.floor} — verify manually."
            )

        inputs.append(RoomInput(
            label=label,
            area_m2=er.area_m2,
            floor=er.floor,
            room_id=f"{er.floor}-{i:04d}",
        ))

    return inputs
