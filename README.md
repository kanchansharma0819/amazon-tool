# Amazon SKU Label Printer

A production-oriented FastAPI web app for Amazon sellers. Upload an Amazon shipping-label PDF, detect explicitly labelled SKUs, add a bold SKU box to the bottom-right area of each label without rasterizing the original page, review missing SKUs, and download a ready-to-print PDF.

## Features

- Upload Amazon PDF labels
- Automatic SKU / Merchant SKU / Seller SKU extraction
- Manual SKU correction for labels where extraction is unavailable
- Bottom-right vector SKU box with collision-aware positioning
- Preserves original page dimensions and existing PDF content
- PDF thumbnail preview
- Ready-to-print PDF download
- Short-lived in-memory processing; no customer-label database
- File validation, size limits, and friendly errors
- Responsive desktop/tablet/mobile UI
- Render-ready configuration

## Project structure

```text
amazon-sku-label-printer/
├── app/
│   ├── main.py
│   ├── pdf_processor.py
│   ├── sku_extractor.py
│   ├── templates/index.html
│   └── static/
│       ├── css/style.css
│       └── js/app.js
├── uploads/.gitkeep
├── output/.gitkeep
├── requirements.txt
├── render.yaml
├── Procfile
├── .gitignore
└── README.md
```

## Local installation

Python 3.10+ is recommended.

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd amazon-sku-label-printer
python -m venv .venv
```

Windows:
```bash
.venv\\Scripts\\activate
```

macOS/Linux:
```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## GitHub deployment

```bash
git init
git add .
git commit -m "Initial Amazon SKU Label Printer"
git branch -M main
git remote add origin <YOUR_GITHUB_REPOSITORY_URL>
git push -u origin main
```

Do not commit Amazon PDFs. The `.gitignore` excludes PDFs and temporary folders.

## Render deployment

### Option A: Blueprint

Push the repository to GitHub, then in Render choose **New → Blueprint** and select the repository. Render will use `render.yaml`.

### Option B: Web Service

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Render supplies `$PORT`; the app binds to `0.0.0.0`.

## API

- `POST /upload` — multipart PDF upload
- `POST /process` — detect SKUs and create an intermediate processed PDF
- `POST /generate` — apply supplied/manual SKUs and create final PDF
- `GET /download/{file_id}` — download final PDF
- `GET /health` — health check

## How SKU extraction works

`app/sku_extractor.py` is intentionally modular. It prioritizes explicit fields such as:

- `SKU:`
- `Merchant SKU:`
- `Seller SKU:`
- `SellerSKU:`

It does **not** guess a SKU from arbitrary text. If Amazon changes its label layout, update `extract_sku_from_page()` with a new explicit pattern.

## Privacy and retention

Uploaded PDFs are kept only in process memory and associated with a random temporary session ID. Sessions expire after one hour. No customer-address data is intentionally persisted to disk or a database.

For a multi-worker production deployment, replace the in-memory session store with a short-lived encrypted object store or Redis if needed. Do not store label PDFs longer than operationally necessary.

## Processing quality

The original PDF page is modified directly with PyMuPDF. The application does not rasterize the whole page, so existing barcodes and QR codes remain vector/sharp whenever they were vector content in the source PDF.

## End-to-end test

1. Start the app locally.
2. Upload an Amazon PDF containing a label such as `Seller SKU: UNIKAN-PINK-M`.
3. Click **Process PDF**.
4. Confirm the SKU is detected.
5. For missing SKUs, enter them manually.
6. Click **Apply SKU & Generate PDF**.
7. Click **Download Ready PDF**.
8. Open the downloaded PDF and confirm the original label remains intact and the SKU is visible near the bottom-right.

## CSV Product → SKU Matching

The application supports a second upload: a CSV containing `product_name,sku`.
For Amazon PDFs that contain a shipping label followed by its tax invoice, the app pairs those pages, reads the product description from the invoice, and fuzzy-matches it to the CSV product name. The matched SKU is then printed on the corresponding shipping label in sequence.

Example CSV:

```csv
product_name,sku
UNIKAN Women's Checkered Oversized Shirt for Women,UNIKAN-BLACK-M
Cutie Scooter Doll | Toy Scooter Doll for Kids,cutie-scooter-doll
```

By default the final PDF contains shipping-label pages only, in their original sequence, with invoice pages removed. The UI also allows keeping the full original page set.

### Recommended CSV format

Use UTF-8 CSV with exactly two important columns:

- `product_name` — the product name/title as it appears in the Amazon invoice
- `sku` — the SKU that should be printed on the shipping label

For products that differ only by size/color, include those details in `product_name` so the matcher can select the correct SKU.

### How matching works

1. The app identifies Amazon invoice pages.
2. It pairs each invoice with the shipping-label page immediately before it.
3. It extracts the invoice product description.
4. It matches that description against `product_name` in the uploaded CSV.
5. It places the corresponding `sku` on the paired label.
6. Labels remain in the original sequence.
7. By default, invoice pages are removed from the final output, leaving a print-ready shipping-label PDF.
8. If a match is missing or uncertain, the SKU can be entered manually before generating the final PDF.

The app also supports the common Amazon case where the shipping-label page itself is image-based and has no selectable text; the invoice is used as the matching anchor instead of depending on label OCR.
