# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies ────────────────────────────────────────────────────────
# tesseract-ocr   → OCR for scanned PDFs and images
# poppler-utils   → pdf2image (PDF → image conversion)
# libreoffice     → DWG → DXF conversion fallback
# libgl1          → required by some image processing libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-chi-tra \
    poppler-utils \
    libreoffice \
    libgl1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App source ────────────────────────────────────────────────────────────────
COPY . .

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p uploads outputs

# ── Non-root user (security best practice) ────────────────────────────────────
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Start command ─────────────────────────────────────────────────────────────
# gunicorn: production WSGI server
# --workers 2        → 2 worker processes (good for Render free tier RAM)
# --timeout 120      → allow 120s for large file uploads/processing
# --bind 0.0.0.0     → listen on all interfaces
CMD ["gunicorn", "api:app", "--workers", "2", "--timeout", "120", "--bind", "0.0.0.0:8000"]
