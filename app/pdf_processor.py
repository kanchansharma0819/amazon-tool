import base64
import io
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import fitz

from .sku_extractor import extract_invoice_items, extract_invoice_product, extract_invoice_sku, extract_sku_from_page


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


def _norm_sku(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

def map_invoice_items(invoice_text: str, mapping: Dict[str, str]):
    """Return output lines using Old SKU -> New SKU mappings and invoice quantities."""
    extracted = extract_invoice_items(invoice_text)
    output = []
    matched_names = []
    for item in extracted:
        old_sku = item["old_sku"]
        mapping_key = next((old for old in mapping if _norm_sku(old) == _norm_sku(old_sku)), None)
        new_sku = mapping.get(mapping_key) if mapping_key is not None else None
        # Exact normalized SKU matching avoids accidentally assigning another size/color.
        if not new_sku:
            new_sku = old_sku
        output.append((new_sku, item["quantity"]))
        if mapping_key is not None:
            matched_names.append(old_sku)
    # Merge repeated mapped SKUs while preserving the first-seen order.
    merged = []
    indexes = {}
    for sku, qty in output:
        key = _norm_sku(sku)
        if key in indexes:
            merged[indexes[key]][1] += qty
        else:
            indexes[key] = len(merged)
            merged.append([sku, qty])
    lines = [f"{sku}({qty})" for sku, qty in merged]
    return lines, [x for x in matched_names if x]


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


def analyze_pdf(pdf_bytes: bytes, mapping: Optional[Dict[str, str]] = None, mapping_mode: str = "old_new"):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.is_encrypted and not doc.authenticate(""):
        doc.close()
        raise ValueError("Password-protected PDF is not supported.")
    pairs = find_label_invoice_pairs(doc)
    if not pairs:
        pairs = [(i, None) for i in range(doc.page_count)]

    items = []
    for seq, (label_idx, invoice_idx) in enumerate(pairs, start=1):
        label_page = doc[label_idx]
        detected = extract_sku_from_page(label_page)
        product = None
        matched_csv = None
        confidence = 0.0
        source = "label"
        sku_lines = []
        if invoice_idx is not None:
            invoice_text = doc[invoice_idx].get_text("text") or ""
            product = extract_invoice_product(invoice_text)
            invoice_items = extract_invoice_items(invoice_text)
            if mapping and invoice_items:
                if mapping_mode == "old_new":
                    sku_lines, matched_old = map_invoice_items(invoice_text, mapping)
                else:
                    legacy_sku, legacy_conf, legacy_name = match_product_to_sku(product or "", mapping)
                    if legacy_sku:
                        total_qty = sum(item["quantity"] for item in invoice_items)
                        sku_lines = [f"{legacy_sku}({total_qty})"]
                        matched_old = [legacy_name] if legacy_name else []
                        confidence = legacy_conf
                if sku_lines:
                    detected = "\n".join(sku_lines)
                    matched_csv = ", ".join(matched_old) if matched_old else None
                    source = "csv-match" if matched_old else "invoice-sku"
                    if matched_old and confidence == 0.0:
                        confidence = 1.0
            if not detected or source == "label":
                invoice_sku = extract_invoice_sku(invoice_text)
                if invoice_sku:
                    detected = invoice_sku
                    source = "invoice"
        items.append({
            "sequence": seq, "label_page": label_idx, "invoice_page": invoice_idx,
            "sku": detected, "sku_lines": sku_lines, "product": product,
            "matched_csv": matched_csv, "confidence": round(confidence, 3),
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

    for line_index, line in enumerate(str(sku).splitlines()):
        line = line.strip()
        if not line:
            continue
        page.insert_text(
            (x, y + line_index * (font_size + 3)),
            line,
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
    mapping_mode: str = "old_new",
):
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    if src.is_encrypted and not src.authenticate(""):
        src.close()
        raise ValueError("Password-protected PDF is not supported.")
    if src.page_count == 0:
        src.close()
        raise ValueError("The PDF contains no pages.")

    analysis = analyze_pdf(pdf_bytes, mapping, mapping_mode=mapping_mode)
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
        max_lines = max(1, len((skus[i] or "").splitlines()))
        sku_area_height = max(60, int(max_lines * (float(sku_font_size) + 3) + 20))
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
