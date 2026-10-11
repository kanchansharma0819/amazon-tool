"""Small Supabase persistence layer for the single-owner SKU mapping app.

Only the server uses the Supabase service-role key. Never expose it to the browser.
"""
import os
import re
from typing import Dict, Iterable, List, Tuple

try:
    from supabase import create_client
except ImportError:  # Gives a useful runtime setup error if dependencies were not installed.
    create_client = None

_client = None
_client_key = None


def normalize_old_sku(value: str) -> str:
    # Preserve meaningful punctuation; normalize only case and whitespace.
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def configured() -> bool:
    return bool(os.getenv("SUPABASE_URL", "").strip() and os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())


def client():
    global _client, _client_key
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your hosting environment.")
    if create_client is None:
        raise RuntimeError("The Supabase Python package is missing. Install dependencies with: pip install -r requirements.txt")
    signature = (url, key)
    if _client is None or _client_key != signature:
        _client = create_client(url, key)
        _client_key = signature
    return _client


def list_mappings() -> List[dict]:
    db = client()
    result: List[dict] = []
    start = 0
    page_size = 1000
    while True:
        response = db.table("sku_mappings").select("id,old_sku,new_sku,normalized_old_sku,created_at,updated_at").order("old_sku").range(start, start + page_size - 1).execute()
        batch = response.data or []
        result.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return result


def mapping_dict() -> Dict[str, str]:
    return {row["old_sku"]: row["new_sku"] for row in list_mappings() if row.get("old_sku") and row.get("new_sku")}


def merge_mappings(incoming: Iterable[Tuple[str, str]]) -> dict:
    """Add new mappings, keep existing ones on conflict, and return a conflict report."""
    db = client()
    rows = [(str(old or "").strip(), str(new or "").strip()[:100]) for old, new in incoming]
    rows = [(old, new) for old, new in rows if old and new]
    # Detect conflicting duplicate keys inside this import before inserting anything.
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    for old, new in rows:
        grouped.setdefault(normalize_old_sku(old), []).append((old, new))

    conflicts = []
    candidates = []
    skipped = 0
    invalid_duplicates = set()
    for key, entries in grouped.items():
        distinct_new = {new for _, new in entries}
        if len(distinct_new) > 1:
            invalid_duplicates.add(key)
            conflicts.append({
                "normalized_old_sku": key,
                "old_sku": entries[0][0],
                "existing_new_sku": None,
                "incoming_new_sku": sorted(distinct_new),
                "reason": "Conflicting New SKU values within the uploaded file",
            })
            skipped += len(entries)
            continue
        # Identical duplicates within the file count once.
        old, new = entries[0]
        skipped += max(0, len(entries) - 1)
        candidates.append({"old_sku": old, "new_sku": new, "normalized_old_sku": key})

    existing_rows = list_mappings()
    existing = {row.get("normalized_old_sku") or normalize_old_sku(row.get("old_sku", "")): row for row in existing_rows}
    to_insert = []
    unchanged = 0
    for row in candidates:
        key = row["normalized_old_sku"]
        current = existing.get(key)
        if current:
            if current.get("new_sku") == row["new_sku"]:
                unchanged += 1
            else:
                conflicts.append({
                    "normalized_old_sku": key,
                    "old_sku": current.get("old_sku") or row["old_sku"],
                    "existing_new_sku": current.get("new_sku"),
                    "incoming_new_sku": row["new_sku"],
                    "reason": "This Old SKU already has a different New SKU",
                })
                skipped += 1
        else:
            to_insert.append(row)

    inserted = 0
    failed = []
    # Batch writes for normal imports. If a concurrent import races a unique key,
    # retry row-by-row so a conflict cannot overwrite an existing mapping.
    for offset in range(0, len(to_insert), 200):
        batch = to_insert[offset:offset + 200]
        try:
            response = db.table("sku_mappings").insert(batch).execute()
            inserted += len(response.data or batch)
        except Exception:
            for row in batch:
                try:
                    response = db.table("sku_mappings").insert(row).execute()
                    inserted += len(response.data or []) or 1
                except Exception as exc:
                    # Re-read the key to distinguish a concurrent duplicate from a real failure.
                    try:
                        found = db.table("sku_mappings").select("old_sku,new_sku,normalized_old_sku").eq("normalized_old_sku", row["normalized_old_sku"]).limit(1).execute().data
                    except Exception:
                        found = []
                    if found:
                        current = found[0]
                        if current.get("new_sku") != row["new_sku"]:
                            conflicts.append({
                                "normalized_old_sku": row["normalized_old_sku"],
                                "old_sku": current.get("old_sku") or row["old_sku"],
                                "existing_new_sku": current.get("new_sku"),
                                "incoming_new_sku": row["new_sku"],
                                "reason": "Another import saved a different mapping first",
                            })
                        else:
                            unchanged += 1
                        skipped += 1
                    else:
                        failed.append({"old_sku": row["old_sku"], "error": str(exc)[:200]})

    return {
        "added": inserted,
        "unchanged": unchanged,
        "skipped": skipped,
        "conflicts": conflicts,
        "failed": failed,
        "total": len(list_mappings()),
    }


def resolve_mapping(normalized_old_sku: str, new_sku: str, old_sku: str = "") -> dict:
    db = client()
    value = str(new_sku or "").strip()[:100]
    if not value:
        raise ValueError("New SKU cannot be empty.")
    key = normalize_old_sku(normalized_old_sku)
    response = db.table("sku_mappings").update({"new_sku": value}).eq("normalized_old_sku", key).execute()
    if response.data:
        return response.data[0]
    original = str(old_sku or normalized_old_sku or "").strip()
    if not original:
        raise LookupError("Old SKU is required to create a mapping from this conflict.")
    try:
        inserted = db.table("sku_mappings").insert({
            "old_sku": original,
            "new_sku": value,
            "normalized_old_sku": key,
        }).execute()
    except Exception as exc:
        # A concurrent import may have inserted the same key; update only because
        # the user explicitly selected the incoming mapping in the conflict UI.
        try:
            inserted = db.table("sku_mappings").update({"new_sku": value}).eq("normalized_old_sku", key).execute()
        except Exception:
            raise exc
    if not inserted.data:
        raise LookupError("Could not save this mapping. Refresh the page and try again.")
    return inserted.data[0]


def status() -> dict:
    rows = list_mappings()
    latest = max((row.get("updated_at") or row.get("created_at") or "" for row in rows), default=None)
    return {"configured": True, "total": len(rows), "last_updated": latest}
