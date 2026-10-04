import base64
import csv
import io
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .pdf_processor import analyze_pdf, preview_pdf, process_pdf

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_CSV_SIZE = 5 * 1024 * 1024
SESSION_TTL = 60 * 60

app = FastAPI(title="Amazon SKU Label Printer", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# Short-lived in-memory state avoids persistent storage of customer label PDFs.
sessions: Dict[str, Dict[str, Any]] = {}


class GenerateRequest(BaseModel):
    file_id: str
    skus: List[Optional[str]]
    labels_only: bool = True


def cleanup_sessions() -> None:
    cutoff = time.time() - SESSION_TTL
    expired = [key for key, value in sessions.items() if value.get("created", 0) < cutoff]
    for key in expired:
        sessions.pop(key, None)


def validate_pdf(data: bytes, filename: str) -> None:
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a valid PDF file.")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, "PDF is too large. Maximum supported size is 25 MB.")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.is_encrypted and not doc.authenticate(""):
            doc.close()
            raise HTTPException(400, "Password-protected PDFs are not supported.")
        if doc.page_count == 0:
            doc.close()
            raise HTTPException(400, "The PDF contains no pages.")
        doc.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "The uploaded PDF is corrupted or unsupported.") from exc


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cleanup_sessions()
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    cleanup_sessions()
    data = await file.read()
    validate_pdf(data, file.filename or "upload.pdf")
    file_id = secrets.token_urlsafe(16)
    doc = fitz.open(stream=data, filetype="pdf")
    pages = doc.page_count
    doc.close()
    sessions[file_id] = {
        "created": time.time(),
        "original": data,
        "filename": file.filename or "amazon_labels.pdf",
        "mapping": {},
    }
    return {"file_id": file_id, "filename": file.filename, "size": len(data), "pages": pages}


@app.post("/upload-csv")
async def upload_csv(file_id: str, file: UploadFile = File(...)):
    cleanup_sessions()
    state = sessions.get(file_id)
    if not state:
        raise HTTPException(404, "Upload session not found. Please upload the PDF again.")
    name = (file.filename or "").lower()
    if not name.endswith(".csv"):
        raise HTTPException(400, "Please upload a CSV file.")
    data = await file.read()
    if len(data) > MAX_CSV_SIZE:
        raise HTTPException(413, "CSV is too large. Maximum supported size is 5 MB.")
    try:
        text = data.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as exc:
        raise HTTPException(400, "Could not read the CSV. Please save it as UTF-8 CSV.") from exc
    if not rows:
        raise HTTPException(400, "The CSV is empty.")
    headers = {str(k).strip().lower(): k for k in (rows[0].keys() if rows else []) if k}
    product_key = next((headers[k] for k in ("product_name", "product name", "name", "product") if k in headers), None)
    sku_key = next((headers[k] for k in ("sku", "seller_sku", "seller sku", "merchant_sku", "merchant sku") if k in headers), None)
    if not product_key or not sku_key:
        raise HTTPException(400, "CSV must contain product_name and sku columns. Example: product_name,sku")
    mapping = {}
    for row in rows:
        product = str(row.get(product_key, "") or "").strip()
        sku = str(row.get(sku_key, "") or "").strip()
        if product and sku:
            mapping[product] = sku[:100]
    if not mapping:
        raise HTTPException(400, "No usable product_name + sku rows were found in the CSV.")
    state["mapping"] = mapping
    state["csv_filename"] = file.filename
    return {"file_id": file_id, "filename": file.filename, "rows": len(mapping)}


@app.get("/template-csv")
async def template_csv():
    content = "product_name,sku\nUNIKAN Women's Checkered Oversized Shirt for Women,UNIKAN-BLACK-M\nCutie Scooter Doll | Toy Scooter Doll for Kids,cutie-scooter-doll\n"
    return Response(content=content, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="sku_mapping_template.csv"'})


@app.post("/process")
async def process(payload: Dict[str, Any]):
    cleanup_sessions()
    file_id = payload.get("file_id")
    if not file_id or file_id not in sessions:
        raise HTTPException(404, "Upload session not found. Please upload the PDF again.")
    try:
        state = sessions[file_id]
        mapping = state.get("mapping") or {}
        analysis = analyze_pdf(state["original"], mapping)
        output, skus, analysis = process_pdf(state["original"], mapping=mapping, labels_only=True)
        state["processed"] = output
        state["skus"] = skus
        state["analysis"] = analysis
        return {
            "file_id": file_id,
            "pages": len(skus),
            "source_pages": fitz.open(stream=state["original"], filetype="pdf").page_count,
            "detected": sum(bool(s) for s in skus),
            "missing": sum(not bool(s) for s in skus),
            "skus": skus,
            "analysis": analysis,
            "previews": preview_pdf(output),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, "Something went wrong while processing the PDF. Please try again.") from exc


@app.post("/generate")
async def generate(payload: GenerateRequest):
    cleanup_sessions()
    state = sessions.get(payload.file_id)
    if not state:
        raise HTTPException(404, "Generated session expired. Please upload the PDF again.")
    analysis = state.get("analysis") or []
    if len(payload.skus) != len(analysis):
        raise HTTPException(400, "SKU count does not match the number of shipping labels.")
    if any(s is not None and len(s.strip()) > 100 for s in payload.skus):
        raise HTTPException(400, "A SKU is too long. Please keep each SKU under 100 characters.")
    try:
        output, skus, analysis = process_pdf(state["original"], mapping=state.get("mapping") or {}, manual_skus=payload.skus, labels_only=payload.labels_only)
        state["processed"] = output
        state["skus"] = skus
        state["analysis"] = analysis
        return {
            "file_id": payload.file_id,
            "pages": len(skus),
            "detected": sum(bool(s) for s in skus),
            "missing": sum(not bool(s) for s in skus),
            "analysis": analysis,
            "previews": preview_pdf(output),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, "Something went wrong while generating the PDF.") from exc


@app.get("/download/{file_id}")
async def download(file_id: str):
    cleanup_sessions()
    state = sessions.get(file_id)
    if not state or not state.get("processed"):
        raise HTTPException(404, "Generated PDF not found. Please process the PDF again.")
    return Response(
        content=state["processed"],
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="amazon_labels_with_sku.pdf"'},
    )
