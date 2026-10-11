import re
from typing import Optional

SKU_PATTERNS = [
    re.compile(r"\b(?:seller\s*sku|merchant\s*sku|sellersku|sku)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._/&+ -]{0,99})", re.I),
]


def clean_sku(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" :|-\t\r\n")
    return value[:100].strip()


def extract_sku_from_text(text: str) -> Optional[str]:
    for pattern in SKU_PATTERNS:
        m = pattern.search(text or "")
        if m:
            candidate = clean_sku(m.group(1))
            # Stop at common label/table boundaries.
            candidate = re.split(r"\s+(?:ASIN|HSN|Qty|Price|Order|Invoice)\b", candidate, flags=re.I)[0].strip()
            if candidate:
                return candidate
    return None


def extract_sku_from_page(page) -> Optional[str]:
    return extract_sku_from_text(page.get_text("text") or "")


def extract_invoice_product(text: str) -> Optional[str]:
    """Extract the product description immediately before the invoice ASIN."""
    text = text or ""
    asin_matches = list(re.finditer(r"\bB0[A-Z0-9]{8,13}\b", text, re.I))
    if not asin_matches:
        return None
    asin = asin_matches[0]
    # Find the last invoice item-number marker before the ASIN. This handles both
    # `1 Product...` and `1\nProduct...` layouts used by Amazon invoices.
    markers = list(re.finditer(r"(?m)^1(?:\s+|$)", text[:asin.start()]))
    start = markers[-1].end() if markers else max(0, asin.start() - 1200)
    product = text[start:asin.start()]
    # Strip common invoice table headers accidentally captured by the fallback.
    product = re.sub(r"^(?:\s*No\s+Description.*?Amount\s*)", "", product, flags=re.I | re.S)
    product = re.sub(r"\s+", " ", product).strip(" |:\t\r\n")
    # The item description may have a pipe immediately before the ASIN.
    product = re.sub(r"\s*\|\s*$", "", product).strip()
    return product or None


def extract_invoice_sku(text: str) -> Optional[str]:
    """Fallback: Amazon invoices commonly contain the seller SKU in parentheses after the ASIN."""
    m = re.search(r"\bB0[A-Z0-9]{8,13}\s*\(\s*([^\)]+?)\s*\)", text or "", re.I)
    return clean_sku(m.group(1)) if m else None


def extract_invoice_items(text: str):
    """Extract each invoice line item's seller SKU and quantity.

    Amazon invoices usually show the seller SKU in parentheses after the ASIN,
    and the line quantity between unit price and net amount.
    """
    text = text or ""
    items = []
    asin_matches = list(re.finditer(r"\bB0[A-Z0-9]{8,13}\b", text, re.I))
    for index, asin_match in enumerate(asin_matches):
        end = asin_matches[index + 1].start() if index + 1 < len(asin_matches) else len(text)
        chunk = text[asin_match.start():end]
        sku_match = re.search(r"\bB0[A-Z0-9]{8,13}\s*\(\s*([^\)]+?)\s*\)", chunk, re.I)
        if not sku_match:
            continue
        old_sku = clean_sku(sku_match.group(1))
        # Limit quantity parsing to this line item, before the next invoice item.
        qty_match = re.search(r"₹\s*[\d,]+(?:\.\d{1,2})?\s+(\d{1,4})\s+₹", chunk, re.I)
        if not qty_match:
            # Some PDF text extraction layouts put HSN and price on separate lines.
            qty_match = re.search(r"HSN\s*:?[^₹]{0,100}₹\s*[\d,]+(?:\.\d{1,2})?\s+(\d{1,4})\s+₹", chunk, re.I | re.S)
        quantity = int(qty_match.group(1)) if qty_match else 1
        # Product description is the text before the ASIN, cleaned of table marker.
        before = text[:asin_match.start()]
        last_marker = re.search(r"(?s)(?:^|\n)\s*1\s+(.{0,1200})$", before)
        product = re.sub(r"\s+", " ", last_marker.group(1) if last_marker else "").strip(" |:\t\r\n")
        product = re.sub(r"\s*\|\s*$", "", product).strip()
        items.append({"old_sku": old_sku, "quantity": max(1, quantity), "product": product or None})
    return items
