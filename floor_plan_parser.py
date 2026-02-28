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


def _pdf_area_from_mm_per_pt(area_pts2: float, mm_per_pt: float) -> float:
    """Convert PDF area (pts²) to m² using a direct mm/pt calibration factor."""
    return area_pts2 * (mm_per_pt / 1000.0) ** 2


# ─── Dimension annotation calibration ────────────────────────────────────────
# Finds numeric dimension annotations in vector PDFs (e.g. "2850", "3100") and
# uses the ratio of annotated value to measured point-span to derive a direct
# mm-per-pt calibration factor — no paper size or DPI needed.

_DIM_NUM_RE  = re.compile(r'^(\d{3,5})$')     # standalone 3-5 digit integer
_DIM_MIN_MM  = 200
_DIM_MAX_MM  = 50_000


def _infer_mm_per_pt_from_dimensions(
    blocks: list[dict],
    page_width: float,
    page_height: float,
) -> Optional[float]:
    """
    Infer a mm-per-point calibration factor from dimension annotations in a
    vector PDF page (blocks = list of {text, x0, y0, x1, y1}).

    Looks for horizontally-aligned pairs of dimension numbers.
    The distance between their text centres vs their summed mm values gives
    mm/pt.  Returns median of all valid estimates, or None if < 2 found.
    """
    candidates = []
    for b in blocks:
        raw = b["text"].strip()
        if _is_title_block_text(raw, b["x0"], b["y0"], page_width, page_height):
            continue
        m = _DIM_NUM_RE.match(raw)
        if not m:
            continue
        val = int(m.group(1))
        if not (_DIM_MIN_MM <= val <= _DIM_MAX_MM):
            continue
        candidates.append({
            "val": val,
            "cx": (b["x0"] + b["x1"]) / 2,
            "cy": (b["y0"] + b["y1"]) / 2,
        })

    if len(candidates) < 2:
        return None

    # Group into horizontal bands (5pt tolerance) and try consecutive pairs
    by_row: dict[int, list[dict]] = {}
    for c in candidates:
        key = int(round(c["cy"] / 5)) * 5
        by_row.setdefault(key, []).append(c)

    estimates: list[float] = []
    for row in by_row.values():
        if len(row) < 2:
            continue
        row.sort(key=lambda r: r["cx"])
        for i in range(len(row) - 1):
            a, b = row[i], row[i + 1]
            span_pt = abs(b["cx"] - a["cx"])
            if span_pt < 5:
                continue
            for val in (a["val"], b["val"], a["val"] + b["val"]):
                ratio = val / span_pt
                # Sanity: covers 1:20 (7 mm/pt) to 1:500 (176 mm/pt) at 72dpi
                if 5.0 <= ratio <= 200.0:
                    estimates.append(ratio)

    if not estimates:
        return None

    estimates.sort()
    median = estimates[len(estimates) // 2]
    implied_scale = int(median / 0.3528)
    logger.info(
        f"Dimension calibration: {len(estimates)} estimates, "
        f"median {median:.4f} mm/pt → implied scale ≈ 1:{implied_scale}"
    )
    return median


def _mm_per_pt_to_scale(mm_per_pt: float) -> int:
    """Round mm/pt calibration to nearest standard scale integer."""
    raw = mm_per_pt / 0.3528
    for std in [20, 50, 75, 100, 150, 200, 250, 500, 1000]:
        if raw <= std * 1.25:
            return std
    return 1000


# ─── Area keyword extractor ───────────────────────────────────────────────────

# Regex to pull explicit area annotations from text (e.g. "14.2 m²" or "14.20m2")
_AREA_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:m²|m2|sq\.?\s*m|sqm)',
    re.IGNORECASE
)

def _extract_area_from_text(text: str) -> Optional[float]:
    m = _AREA_RE.search(text)
    return float(m.group(1)) if m else None


# ─── Title block filtering ────────────────────────────────────────────────────
# HK BD drawings almost always have the title block in the bottom-right corner,
# occupying roughly the rightmost 25% × bottom 20% of the page.
# We exclude that zone AND filter by known title block keywords.

_TITLE_BLOCK_RIGHT  = 0.75   # exclude text with x0 > 75% of page width
_TITLE_BLOCK_BOTTOM = 0.80   # exclude text with y0 > 80% of page height

# Keywords that almost never appear inside floor plan rooms
_TITLE_BLOCK_KEYWORDS = re.compile(
    r'\b('
    # BD / project admin (English)
    r'bim\s*ref|bd\s*ref|bd.s\s*official|fsd\s*ref'
    r'|source\s*drawing|rev(?:ision)?\.?\s*no|drawing\s*no|dwg\.?\s*no'
    r'|drawing\s*title|project\s*title|project\s*name'
    r'|checked\s*by|drawn\s*by|approved\s*by|authorised\s*by'
    r'|signature|date\s*of\s*issue'
    # Scale / north arrow labels
    r'|north\s*arrow|true\s*north|magnetic\s*north'
    # Standard notes / legend headers
    r'|general\s*notes?|legend|abbreviations?|symbols?'
    r'|all\s*dimensions?\s*in|dimensions?\s*in\s*mm'
    r'|do\s*not\s*scale|not\s*to\s*scale|nts'
    # Revision table
    r'|rev\.\s*description|amendment'
    # Certification / disclaimers
    r'|copyright|all\s*rights\s*reserved|confidential'
    # Common BD form labels
    r'|xxx|a00[0-9]|c00[0-9]'
    r')\b'
    # Chinese title block keywords (no word boundaries needed for CJK)
    r'|圖紙編號|圖號|圖名|圖則編號'
    r'|項目名稱|工程名稱|項目編號'
    r'|審核|審批|核准|批准|簽署|簽名'
    r'|繪圖|製圖|校對|核對'
    r'|發出日期|修訂日期|日期'
    r'|修訂編號|修訂記錄|版本'
    r'|比例尺|圖紙比例'          # "scale" label in title block
    r'|備註|一般備註|圖例|縮寫'
    r'|版權|保密|機密'
    r'|所有尺寸以毫米計|尺寸單位',
    re.IGNORECASE,
)

# Short all-caps strings typical of title block codes (e.g. "REV", "NTS", "BD")
_TITLE_BLOCK_CODE = re.compile(r'^[A-Z0-9/\-]{1,6}$')


def _is_title_block_text(
    text: str,
    x0: float, y0: float,
    page_width: float, page_height: float,
) -> bool:
    """
    Return True if this text block is likely part of the title block / border
    and should be excluded from room label extraction.

    Rules (any one triggers exclusion):
      1. Located in the bottom-right corner zone (right 25% × bottom 20%).
      2. Located in the bottom strip only (bottom 10% of page) — stamps, page nos.
      3. Contains known title block keywords.
      4. Is a short all-caps code with no alphabetic room-name meaning
         AND is positioned in the right 30% of the page.
    """
    # Rule 1 — bottom-right corner
    if (page_width  > 0 and x0 / page_width  > _TITLE_BLOCK_RIGHT and
            page_height > 0 and y0 / page_height > _TITLE_BLOCK_BOTTOM):
        return True

    # Rule 2 — bottom strip (page numbers, stamps, revision dates)
    if page_height > 0 and y0 / page_height > 0.92:
        return True

    # Rule 3 — keyword match
    if _TITLE_BLOCK_KEYWORDS.search(text):
        return True

    # Rule 4 — short all-caps code in right margin
    stripped = text.strip()
    if (_TITLE_BLOCK_CODE.match(stripped) and
            page_width > 0 and x0 / page_width > 0.70):
        return True

    return False


# ─── Room label cleaner ───────────────────────────────────────────────────────

_NOISE = re.compile(
    r'(\d+(?:\.\d+)?\s*(?:m²|m2|sqm|sq\.m)?'   # area annotations
    r'|\bscale\b|\bratio\b|\bfl\b|\bfloor\b'    # common noise words
    r'|\d{1,2}/f\b)',                            # floor tags like 3/F
    re.IGNORECASE
)

# Characters to strip from start/end of labels — explicitly excludes CJK range
_STRIP_CHARS = " \t\n\r.,;:-_/\\"


def _clean_label(raw: str) -> str:
    label = _NOISE.sub("", raw).strip(_STRIP_CHARS)
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
    Title block text is excluded.
    Scale calibration priority:
      1. Dimension annotations (mm values) → mm/pt direct calibration
      2. Title block text "SCALE 1:100" → scale integer
      3. User-supplied scale fallback
    """
    import pdfplumber

    rooms: list[ExtractedRoom] = []

    with pdfplumber.open(filepath) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_w = float(page.width)
            page_h = float(page.height)

            words = page.extract_words(
                x_tolerance=3, y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            full_text = " ".join(w["text"] for w in words)

            # Build text blocks
            lines: dict[float, list[dict]] = {}
            for w in words:
                key = round(w["top"], 1)
                lines.setdefault(key, []).append(w)

            blocks: list[dict] = []
            for y, line_words in sorted(lines.items()):
                line_words.sort(key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in line_words)
                x0   = min(w["x0"]     for w in line_words)
                y0   = min(w["top"]    for w in line_words)
                x1   = max(w["x1"]     for w in line_words)
                y1   = max(w["bottom"] for w in line_words)
                blocks.append({"text": text, "x0": x0, "y0": y0,
                               "x1": x1, "y1": y1})

            # ── Calibration priority ─────────────────────────────────────────
            # 1. Try dimension annotations first (most accurate)
            mm_per_pt = _infer_mm_per_pt_from_dimensions(blocks, page_w, page_h)
            calibration_source = "dimension_annotations"

            if mm_per_pt is None:
                # 2. Fall back to title block scale text
                detected_scale = _detect_scale([full_text])
                if detected_scale:
                    scale = detected_scale
                    calibration_source = "title_block_text"
                else:
                    calibration_source = "user_input"
                mm_per_pt = None   # will use _pdf_pts_to_m2(scale) below

            logger.info(
                f"Page {page_num+1}: calibration={calibration_source}, "
                + (f"mm/pt={mm_per_pt:.4f}" if mm_per_pt else f"scale=1:{scale}")
            )

            skipped_title_block = 0
            for block in blocks:
                raw = block["text"].strip()

                # ── Title block filter ───────────────────────────────────────
                if _is_title_block_text(raw, block["x0"], block["y0"],
                                        page_w, page_h):
                    skipped_title_block += 1
                    continue

                label = _clean_label(raw)
                if not label or len(label) < 2:
                    continue
                if re.fullmatch(r'[\d\s.,:/\\-]+', label):
                    continue

                explicit_area  = _extract_area_from_text(raw)
                bbox_area_pts2 = (block["x1"]-block["x0"]) * (block["y1"]-block["y0"])

                if explicit_area:
                    area_m2 = explicit_area
                    note    = ""
                elif mm_per_pt is not None:
                    area_m2 = _pdf_area_from_mm_per_pt(bbox_area_pts2, mm_per_pt)
                    note    = f"⚠️ Area estimated from label bbox via dimension calibration ({mm_per_pt:.3f} mm/pt). Verify manually."
                else:
                    area_m2 = _pdf_pts_to_m2(bbox_area_pts2, scale)
                    note    = "⚠️ Area estimated from label bbox — verify manually."

                rooms.append(ExtractedRoom(
                    label=label,
                    area_m2=round(area_m2, 4),
                    floor=floor,
                    bbox=(block["x0"], block["y0"], block["x1"], block["y1"]),
                    source="pdf_vector",
                    notes=note,
                ))

            if skipped_title_block:
                logger.info(
                    f"Page {page_num+1}: skipped {skipped_title_block} "
                    f"title-block text blocks."
                )

    return rooms


# ─── OCR parser (scanned PDF or image) ───────────────────────────────────────

def _ocr_image(img) -> list[dict]:
    """
    Run pytesseract on a PIL image and return word-level data.

    Language priority:
      chi_tra (Traditional Chinese) + chi_sim (Simplified Chinese) + eng
    Falls back to eng-only if Chinese language data is not installed.

    To install Chinese support on Ubuntu / Render:
      apt-get install -y tesseract-ocr-chi-tra tesseract-ocr-chi-sim
    """
    import pytesseract

    # Determine available languages
    try:
        available = pytesseract.get_languages()
    except Exception:
        available = ["eng"]

    if "chi_tra" in available and "chi_sim" in available:
        lang = "chi_tra+chi_sim+eng"
    elif "chi_tra" in available:
        lang = "chi_tra+eng"
    elif "chi_sim" in available:
        lang = "chi_sim+eng"
    else:
        lang = "eng"

    logger.info(f"OCR language: {lang}")

    data = pytesseract.image_to_data(
        img,
        lang=lang,
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


def _words_to_rooms(
    words: list[dict], floor: str,
    scale: int, source: str,
    dpi: float = 150.0,
    img_width: int = 0,
    img_height: int = 0,
) -> list[ExtractedRoom]:
    """Convert OCR word list to ExtractedRoom list, filtering title block text."""
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

    skipped = 0
    for p in phrases:
        raw = p["text"].strip()

        # ── Title block filter ───────────────────────────────────────────────
        if _is_title_block_text(raw, p["x0"], p["y0"],
                                 float(img_width), float(img_height)):
            skipped += 1
            continue

        label = _clean_label(raw)
        if not label or len(label) < 2:
            continue
        if re.fullmatch(r'[\d\s.,:/\\-]+', label):
            continue

        explicit_area = _extract_area_from_text(raw)
        if not explicit_area:
            bbox_px2 = (p["x1"] - p["x0"]) * (p["y1"] - p["y0"])
            est_area = _px_to_m2(bbox_px2, scale, dpi) if dpi > 0 else 0.0
        else:
            est_area = None

        rooms.append(ExtractedRoom(
            label=label,
            area_m2=explicit_area or 0.0,
            floor=floor,
            bbox=(p["x0"], p["y0"], p["x1"], p["y1"]),
            source=source,
            confidence=p["conf"],
            notes="" if explicit_area else
                  f"⚠️ No area annotation — bbox estimate: ~{est_area:.2f} m² "
                  f"(scale 1:{scale}, DPI {dpi:.0f}). Manual verification required.",
        ))

    if skipped:
        logger.info(f"OCR: skipped {skipped} title-block text blocks.")
    return rooms


def _parse_pdf_ocr(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """Extract rooms from a scanned PDF via OCR, filtering title block."""
    from pdf2image import convert_from_path
    images = convert_from_path(filepath, dpi=150)
    rooms  = []
    for img in images:
        words = _ocr_image(img)
        rooms.extend(_words_to_rooms(
            words, floor, scale, "pdf_ocr",
            img_width=img.width, img_height=img.height,
        ))
    return rooms


def _parse_image(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """Extract rooms from a JPG/PNG image via OCR, filtering title block."""
    from PIL import Image
    img   = Image.open(filepath)
    words = _ocr_image(img)
    return _words_to_rooms(
        words, floor, scale, "image_ocr",
        img_width=img.width, img_height=img.height,
    )


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
