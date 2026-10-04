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


def _clean_sku(value: Optional[str]) -> str:
    """Return only the SKU value, never a legacy `SKU:` prefix."""
    if not value:
        return ""
    return re.sub(r"^\s*SKU\s*:\s*", "", str(value).strip(), flags=re.I)


def stamp_sku(
    page: fitz.Page,
    sku: str,
    x: float,
    y: float,
    font_size: float = 10,
) -> None:
    """Print ONLY the SKU text at user-controlled PDF coordinates.

    Coordinates are measured from the top-left of the output page, matching
    PyMuPDF's coordinate system. There is deliberately no rectangle, border,
    background, or `SKU:` prefix.
    """
    sku = _clean_sku(sku)
    if not sku:
        return

    width, height = page.rect.width, page.rect.height
    font_size = max(6.0, min(30.0, float(font_size)))
    x = max(0.0, min(float(x), max(0.0, width - 5.0)))
    y = max(font_size, min(float(y), height - 1.0))

    page.insert_text(
        (x, y),
        sku,
        fontsize=font_size,
        fontname="helv",
        color=(0, 0, 0),
        overlay=True,
    )


def process_pdf(
    pdf_bytes: bytes,
    mapping: Optional[Dict[str, str]] = None,
    manual_skus: Optional[List[Optional[str]]] = None,
    labels_only: bool = True,
    sku_x: float = 300.0,
    sku_y: Optional[float] = None,
    sku_font_size: float = 10.0,
):
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
        skus.append(_clean_sku(sku) or None)

    # Build the output in label/invoice sequence. When invoices are included,
    # each shipping label is followed by its original Amazon invoice page.
    # The label page is extended downward so the SKU remains outside the
    # original label artwork; the invoice page is preserved unchanged.
    out_doc = fitz.open()
    for i, item in enumerate(analysis):
        label_page = src[item["label_page"]]
        original_width, original_height = label_page.rect.width, label_page.rect.height
        sku_area_height = 60
        new_page = out_doc.new_page(
            width=original_width,
            height=original_height + sku_area_height,
        )
        effective_y = float(sku_y) if sku_y is not None else original_height + 32
        effective_y = max(8.0, min(effective_y, new_page.rect.height - 2.0))
        new_page.show_pdf_page(
            fitz.Rect(0, 0, original_width, original_height),
            src,
            item["label_page"],
        )
        if skus[i]:
            stamp_sku(new_page, skus[i], sku_x, effective_y, sku_font_size)

        # Keep the invoice immediately after its corresponding label when
        # the user asks to include invoices.
        if not labels_only and item.get("invoice_page") is not None:
            invoice_page = src[item["invoice_page"]]
            invoice_copy = out_doc.new_page(
                width=invoice_page.rect.width,
                height=invoice_page.rect.height,
            )
            invoice_copy.show_pdf_page(invoice_copy.rect, src, item["invoice_page"])

    # For label-only mode the loop above already produced only shipping labels.
    # If the source PDF had no invoice pages, this also preserves every source page.
    if not analysis:
        raise ValueError("No shipping labels were found in the PDF.")

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
