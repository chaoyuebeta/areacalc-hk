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


def detect_scale_from_image(img) -> Optional[int]:
    """
    Attempt to detect drawing scale from a PIL image by:
    1. OCR to find scale text (e.g. "SCALE 1:100")
    2. Scale bar analysis via OpenCV line detection (if available)

    Returns scale denominator int or None.
    """
    # Method 1: OCR text detection
    try:
        import pytesseract
        text = pytesseract.image_to_string(img, config="--psm 6")
        s = _detect_scale([text])
        if s:
            return s
    except Exception:
        pass

    # Method 2: OpenCV scale bar detection (optional dependency)
    try:
        import cv2
        import numpy as np
        from PIL import Image as PILImage

        # Convert to grayscale numpy array
        gray = np.array(img.convert("L"))

        # Look for horizontal lines in the bottom portion (scale bar location)
        h, w = gray.shape
        roi   = gray[int(h * 0.75):, :]   # bottom 25%
        edges = cv2.Canny(roi, 50, 150)
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=50,
                                 minLineLength=w//20, maxLineGap=10)
        if lines is not None:
            # Find longest horizontal line
            best_len = 0
            for line in lines:
                x1, y1, x2, y2 = line[0]
                if abs(y2 - y1) < 5:   # horizontal
                    best_len = max(best_len, abs(x2 - x1))
            if best_len > 0:
                logger.debug(f"Scale bar candidate: {best_len}px wide")
                # Can't determine scale without knowing the bar's labelled length
                # Return None — caller will prompt user
    except Exception:
        pass

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

# Patterns that indicate leader / annotation text (NOT room labels)
_LEADER_RE = re.compile(
    r'\b(arch\.?\s*feature|for\s+[a-z]\.[a-z]\.[a-z]|arch\.|feature|'
    r'r\.w\.p|p\.d\.|r\.c\.|a\.c\.\s*outdoor|outdoor\s*unit|'
    r'管道|管線|建築裝飾|之建築裝飾|之空調|外牆|低層|升降機槽外|大廈|mansion)\b',
    re.IGNORECASE,
)
# Pure dimension / measurement line (numbers + separators only)
_PURE_DIM_RE = re.compile(r'^[\d\s.,:/\\×xX\-\+]+$')

# HK floor plan: label font is typically 2-5x larger than dim text
# We use char height as a proxy: room labels ≥ 5pt, dim text ≤ 4pt
_MIN_LABEL_HEIGHT_PT = 3.5


def _cluster_words_spatial(words: list[dict], page_w: float, page_h: float) -> list[dict]:
    """
    Group individual words into label clusters using spatial proximity.

    Two words merge into the same cluster if:
      - Same horizontal band: |Δy_centre| < max(h1,h2) * 1.5
      - Horizontally adjacent: gap < max(char_width) * 3
    OR
      - Vertically stacked (same x zone, different y): |Δx_centre| < cluster_width * 0.6
        and vertical gap < max(h1,h2) * 2  (multi-line labels like 睡房 / B.R.)

    Returns list of cluster dicts with keys: text, x0, y0, x1, y1, line_count.
    """
    if not words:
        return []

    # Pre-filter: skip title block words early
    candidates = [
        w for w in words
        if not _is_title_block_text(w["text"], w["x0"], w["top"],
                                    page_w, page_h)
        and (w["bottom"] - w["top"]) >= _MIN_LABEL_HEIGHT_PT
    ]

    # Sort top→bottom, left→right
    candidates.sort(key=lambda w: (round(w["top"] / 4) * 4, w["x0"]))

    merged   = [False] * len(candidates)
    clusters = []

    for i, wi in enumerate(candidates):
        if merged[i]:
            continue
        group = [wi]
        merged[i] = True
        hi = wi["bottom"] - wi["top"]
        wi_cx = (wi["x0"] + wi["x1"]) / 2

        for j, wj in enumerate(candidates):
            if merged[j] or i == j:
                continue
            hj   = wj["bottom"] - wj["top"]
            h_   = max(hi, hj)
            dy   = abs((wj["top"] + wj["bottom"]) / 2 - (wi["top"] + wi["bottom"]) / 2)
            dx   = wj["x0"] - wi["x1"]   # positive = wj is to the right of wi

            # Horizontal merge: same line, close gap
            same_line = dy < h_ * 0.6
            h_gap_ok  = -h_ < dx < h_ * 4
            is_dim_i  = bool(_PURE_DIM_RE.match(wi["text"]))
            is_dim_j  = bool(_PURE_DIM_RE.match(wj["text"]))

            # Vertical merge: stacked label (e.g. 睡房 above B.R.)
            wj_cx    = (wj["x0"] + wj["x1"]) / 2
            w_span   = max(wi["x1"] - wi["x0"], wj["x1"] - wj["x0"])
            same_col = abs(wj_cx - wi_cx) < max(w_span * 0.9, 10)
            v_gap_ok = 0 <= (wj["top"] - wi["bottom"]) < h_ * 3.0

            h_merge = same_line and h_gap_ok and not (is_dim_i or is_dim_j)
            v_merge = same_col and v_gap_ok and not same_line and not is_dim_i and not is_dim_j

            if h_merge or v_merge:
                group.append(wj)
                merged[j] = True

        # Sort group: top→bottom, left→right; join lines with space or newline
        group.sort(key=lambda w: (round(w["top"] / 3) * 3, w["x0"]))

        # Reconstruct: words on same y-band → space; different bands → " / "
        lines_out: list[list[dict]] = []
        cur_line: list[dict] = [group[0]]
        for w in group[1:]:
            prev = cur_line[-1]
            if abs(w["top"] - prev["top"]) < max(prev["bottom"]-prev["top"],
                                                  w["bottom"]-w["top"]) * 1.2:
                cur_line.append(w)
            else:
                lines_out.append(cur_line)
                cur_line = [w]
        lines_out.append(cur_line)

        text_parts = [" ".join(w["text"] for w in ln) for ln in lines_out]
        text = " / ".join(text_parts) if len(text_parts) > 1 else text_parts[0]

        clusters.append({
            "text":       text,
            "x0":         min(w["x0"]     for w in group),
            "y0":         min(w["top"]    for w in group),
            "x1":         max(w["x1"]     for w in group),
            "y1":         max(w["bottom"] for w in group),
            "line_count": len(lines_out),
            "char_h":     max(w["bottom"] - w["top"] for w in group),
        })

    return clusters


def _is_noise_label(raw: str) -> bool:
    """
    Return True if the text is clearly NOT a room label:
    - Pure dimension / number string
    - Leader annotation (ARCH. FEATURE FOR R.W.P. etc.)
    - Single letter or symbol
    - Floor/level indicator (e.g. "3/F", "G/F")
    - Scale / north arrow labels
    """
    s = raw.strip()
    if not s or len(s) < 2:
        return True
    if _PURE_DIM_RE.match(s):
        return True
    if _LEADER_RE.search(s):
        return True
    # Single CJK character alone (not a room name)
    if len(s) == 1:
        return True
    # Floor indicator like "1/F", "G/F", "B1/F"
    if re.fullmatch(r'[BbGg]?\d{0,2}/[Ff]', s):
        return True
    # North arrow / scale bar label
    if re.fullmatch(r'N\.?|NORTH|TRUE\s*NORTH', s, re.IGNORECASE):
        return True
    return False


def _extract_room_rects(page) -> list[dict]:
    """
    Extract closed rectangular / polygonal regions from PDF page geometry.
    These represent room boundaries drawn by the architect.

    Returns list of dicts: {area_pts2, cx, cy, x0, y0, x1, y1}
    Uses pdfplumber rects + curves (polylines that form closed shapes).

    Minimum meaningful room size: 0.5 m² at 1:100 → ~5000 pt²  (≈70×70pt)
    """
    MIN_AREA_PT2 = 1000   # ~30×30 pt — smaller than this is a line artifact

    rects_out = []

    # Method 1: explicit rect objects
    for r in getattr(page, "rects", []) or []:
        try:
            w = r["width"]; h = r["height"]
            if w < 5 or h < 5:
                continue
            area = w * h
            if area < MIN_AREA_PT2:
                continue
            rects_out.append({
                "area_pts2": area,
                "cx": r["x0"] + w / 2,
                "cy": r["y0"] + h / 2,
                "x0": r["x0"], "y0": r["y0"],
                "x1": r["x1"], "y1": r["y1"],
            })
        except Exception:
            continue

    # Method 2: extract from lines — find axis-aligned closed rectangles
    # Group horizontal and vertical lines, find intersecting pairs
    h_lines = []
    v_lines = []
    for ln in getattr(page, "lines", []) or []:
        try:
            x0, y0, x1, y1 = ln["x0"], ln["y0"], ln["x1"], ln["y1"]
            if abs(y1 - y0) < 2 and abs(x1 - x0) > 20:   # horizontal
                h_lines.append((min(x0,x1), max(x0,x1), (y0+y1)/2))
            elif abs(x1 - x0) < 2 and abs(y1 - y0) > 20: # vertical
                v_lines.append(((x0+x1)/2, min(y0,y1), max(y0,y1)))
        except Exception:
            continue

    # For each pair of h-lines and v-lines forming a rectangle
    TOL = 4   # pt tolerance for line intersections
    for i, (hx0_a, hx1_a, hy_a) in enumerate(h_lines):
        for hx0_b, hx1_b, hy_b in h_lines[i+1:]:
            if abs(hy_a - hy_b) < 10:
                continue
            y_top = min(hy_a, hy_b); y_bot = max(hy_a, hy_b)
            x_overlap_min = max(hx0_a, hx0_b)
            x_overlap_max = min(hx1_a, hx1_b)
            if x_overlap_max - x_overlap_min < 20:
                continue
            # Find vertical lines closing this rectangle
            for vx, vy0, vy1 in v_lines:
                if not (x_overlap_min - TOL <= vx <= x_overlap_max + TOL):
                    continue
                if vy0 <= y_top + TOL and vy1 >= y_bot - TOL:
                    w = x_overlap_max - x_overlap_min
                    h = y_bot - y_top
                    area = w * h
                    if area >= MIN_AREA_PT2:
                        rects_out.append({
                            "area_pts2": area,
                            "cx": (x_overlap_min + x_overlap_max) / 2,
                            "cy": (y_top + y_bot) / 2,
                            "x0": x_overlap_min, "y0": y_top,
                            "x1": x_overlap_max, "y1": y_bot,
                        })
                    break

    # Deduplicate overlapping rects (keep larger)
    rects_out.sort(key=lambda r: -r["area_pts2"])
    deduped = []
    for r in rects_out:
        overlap = False
        for d in deduped:
            ix0 = max(r["x0"], d["x0"]); iy0 = max(r["y0"], d["y0"])
            ix1 = min(r["x1"], d["x1"]); iy1 = min(r["y1"], d["y1"])
            if ix1 > ix0 and iy1 > iy0:
                inter = (ix1-ix0)*(iy1-iy0)
                if inter / r["area_pts2"] > 0.7:
                    overlap = True
                    break
        if not overlap:
            deduped.append(r)

    return deduped


def _match_labels_to_rects(
    clusters: list[dict],
    rects:    list[dict],
    page_w:   float,
    page_h:   float,
) -> list[tuple[dict, Optional[dict]]]:
    """
    For each label cluster, find the best matching room rect.

    Matching priority:
      1. Label centre is INSIDE the rect
      2. Label centre is closest to rect centre (within 3× rect diagonal)

    Returns list of (cluster, rect_or_None).
    Each rect can be matched to at most one label.
    """
    used_rects: set[int] = set()
    matched: list[tuple[dict, Optional[dict]]] = []

    for cl in clusters:
        cx = (cl["x0"] + cl["x1"]) / 2
        cy = (cl["y0"] + cl["y1"]) / 2

        # Priority 1: label centre inside rect
        inside = [
            (i, r) for i, r in enumerate(rects)
            if i not in used_rects
            and r["x0"] <= cx <= r["x1"]
            and r["y0"] <= cy <= r["y1"]
        ]
        if inside:
            # Pick smallest rect that contains the label (most specific)
            best_i, best_r = min(inside, key=lambda ir: ir[1]["area_pts2"])
            used_rects.add(best_i)
            matched.append((cl, best_r))
            continue

        # Priority 2: nearest rect centre within reasonable distance
        if rects:
            def _score(ir):
                i, r = ir
                if i in used_rects:
                    return float("inf")
                diag = math.hypot(r["x1"]-r["x0"], r["y1"]-r["y0"])
                dist = math.hypot(cx - r["cx"], cy - r["cy"])
                return dist / max(diag, 1)

            best_i, best_r = min(enumerate(rects), key=_score)
            score = _score((best_i, best_r))
            if score < 2.0:   # within 2× diagonal distance
                used_rects.add(best_i)
                matched.append((cl, best_r))
                continue

        matched.append((cl, None))

    return matched


def _parse_pdf_vector(filepath: str, floor: str, scale: int) -> list[ExtractedRoom]:
    """
    Extract rooms from a vector PDF using pdfplumber.

    Pipeline:
      1. Extract all words → spatial cluster into label groups
         (fixes CJK single-char splitting, merges multi-line labels)
      2. Noise filter: remove dimensions, leader annotations, titles
      3. Extract room geometry (rects / closed polylines) for accurate area
      4. Match labels → rects via spatial containment
      5. Calibration: dimension annotations → mm/pt → area_m2
         (fallback: title block scale text, then user-supplied scale)
    """
    import pdfplumber

    rooms: list[ExtractedRoom] = []

    with pdfplumber.open(filepath) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_w = float(page.width)
            page_h = float(page.height)

            # ── Step 1: Extract words ────────────────────────────────────────
            words = page.extract_words(
                x_tolerance=2, y_tolerance=2,
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=["size"],   # get font size for filtering
            )

            # ── Step 2: Calibration ──────────────────────────────────────────
            # Build single-line blocks for dimension detection
            line_map: dict[float, list[dict]] = {}
            for w in words:
                line_map.setdefault(round(w["top"], 1), []).append(w)
            raw_blocks = []
            for y_key, lw in sorted(line_map.items()):
                lw.sort(key=lambda w: w["x0"])
                raw_blocks.append({
                    "text": " ".join(w["text"] for w in lw),
                    "x0": min(w["x0"] for w in lw),
                    "y0": min(w["top"] for w in lw),
                    "x1": max(w["x1"] for w in lw),
                    "y1": max(w["bottom"] for w in lw),
                })

            mm_per_pt = _infer_mm_per_pt_from_dimensions(raw_blocks, page_w, page_h)
            calib_src = "dimension_annotations"
            if mm_per_pt is None:
                full_text = " ".join(b["text"] for b in raw_blocks)
                ds = _detect_scale([full_text])
                if ds:
                    scale    = ds
                    calib_src = "title_block_text"
                else:
                    calib_src = "user_input"

            logger.info(
                f"Page {page_num+1}: calib={calib_src} "
                + (f"mm/pt={mm_per_pt:.4f}" if mm_per_pt else f"scale=1:{scale}")
            )

            # ── Step 3: Spatial clustering ───────────────────────────────────
            clusters = _cluster_words_spatial(words, page_w, page_h)

            # ── Step 4: Noise filtering ──────────────────────────────────────
            clean_clusters = []
            skipped_noise = 0
            for cl in clusters:
                raw = cl["text"].strip()
                if _is_noise_label(raw):
                    skipped_noise += 1
                    continue
                label = _clean_label(raw)
                if not label or len(label) < 2:
                    skipped_noise += 1
                    continue
                cl["label"] = label
                clean_clusters.append(cl)

            logger.info(
                f"Page {page_num+1}: {len(clusters)} clusters → "
                f"{len(clean_clusters)} after noise filter "
                f"({skipped_noise} removed)"
            )

            # ── Step 5: Extract room geometry ────────────────────────────────
            rects = _extract_room_rects(page)
            logger.info(f"Page {page_num+1}: {len(rects)} room rects found")

            # ── Step 6: Match labels → rects ─────────────────────────────────
            matched = _match_labels_to_rects(clean_clusters, rects, page_w, page_h)

            # ── Step 7: Build ExtractedRoom list ─────────────────────────────
            for cl, rect in matched:
                label = cl["label"]

                # Area: prefer explicit annotation, then rect geometry, then 0
                explicit_area = _extract_area_from_text(cl["text"])

                if explicit_area:
                    area_m2 = explicit_area
                    note    = ""
                elif rect is not None:
                    if mm_per_pt is not None:
                        area_m2 = _pdf_area_from_mm_per_pt(rect["area_pts2"], mm_per_pt)
                        note    = f"Area from room geometry · calibration: {mm_per_pt:.3f} mm/pt"
                    else:
                        area_m2 = _pdf_pts_to_m2(rect["area_pts2"], scale)
                        note    = f"Area from room geometry · scale 1:{scale}"
                else:
                    area_m2 = 0.0
                    note    = "⚠️ No room boundary found — enter area manually."

                rooms.append(ExtractedRoom(
                    label=label,
                    area_m2=round(area_m2, 4),
                    floor=floor,
                    bbox=(cl["x0"], cl["y0"], cl["x1"], cl["y1"]),
                    source="pdf_vector",
                    notes=note,
                ))

            if skipped_noise:
                logger.info(
                    f"Page {page_num+1}: removed {skipped_noise} noise labels."
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


def _parse_pdf_ocr(
    filepath: str, floor: str, scale: int,
    paper_size: str = "A1", paper_width_mm: float = 0, paper_height_mm: float = 0,
) -> list[ExtractedRoom]:
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


def _parse_image(
    filepath: str, floor: str, scale: int,
    paper_size: str = "A1", paper_width_mm: float = 0, paper_height_mm: float = 0,
) -> list[ExtractedRoom]:
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
    filepath:        str,
    floor:           str   = "—",
    scale:           int   = 100,
    force_ocr:       bool  = False,
    paper_size:      str   = "A1",
    paper_width_mm:  float = 0,
    paper_height_mm: float = 0,
) -> list[ExtractedRoom]:
    """
    Parse a floor plan file and return a list of ExtractedRoom objects.

    Args:
        filepath:        Path to DWG, DXF, PDF, JPG, or PNG file.
        floor:           Floor label, e.g. "3/F".
        scale:           Fallback drawing scale denominator (default 100).
                         Ignored for DWG/DXF (real-world coordinates used).
        force_ocr:       Force OCR even for vector PDFs.
        paper_size:      Paper size for raster images / scanned PDFs
                         ("A0","A1","A2","A3","A4","A1+","custom").
                         Used to estimate DPI. Ignored for vector PDFs/DWG.
        paper_width_mm:  Used when paper_size="custom".
        paper_height_mm: Used when paper_size="custom".

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

    logger.info(f"Parsing {ext} | floor={floor} | scale=1:{scale} | paper={paper_size}")

    if ext in (".dwg", ".dxf"):
        if ext == ".dwg":
            raise ValueError(
                "DWG files must be converted to DXF before parsing.\n"
                "Recommended tools: ODA File Converter (free) or LibreCAD.\n"
                "Note: Scale is NOT needed for DXF — real coordinates are used."
            )
        return _parse_dwg_dxf(filepath, floor, scale)

    elif ext == ".pdf":
        if force_ocr or _is_scanned_pdf(filepath):
            logger.info("PDF detected as scanned — using OCR.")
            return _parse_pdf_ocr(filepath, floor, scale,
                                  paper_size, paper_width_mm, paper_height_mm)
        else:
            logger.info("PDF detected as vector — using pdfplumber.")
            return _parse_pdf_vector(filepath, floor, scale)

    elif ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"):
        return _parse_image(filepath, floor, scale,
                            paper_size, paper_width_mm, paper_height_mm)

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
