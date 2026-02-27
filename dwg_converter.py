"""
dwg_converter.py
────────────────
Converts DWG files to DXF format for use with the floor plan parser.

Conversion backends (tried in order of preference):
  1. ODA File Converter  — highest fidelity, free download from opendesign.com
  2. LibreOffice         — available on most servers, good compatibility
  3. ezdxf               — pure Python, limited DWG support (R2013 and older)

Usage:
    from dwg_converter import convert_dwg, batch_convert_dwg

    # Single file
    dxf_path = convert_dwg("floor_plan.dwg")
    # → returns "floor_plan.dxf" (same directory, or temp dir)

    # Batch
    results  = batch_convert_dwg(["A1.dwg", "A2.dwg", "A3.dwg"])
    # → {"A1.dwg": "A1.dxf", "A2.dwg": "A2.dxf", "A3.dwg": ConversionError(...)}

Run as CLI:
    python dwg_converter.py input.dwg [output.dxf]
    python dwg_converter.py --batch *.dwg --output-dir ./dxf_output
"""

from __future__ import annotations

import os
import re
import sys
import shutil
import logging
import platform
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ConversionResult:
    source_path:  str
    output_path:  Optional[str]   = None
    backend_used: str             = ""
    success:      bool            = False
    error:        str             = ""
    warnings:     list[str]       = field(default_factory=list)

    def __bool__(self):
        return self.success


class ConversionError(Exception):
    pass


# ─── Backend detection ────────────────────────────────────────────────────────

def _find_oda() -> Optional[str]:
    """Find ODA File Converter executable."""
    candidates = [
        "ODAFileConverter",
        "ODAFileConverter_title",
        "/usr/bin/ODAFileConverter",
        "/usr/local/bin/ODAFileConverter",
        "/opt/ODAFileConverter/ODAFileConverter",
        # macOS
        "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
        # Windows
        r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
        r"C:\Program Files (x86)\ODA\ODAFileConverter\ODAFileConverter.exe",
    ]
    # Also check env var
    env_path = os.environ.get("ODA_FILE_CONVERTER")
    if env_path:
        candidates.insert(0, env_path)

    for c in candidates:
        if shutil.which(c) or Path(c).is_file():
            return c
    return None


def _find_libreoffice() -> Optional[str]:
    """Find LibreOffice or soffice executable."""
    candidates = ["libreoffice", "soffice", "LibreOffice"]
    env_path = os.environ.get("LIBREOFFICE_PATH")
    if env_path:
        candidates.insert(0, env_path)
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return None


def _has_ezdxf() -> bool:
    try:
        import ezdxf  # noqa
        return True
    except ImportError:
        return False


# ─── Backend implementations ──────────────────────────────────────────────────

def _convert_with_oda(
    dwg_path: Path,
    output_dir: Path,
    version: str = "ACAD2018",
) -> Path:
    """
    Convert DWG → DXF using ODA File Converter.

    ODA CLI signature:
      ODAFileConverter <input_folder> <output_folder> <input_format>
                       <output_format> [recurse] [audit]

    Supported output versions: ACAD9, ACAD10, ..., ACAD2018, ACAD2023
    """
    oda = _find_oda()
    if not oda:
        raise ConversionError(
            "ODA File Converter not found.\n"
            "Download free from: https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "Then set ODA_FILE_CONVERTER=/path/to/ODAFileConverter in your environment."
        )

    # ODA works on directories, not individual files
    input_dir = dwg_path.parent
    stem      = dwg_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        oda,
        str(input_dir),
        str(output_dir),
        "DWG",       # input format
        "DXF",       # output format
        "0",         # recurse (0 = no)
        "1",         # audit (1 = yes)
        f"*.dwg",
    ]

    logger.info(f"ODA convert: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    dxf_path = output_dir / (stem + ".dxf")
    if not dxf_path.exists():
        raise ConversionError(
            f"ODA conversion failed. Return code: {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    logger.info(f"ODA conversion successful: {dxf_path}")
    return dxf_path


def _convert_with_libreoffice(dwg_path: Path, output_dir: Path) -> Path:
    """
    Convert DWG → DXF using LibreOffice headless.
    LibreOffice can import DWG (via its Draw module) and export to DXF.
    Note: fidelity is lower than ODA for complex DWG files.
    """
    lo = _find_libreoffice()
    if not lo:
        raise ConversionError(
            "LibreOffice not found. Install with:\n"
            "  Ubuntu/Debian: sudo apt install libreoffice\n"
            "  macOS: brew install --cask libreoffice\n"
            "Or set LIBREOFFICE_PATH=/path/to/libreoffice in your environment."
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    # LibreOffice --convert-to produces DXF in the output dir
    cmd = [
        lo,
        "--headless",
        "--norestore",
        "--nofirststartwizard",
        "--convert-to", "dxf",
        "--outdir", str(output_dir),
        str(dwg_path),
    ]

    logger.info(f"LibreOffice convert: {' '.join(cmd)}")

    # Use a temporary HOME to avoid LibreOffice profile conflicts
    env = os.environ.copy()
    with tempfile.TemporaryDirectory() as tmpdir:
        env["HOME"] = tmpdir
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    dxf_path = output_dir / (dwg_path.stem + ".dxf")

    if result.returncode != 0 or not dxf_path.exists():
        raise ConversionError(
            f"LibreOffice conversion failed. Return code: {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    logger.info(f"LibreOffice conversion successful: {dxf_path}")
    return dxf_path


def _convert_with_ezdxf(dwg_path: Path, output_dir: Path) -> Path:
    """
    Attempt DWG read using ezdxf's limited DWG support.
    ezdxf can read some DWG files (R2013 and older) and re-save as DXF.
    This is a last resort — use ODA or LibreOffice when possible.
    """
    try:
        import ezdxf
    except ImportError:
        raise ConversionError(
            "ezdxf not installed. Run: pip install ezdxf\n"
            "Note: ezdxf's DWG support is limited — prefer ODA File Converter."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    dxf_path = output_dir / (dwg_path.stem + ".dxf")

    try:
        # ezdxf can sometimes read binary DWG files
        doc = ezdxf.readfile(str(dwg_path))
        doc.saveas(str(dxf_path))
        if not dxf_path.exists():
            raise ConversionError("ezdxf saved no output file.")
        logger.info(f"ezdxf conversion successful: {dxf_path}")
        return dxf_path
    except ezdxf.DXFError as e:
        raise ConversionError(f"ezdxf could not read DWG: {e}")


# ─── Validation ───────────────────────────────────────────────────────────────

def _validate_dxf(dxf_path: Path) -> list[str]:
    """
    Basic DXF validation — check it's a real DXF file and has content.
    Returns a list of warning strings (empty = OK).
    """
    warnings = []
    try:
        content = dxf_path.read_text(errors="replace")
        if not content.strip().startswith("  0\nSECTION"):
            warnings.append("DXF file may be malformed — unexpected header.")
        if len(content) < 500:
            warnings.append("DXF file is very small — may be empty or incomplete.")
        # Count entity types
        entity_count = len(re.findall(r"^  0\r?\n(?!SECTION|ENDSEC|EOF)", content, re.MULTILINE))
        if entity_count == 0:
            warnings.append("No drawing entities found in DXF — the conversion may have produced an empty file.")
        else:
            logger.info(f"DXF validation: {entity_count} entities found.")
    except Exception as e:
        warnings.append(f"Could not validate DXF: {e}")
    return warnings


# ─── Public API ───────────────────────────────────────────────────────────────

def convert_dwg(
    dwg_path:       str,
    output_dir:     Optional[str]  = None,
    output_filename:Optional[str]  = None,
    preferred_backend: str         = "auto",
    dxf_version:    str            = "ACAD2018",
    validate:       bool           = True,
) -> ConversionResult:
    """
    Convert a DWG file to DXF.

    Args:
        dwg_path:          Path to the .dwg file.
        output_dir:        Directory for output .dxf (defaults to same as input).
        output_filename:   Override output filename (without extension).
        preferred_backend: "oda" | "libreoffice" | "ezdxf" | "auto"
                           "auto" tries ODA → LibreOffice → ezdxf in order.
        dxf_version:       ODA output version (default "ACAD2018").
        validate:          Run basic DXF validation after conversion.

    Returns:
        ConversionResult
    """
    src = Path(dwg_path)
    if not src.exists():
        return ConversionResult(
            source_path=dwg_path,
            success=False,
            error=f"File not found: {dwg_path}",
        )

    if src.suffix.lower() != ".dwg":
        return ConversionResult(
            source_path=dwg_path,
            success=False,
            error=f"Not a DWG file: {dwg_path}",
        )

    out_dir = Path(output_dir) if output_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem     = output_filename or src.stem
    dxf_dest = out_dir / (stem + ".dxf")

    # ── Try backends ─────────────────────────────────────────────────────────
    backends = []
    if preferred_backend == "auto":
        if _find_oda():         backends.append("oda")
        if _find_libreoffice(): backends.append("libreoffice")
        if _has_ezdxf():        backends.append("ezdxf")
        if not backends:
            return ConversionResult(
                source_path=dwg_path,
                success=False,
                error=(
                    "No DWG conversion backend found.\n"
                    "Options:\n"
                    "  • ODA File Converter (best): https://www.opendesign.com/guestfiles/oda_file_converter\n"
                    "  • LibreOffice: sudo apt install libreoffice\n"
                    "  • ezdxf (limited): pip install ezdxf"
                ),
            )
    else:
        backends = [preferred_backend]

    last_error = ""
    for backend in backends:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_out = Path(tmpdir)
                if backend == "oda":
                    raw = _convert_with_oda(src, tmp_out, dxf_version)
                elif backend == "libreoffice":
                    raw = _convert_with_libreoffice(src, tmp_out)
                elif backend == "ezdxf":
                    raw = _convert_with_ezdxf(src, tmp_out)
                else:
                    continue

                # Move to final destination
                shutil.move(str(raw), str(dxf_dest))

            warnings = _validate_dxf(dxf_dest) if validate else []
            logger.info(f"Converted '{src.name}' → '{dxf_dest.name}' via {backend}")

            return ConversionResult(
                source_path=str(src),
                output_path=str(dxf_dest),
                backend_used=backend,
                success=True,
                warnings=warnings,
            )

        except ConversionError as e:
            last_error = str(e)
            logger.warning(f"Backend '{backend}' failed for '{src.name}': {e}")
            continue
        except subprocess.TimeoutExpired:
            last_error = f"Conversion timed out after 120s (backend: {backend})"
            logger.warning(last_error)
            continue
        except Exception as e:
            last_error = f"Unexpected error with backend '{backend}': {e}"
            logger.error(last_error, exc_info=True)
            continue

    return ConversionResult(
        source_path=str(src),
        success=False,
        error=f"All backends failed. Last error: {last_error}",
    )


def batch_convert_dwg(
    dwg_paths:       list[str],
    output_dir:      Optional[str] = None,
    preferred_backend: str         = "auto",
    max_workers:     int           = 4,
    progress_cb      = None,
) -> dict[str, ConversionResult]:
    """
    Convert multiple DWG files concurrently.

    Args:
        dwg_paths:         List of .dwg file paths.
        output_dir:        Shared output directory (defaults to each file's dir).
        preferred_backend: Same as convert_dwg().
        max_workers:       Thread pool size.
        progress_cb:       Optional callable(completed, total, result) for progress.

    Returns:
        Dict mapping source path → ConversionResult.
    """
    if not dwg_paths:
        return {}

    results: dict[str, ConversionResult] = {}
    total = len(dwg_paths)

    def _do(path):
        return path, convert_dwg(
            path,
            output_dir=output_dir,
            preferred_backend=preferred_backend,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_do, p): p for p in dwg_paths}
        for i, future in enumerate(as_completed(futures), 1):
            path, result = future.result()
            results[path] = result
            if progress_cb:
                progress_cb(i, total, result)
            status = "✅" if result.success else "❌"
            logger.info(f"{status} [{i}/{total}] {Path(path).name} → {result.backend_used or 'failed'}")

    return results


def get_available_backends() -> dict[str, str | None]:
    """Return which conversion backends are available on this system."""
    return {
        "oda":         _find_oda(),
        "libreoffice": _find_libreoffice(),
        "ezdxf":       "installed" if _has_ezdxf() else None,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert DWG files to DXF for the Floor Plan Area Calculator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dwg_converter.py floor_plan.dwg
  python dwg_converter.py floor_plan.dwg --output-dir ./dxf --backend libreoffice
  python dwg_converter.py --batch A1.dwg A2.dwg A3.dwg --output-dir ./dxf
  python dwg_converter.py --check-backends
""",
    )
    parser.add_argument("files", nargs="*", help="DWG file(s) to convert")
    parser.add_argument("--batch",        action="store_true",  help="Batch mode (convert all files)")
    parser.add_argument("--output-dir",   default=None,         help="Output directory for DXF files")
    parser.add_argument("--backend",      default="auto",
                        choices=["auto","oda","libreoffice","ezdxf"],
                        help="Conversion backend (default: auto)")
    parser.add_argument("--workers",      type=int, default=4,  help="Parallel workers for batch mode")
    parser.add_argument("--check-backends", action="store_true", help="List available backends and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    if args.check_backends:
        backends = get_available_backends()
        print("\nAvailable DWG conversion backends:")
        for name, path in backends.items():
            status = f"✅  {path}" if path else "❌  Not found"
            print(f"  {name:<14} {status}")
        print()
        return

    if not args.files:
        parser.print_help()
        return

    if args.batch or len(args.files) > 1:
        def progress(done, total, result):
            name = Path(result.source_path).name
            if result.success:
                print(f"  ✅ [{done}/{total}] {name} → {result.output_path}")
            else:
                print(f"  ❌ [{done}/{total}] {name}: {result.error}")

        print(f"\nBatch converting {len(args.files)} file(s)…\n")
        results = batch_convert_dwg(
            args.files,
            output_dir=args.output_dir,
            preferred_backend=args.backend,
            max_workers=args.workers,
            progress_cb=progress,
        )
        ok  = sum(1 for r in results.values() if r.success)
        bad = len(results) - ok
        print(f"\n  {ok} succeeded / {bad} failed\n")

    else:
        path = args.files[0]
        print(f"\nConverting {path}…")
        result = convert_dwg(path, output_dir=args.output_dir, preferred_backend=args.backend)
        if result.success:
            print(f"  ✅ Output: {result.output_path}  (backend: {result.backend_used})")
            for w in result.warnings:
                print(f"  ⚠️  {w}")
        else:
            print(f"  ❌ Failed: {result.error}")
            sys.exit(1)


if __name__ == "__main__":
    _cli()
