# AreaCalc HK — Floor Plan Area Calculator

Automated GFA / NOFA area calculator for Hong Kong floor plans.
Classifies rooms per **PNAP APP-2** and **APP-151** (Rev. July 2025).

---

## Deploy to Render (free, ~5 minutes)

### Step 1 — Push to GitHub

```bash
# If you don't have git set up yet:
git init
git add .
git commit -m "Initial deploy"

# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/areacalc-hk.git
git push -u origin main
```

### Step 2 — Connect to Render

1. Go to **[dashboard.render.com](https://dashboard.render.com)**
2. Click **New +** → **Blueprint**
3. Connect your GitHub account and select your repo
4. Render reads `render.yaml` automatically — click **Apply**
5. Wait ~5 minutes for the build to finish

Your app will be live at:
```
https://areacalc-hk-api.onrender.com
```

> **Free tier note:** The app sleeps after 15 minutes of inactivity.
> The first request after sleep takes ~30 seconds to wake up.
> Upgrade to Render Starter ($7/mo) to keep it always-on for clients.

---

## Run locally

```bash
# Install dependencies
pip install -r requirements.txt

# System dependencies (Ubuntu/Debian)
sudo apt install tesseract-ocr poppler-utils libreoffice

# Start the server
python api.py
# → http://localhost:5000
```

---

## Project structure

```
├── api.py                  Flask REST API + UI server
├── room_rules.py           APP-2 & APP-151 classification rules
├── area_calculator.py      GFA / NOFA totals + 10% cap engine
├── floor_plan_parser.py    DXF / PDF / image floor plan parser
├── dwg_converter.py        DWG → DXF conversion (LibreOffice / ODA)
├── batch_processor.py      Multi-floor building analysis
├── excel_exporter.py       3-sheet Excel area schedule exporter
├── index.html              Upload UI (served at /)
├── requirements.txt        Python dependencies
├── Dockerfile              Container build
└── render.yaml             Render deployment config
```

---

## API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Upload UI |
| GET | `/api/health` | Health check |
| GET | `/api/rules` | Room classification rule table |
| GET | `/api/backends` | Available DWG conversion backends |
| POST | `/api/classify` | Classify rooms from JSON |
| POST | `/api/analyse` | Upload + analyse a floor plan file |
| POST | `/api/analyse/batch` | Upload multiple floors (full building) |
| GET | `/api/download/<id>` | Download generated Excel schedule |

---

## Upgrading for client use

When you're ready to share with external AP / QS / surveyors:

1. **Upgrade Render plan** → Starter ($7/mo) — always-on, no cold starts
2. **Add authentication** — user login so each reviewer has their own projects
3. **Custom domain** — attach `areacalc.yourdomain.com` in Render settings
4. **Persistent storage** — add a database to save project history

All of these are incremental upgrades — nothing needs to be rebuilt.

---

## Supported file formats

| Format | Parser method |
|--------|--------------|
| `.dxf` | ezdxf — entity + hatch extraction |
| `.dwg` | Auto-converted to DXF via LibreOffice or ODA File Converter |
| `.pdf` (vector) | pdfplumber — text + geometry extraction |
| `.pdf` (scanned) | pdf2image + pytesseract OCR |
| `.jpg` / `.png` / `.tif` | pytesseract OCR |

---

## Rules last reviewed

- **Spec version:** PNAP APP-2 & APP-151 Rev. July 2025
- **QS review:** Feb 2026 — reviewer P
- **Pending:** Q2.2 utility platform clarification, Q6.1 minor Excel changes, additional reviewer sign-offs for full cap-exempt confirmation
