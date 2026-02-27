"""
api.py
──────
Flask REST API for the Floor Plan Area Calculator.
Connects floor_plan_parser → room_rules → area_calculator → excel_exporter.

Endpoints:
  POST /api/analyse          Upload a floor plan file, get JSON area report
  POST /api/analyse/batch    Upload multiple floors at once
  GET  /api/download/<id>    Download the generated Excel schedule
  GET  /api/health           Health check
  GET  /api/rules            List all room classification rules

Run locally:
  python api.py

Environment variables:
  PORT            (default 5000)
  MAX_FILE_MB     (default 50)
  UPLOAD_FOLDER   (default ./uploads)
  OUTPUT_FOLDER   (default ./outputs)
"""

from __future__ import annotations

import os
import uuid
import logging
import tempfile
import traceback
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, send_file, abort

# ── Project modules ───────────────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(__file__))

from room_rules import ROOM_RULES, BuildingType
from area_calculator import AreaCalculator, RoomInput
from floor_plan_parser import parse_floor_plan, rooms_from_extracted
from excel_exporter import export_to_excel

# ── App setup ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

MAX_FILE_MB   = int(os.getenv("MAX_FILE_MB", 50))
UPLOAD_FOLDER = Path(os.getenv("UPLOAD_FOLDER", "./uploads"))
OUTPUT_FOLDER = Path(os.getenv("OUTPUT_FOLDER", "./outputs"))

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".dxf", ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024


# ── Helpers ───────────────────────────────────────────────────────────────────

def _allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _err(message: str, status: int = 400) -> tuple:
    return jsonify({"success": False, "error": message}), status


def _parse_building_type(raw: str) -> BuildingType:
    try:
        return BuildingType(raw.lower().strip())
    except ValueError:
        valid = [b.value for b in BuildingType]
        raise ValueError(f"Invalid building_type '{raw}'. Must be one of: {valid}")


def _save_upload(file, suffix: str) -> Path:
    """Save an uploaded file to the uploads folder with a UUID name."""
    dest = UPLOAD_FOLDER / f"{uuid.uuid4()}{suffix}"
    file.save(str(dest))
    return dest


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Simple health check."""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": "1.0.0",
        "supported_formats": list(ALLOWED_EXTENSIONS),
        "supported_building_types": [b.value for b in BuildingType],
    })


@app.get("/api/rules")
def list_rules():
    """Return the full room classification rule table."""
    rules_out = []
    for rule in ROOM_RULES:
        rules_out.append({
            "label":             rule.label,
            "keywords":          rule.keywords,
            "gfa_rule":          rule.gfa_rule.value,
            "gfa_multiplier":    rule.gfa_multiplier,
            "gfa_note":          rule.gfa_note,
            "nofa_rule":         rule.nofa_rule.value,
            "nofa_multiplier":   rule.nofa_multiplier,
            "is_concession":     rule.is_concession,
            "concession_item":   rule.concession_item,
            "subject_to_cap":    rule.subject_to_cap,
            "requires_beam_plus":rule.requires_beam_plus,
        })
    return jsonify({"success": True, "count": len(rules_out), "rules": rules_out})


@app.post("/api/analyse")
def analyse():
    """
    Analyse a single floor plan file.

    Multipart form fields:
      file           (required)  Floor plan file (.dxf / .pdf / .jpg / .png)
      building_type  (optional)  residential | non_domestic | composite | hotel
                                 Default: residential
      floor          (optional)  Floor label, e.g. "3/F". Default: "—"
      scale          (optional)  Drawing scale denominator, e.g. 100 for 1:100
                                 Default: 100 (auto-detected from title block)
      project_name   (optional)  Used in Excel export header
      export_excel   (optional)  "true" to generate Excel file. Default: false

    Returns JSON BuildingReport + optional download_id for Excel.
    """
    # ── Validate file ────────────────────────────────────────────────────────
    if "file" not in request.files:
        return _err("No file uploaded. Include a 'file' field in the multipart form.")

    file = request.files["file"]
    if not file.filename:
        return _err("Empty filename.")

    suffix = Path(file.filename).suffix.lower()
    if not _allowed(file.filename):
        return _err(
            f"Unsupported file type '{suffix}'. "
            f"Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # ── Parse form params ────────────────────────────────────────────────────
    raw_bt       = request.form.get("building_type", "residential")
    floor_label  = request.form.get("floor", "—")
    scale        = int(request.form.get("scale", 100))
    project_name = request.form.get("project_name", "Floor Plan Area Calculator")
    export_excel = request.form.get("export_excel", "false").lower() == "true"

    try:
        building_type = _parse_building_type(raw_bt)
    except ValueError as e:
        return _err(str(e))

    # ── Save & parse ─────────────────────────────────────────────────────────
    upload_path = _save_upload(file, suffix)
    logger.info(f"Saved upload: {upload_path.name}  floor={floor_label}  type={building_type.value}")

    try:
        extracted = parse_floor_plan(str(upload_path), floor=floor_label, scale=scale)
        room_inputs = rooms_from_extracted(extracted)

        if not room_inputs:
            return _err(
                "No rooms could be extracted from this file. "
                "Check that the file contains readable text labels or area annotations.",
                422,
            )

        calc   = AreaCalculator(building_type)
        report = calc.calculate(room_inputs)

    except ImportError as e:
        return _err(f"Missing dependency: {e}", 501)
    except Exception as e:
        logger.error(traceback.format_exc())
        return _err(f"Parsing failed: {e}", 500)
    finally:
        # Clean up upload
        try: upload_path.unlink()
        except Exception: pass

    # ── Optionally export Excel ──────────────────────────────────────────────
    download_id = None
    if export_excel:
        try:
            dl_id   = str(uuid.uuid4())
            xl_path = OUTPUT_FOLDER / f"{dl_id}.xlsx"
            export_to_excel(report, str(xl_path), project_name=project_name)
            download_id = dl_id
            logger.info(f"Excel saved: {xl_path.name}")
        except Exception as e:
            logger.warning(f"Excel export failed (report still returned): {e}")

    # ── Build response ───────────────────────────────────────────────────────
    result = report.to_dict()
    result["success"]      = True
    result["project_name"] = project_name
    result["floor"]        = floor_label
    result["rooms_parsed"] = len(room_inputs)
    if download_id:
        result["download_id"]  = download_id
        result["download_url"] = f"/api/download/{download_id}"

    return jsonify(result), 200


@app.post("/api/analyse/batch")
def analyse_batch():
    """
    Analyse multiple floor plan files in one request (full building).

    Multipart form fields:
      files[]        (required)  One file per floor
      floors[]       (optional)  Floor labels matching file order, e.g. "1/F,2/F,3/F"
      building_type  (optional)  Default: residential
      scale          (optional)  Default: 100
      project_name   (optional)
      export_excel   (optional)  "true" to generate combined Excel

    All floors are combined into a single BuildingReport with the
    APP-151 10% cap applied across the whole building.
    """
    files = request.files.getlist("files[]")
    if not files:
        return _err("No files uploaded. Use 'files[]' field for batch uploads.")

    raw_bt       = request.form.get("building_type", "residential")
    floors_raw   = request.form.get("floors[]", "")
    scale        = int(request.form.get("scale", 100))
    project_name = request.form.get("project_name", "Floor Plan Area Calculator")
    export_excel = request.form.get("export_excel", "false").lower() == "true"

    floor_labels = [f.strip() for f in floors_raw.split(",")] if floors_raw else []

    try:
        building_type = _parse_building_type(raw_bt)
    except ValueError as e:
        return _err(str(e))

    # ── Parse each floor ─────────────────────────────────────────────────────
    all_inputs: list[RoomInput] = []
    parse_errors: list[str]     = []

    for i, file in enumerate(files):
        suffix = Path(file.filename).suffix.lower()
        if not _allowed(file.filename):
            parse_errors.append(f"File {i+1} '{file.filename}': unsupported format.")
            continue

        floor_label = floor_labels[i] if i < len(floor_labels) else f"Floor {i+1}"
        upload_path = _save_upload(file, suffix)

        try:
            extracted   = parse_floor_plan(str(upload_path), floor=floor_label, scale=scale)
            room_inputs = rooms_from_extracted(extracted)
            all_inputs.extend(room_inputs)
            logger.info(f"Parsed floor '{floor_label}': {len(room_inputs)} rooms.")
        except Exception as e:
            parse_errors.append(f"File {i+1} '{file.filename}': {e}")
            logger.warning(f"Error parsing '{file.filename}': {e}")
        finally:
            try: upload_path.unlink()
            except Exception: pass

    if not all_inputs:
        return _err(
            "No rooms could be extracted from any of the uploaded files. "
            + (" Errors: " + "; ".join(parse_errors) if parse_errors else ""),
            422,
        )

    # ── Calculate across full building ────────────────────────────────────────
    try:
        calc   = AreaCalculator(building_type)
        report = calc.calculate(all_inputs)
    except Exception as e:
        logger.error(traceback.format_exc())
        return _err(f"Calculation failed: {e}", 500)

    # ── Excel export ─────────────────────────────────────────────────────────
    download_id = None
    if export_excel:
        try:
            dl_id   = str(uuid.uuid4())
            xl_path = OUTPUT_FOLDER / f"{dl_id}.xlsx"
            export_to_excel(report, str(xl_path), project_name=project_name)
            download_id = dl_id
        except Exception as e:
            logger.warning(f"Excel export failed: {e}")

    result = report.to_dict()
    result["success"]       = True
    result["project_name"]  = project_name
    result["floors_parsed"] = len(files) - len(parse_errors)
    result["rooms_parsed"]  = len(all_inputs)
    result["parse_errors"]  = parse_errors
    if download_id:
        result["download_id"]  = download_id
        result["download_url"] = f"/api/download/{download_id}"

    return jsonify(result), 200


@app.get("/api/download/<download_id>")
def download(download_id: str):
    """
    Download a previously generated Excel schedule.
    Files are kept for the lifetime of the server process.
    """
    # Sanitise ID — must be a UUID
    try:
        uuid.UUID(download_id)
    except ValueError:
        abort(400)

    xl_path = OUTPUT_FOLDER / f"{download_id}.xlsx"
    if not xl_path.exists():
        abort(404)

    return send_file(
        str(xl_path),
        as_attachment=True,
        download_name="area_schedule.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/classify")
def classify_rooms():
    """
    Classify a list of rooms provided directly as JSON (no file upload).
    Useful for testing the rules engine or for pre-parsed data.

    JSON body:
    {
      "building_type": "residential",
      "project_name":  "Tower A",
      "export_excel":  false,
      "rooms": [
        {"label": "Master Bedroom", "area_m2": 14.2, "floor": "3/F"},
        {"label": "Balcony",        "area_m2": 4.5,  "floor": "3/F"}
      ]
    }
    """
    body = request.get_json(silent=True)
    if not body or "rooms" not in body:
        return _err("JSON body with 'rooms' array is required.")

    raw_bt       = body.get("building_type", "residential")
    project_name = body.get("project_name",  "Floor Plan Area Calculator")
    export_excel = body.get("export_excel",  False)

    try:
        building_type = _parse_building_type(raw_bt)
    except ValueError as e:
        return _err(str(e))

    try:
        room_inputs = [
            RoomInput(
                label   = r.get("label", "Unknown"),
                area_m2 = float(r.get("area_m2", 0)),
                floor   = r.get("floor", "—"),
                room_id = r.get("id", ""),
            )
            for r in body["rooms"]
        ]
    except (TypeError, KeyError) as e:
        return _err(f"Invalid room data: {e}")

    if not room_inputs:
        return _err("Rooms array is empty.")

    try:
        calc   = AreaCalculator(building_type)
        report = calc.calculate(room_inputs)
    except Exception as e:
        return _err(f"Calculation failed: {e}", 500)

    download_id = None
    if export_excel:
        try:
            dl_id   = str(uuid.uuid4())
            xl_path = OUTPUT_FOLDER / f"{dl_id}.xlsx"
            export_to_excel(report, str(xl_path), project_name=project_name)
            download_id = dl_id
        except Exception as e:
            logger.warning(f"Excel export failed: {e}")

    result = report.to_dict()
    result["success"]      = True
    result["project_name"] = project_name
    result["rooms_parsed"] = len(room_inputs)
    if download_id:
        result["download_id"]  = download_id
        result["download_url"] = f"/api/download/{download_id}"

    return jsonify(result), 200


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(e):
    return _err(f"File too large. Maximum size is {MAX_FILE_MB} MB.", 413)

@app.errorhandler(404)
def not_found(e):
    return _err("Endpoint not found.", 404)

@app.errorhandler(405)
def method_not_allowed(e):
    return _err("Method not allowed.", 405)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Floor Plan Area Calculator API on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
