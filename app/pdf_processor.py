import base64
import io
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import fitz

from .sku_extractor import extract_invoice_product, extract_invoice_sku, extract_sku_from_page


def _norm(value: str) -> str:
    value = (value or "").lower().replace("’", "'")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(value: str) -> set:
    return set(_norm(value).split())


def match_product_to_sku(product_name: str, mapping: Dict[str, str]) -> Tuple[Optional[str], float, Optional[str]]:
    """Match invoice product to CSV product_name. Returns sku, confidence, matched CSV name."""
    if not product_name or not mapping:
        return None, 0.0, None
    p = _norm(product_name)
    pt = _tokens(product_name)
    best = (0.0, None, None)
    for csv_name, sku in mapping.items():
        c = _norm(csv_name)
        if not c:
            continue
        if p == c:
            score = 1.0
        elif c in p or p in c:
            score = 0.96
        else:
            ct = _tokens(csv_name)
            union = pt | ct
            overlap = len(pt & ct) / len(union) if union else 0.0
            seq = SequenceMatcher(None, p, c).ratio()
            # Token overlap is more useful for long Amazon descriptions; sequence helps short names.
            score = 0.72 * overlap + 0.28 * seq
        if score > best[0]:
            best = (score, sku, csv_name)
    score, sku, name = best
    # Avoid silently assigning an unrelated SKU.
    if score < 0.56:
        return None, score, None
    return sku, score, name


def _is_label_page(page: fitz.Page) -> bool:
    text = page.get_text("text") or ""
    return bool(re.search(r"\bShip\s+To\s*:", text, re.I) and ("AWB" in text or "Order Id" in text or "Delivery Station" in text))


def _is_invoice_page(page: fitz.Page) -> bool:
    text = page.get_text("text") or ""
    return "Tax Invoice/Bill of Supply/Cash Memo" in text or "Tax Invoice" in text


def find_label_invoice_pairs(doc: fitz.Document) -> List[Tuple[int, Optional[int]]]:
    """Pair invoice pages with the shipping-label page immediately before them.

    Amazon's current label format can be image-only (no selectable text), so relying
    on label OCR/text is fragile. Invoice pages contain the product description and
    are therefore the authoritative matching anchor.
    """
    invoice_pairs: List[Tuple[int, Optional[int]]] = []
    invoice_seen = False
    for i, page in enumerate(doc):
        if not _is_invoice_page(page):
            continue
        invoice_seen = True
        label_idx = i - 1 if i > 0 and not _is_invoice_page(doc[i - 1]) else None
        if label_idx is not None:
            invoice_pairs.append((label_idx, i))

    if invoice_pairs:
        return invoice_pairs
    # No invoices: assume the uploaded PDF is already label-only and preserve every page.
    if not invoice_seen:
        return [(i, None) for i in range(doc.page_count)]
    return []


def analyze_pdf(pdf_bytes: bytes, mapping: Optional[Dict[str, str]] = None):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise ValueError("Password-protected PDF is not supported.")
    pairs = find_label_invoice_pairs(doc)
    if not pairs:
        # Fall back to treating every page as a label for simple label-only PDFs.
        pairs = [(i, None) for i in range(doc.page_count)]

    items = []
    for seq, (label_idx, invoice_idx) in enumerate(pairs, start=1):
        label_page = doc[label_idx]
        detected = extract_sku_from_page(label_page)
        product = None
        matched_csv = None
        confidence = 0.0
        source = "label"
        if invoice_idx is not None:
            invoice_text = doc[invoice_idx].get_text("text") or ""
            product = extract_invoice_product(invoice_text)
            if mapping and product:
                matched, confidence, matched_csv = match_product_to_sku(product, mapping)
                if matched:
                    detected = matched
                    source = "csv-match"
            if not detected:
                invoice_sku = extract_invoice_sku(invoice_text)
                if invoice_sku:
                    detected = invoice_sku
                    source = "invoice"
        items.append({
            "sequence": seq,
            "label_page": label_idx,
            "invoice_page": invoice_idx,
            "sku": detected,
            "product": product,
            "matched_csv": matched_csv,
            "confidence": round(confidence, 3),
            "source": source if detected else "missing",
        })
    doc.close()
    return items


def _rect_overlaps_existing_content(page: fitz.Page, rect: fitz.Rect) -> bool:
    for block in page.get_text("blocks"):
        if len(block) >= 5:
            try:
                block_rect = fitz.Rect(block[:4])
            except Exception:
                continue
            if rect.intersects(block_rect):
                return True
    return False


def stamp_sku(page: fitz.Page, sku: str) -> None:
    """Print only the SKU in a dedicated area *below* the original label.

    The original Amazon label artwork ends at ``original_height``. The caller
    creates extra page space below that artwork, so the SKU can never overlap
    the label and is not clipped by the original page boundary.
    """
    width, height = page.rect.width, page.rect.height
    margin = max(12, min(width, height) * 0.025)
    font_size = max(10, min(14, width * 0.028))

    # The bottom strip is intentionally blank: no border, no background, no
    # "SKU:" prefix. Center the raw SKU in the added area.
    rect = fitz.Rect(margin, height - 34, width - margin, height - 8)
    page.insert_textbox(
        rect,
        sku,
        fontsize=font_size,
        fontname="helv",
        color=(0, 0, 0),
        align=1,
        overlay=True,
    )


def process_pdf(pdf_bytes: bytes, mapping: Optional[Dict[str, str]] = None, manual_skus: Optional[List[Optional[str]]] = None, labels_only: bool = True):
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    if src.is_encrypted and not src.authenticate(""):
        src.close()
        raise ValueError("Password-protected PDF is not supported.")
    if src.page_count == 0:
        src.close()
        raise ValueError("The PDF contains no pages.")

    analysis = analyze_pdf(pdf_bytes, mapping)
    if manual_skus is not None and len(manual_skus) != len(analysis):
        src.close()
        raise ValueError("Manual SKU data does not match the number of shipping labels.")

    skus = []
    for i, item in enumerate(analysis):
        sku = manual_skus[i].strip() if manual_skus and manual_skus[i] else item.get("sku")
        skus.append(sku)

    # Create a fresh document so label-only mode can safely omit invoices while preserving original label pages.
    if labels_only:
        out_doc = fitz.open()
        for i, item in enumerate(analysis):
            page = src[item["label_page"]]
            original_width, original_height = page.rect.width, page.rect.height
            # Extend the page downward so the SKU is physically outside the
            # original Amazon label instead of being clipped/overlapping it.
            sku_area_height = 44
            new_page = out_doc.new_page(
                width=original_width,
                height=original_height + sku_area_height,
            )
            new_page.show_pdf_page(
                fitz.Rect(0, 0, original_width, original_height),
                src,
                item["label_page"],
            )
            if skus[i]:
                stamp_sku(new_page, skus[i])
    else:
        out_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for i, item in enumerate(analysis):
            if skus[i]:
                stamp_sku(out_doc[item["label_page"]], skus[i])

    out = io.BytesIO()
    out_doc.save(out, garbage=4, deflate=True, clean=True)
    out_doc.close()
    src.close()
    return out.getvalue(), skus, analysis


def preview_pdf(pdf_bytes: bytes, max_pages: int = 12) -> List[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    thumbs: List[str] = []
    for page in list(doc)[:max_pages]:
        pix = page.get_pixmap(matrix=fitz.Matrix(0.65, 0.65), alpha=False)
        encoded = base64.b64encode(pix.tobytes("png")).decode("ascii")
        thumbs.append(f"data:image/png;base64,{encoded}")
    doc.close()
    return thumbs
