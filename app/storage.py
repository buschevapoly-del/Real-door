"""Local, per-household package storage.

No database, no network call, no automatic submission anywhere -- packages
are only ever viewed, edited, downloaded, or deleted by the applicant
themselves (governance/DATA_USE_AND_SAFETY.md, submission requirement 5).
Each household's data lives in its own JSON file so "delete" is a single
file removal with nothing left behind -- including its activity log, which
lives inside this same file rather than a separate store, so it never
outlives the package it describes.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR


def _path(household_id: str) -> Path:
    safe_id = "".join(c for c in household_id if c.isalnum() or c in "-_")
    return DATA_DIR / f"{safe_id}.json"


def _default_household(household_id: str) -> dict:
    return {
        "household_id": household_id,
        "household_size": None,
        "documents": {},
        "consent_given": False,
        "consent_given_at": None,
        "activity_log": [],
    }


def list_households() -> list:
    return sorted(p.stem for p in DATA_DIR.glob("*.json"))


def get_household(household_id: str) -> dict:
    path = _path(household_id)
    if not path.exists():
        return _default_household(household_id)
    with path.open(encoding="utf-8") as f:
        household = json.load(f)
    # Backfill households saved before consent/activity-log fields existed.
    for key, value in _default_household(household_id).items():
        household.setdefault(key, value)
    return household


def save_household(data: dict) -> None:
    path = _path(data["household_id"])
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def delete_household(household_id: str) -> bool:
    path = _path(household_id)
    if path.exists():
        path.unlink()
        return True
    return False


def _log(household: dict, action: str, detail: str = "") -> None:
    """Record that an action happened, never what data it involved -- no
    corrected values or document contents, only field/document identifiers."""
    household["activity_log"].append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "detail": detail,
        }
    )


def give_consent(household_id: str) -> dict:
    household = get_household(household_id)
    if not household["consent_given"]:
        household["consent_given"] = True
        household["consent_given_at"] = datetime.now(timezone.utc).isoformat()
        _log(household, "consent_given")
        save_household(household)
    return household


def log_activity(household_id: str, action: str, detail: str = "") -> dict:
    household = get_household(household_id)
    _log(household, action, detail)
    save_household(household)
    return household


def set_household_size(household_id: str, size: int) -> dict:
    household = get_household(household_id)
    household["household_size"] = size
    _log(household, "household_size_set")
    save_household(household)
    return household


def add_document(
    household_id: str,
    document_id: str,
    document_type: Optional[str],
    file_name: str,
    extraction,
) -> dict:
    household = get_household(household_id)
    fields = {}
    for f in extraction.fields:
        fields[f.field] = {
            "value": f.value,
            "page": f.page,
            "bbox": f.bbox,
            "confidence": f.confidence,
            "source": f.source,
        }
    household["documents"][document_id] = {
        "document_id": document_id,
        "document_type": document_type,
        "file_name": file_name,
        "page_size_points": extraction.page_size_points,
        "contains_adversarial_text": extraction.contains_adversarial_text,
        "untrusted_instruction_text": extraction.untrusted_instruction_text,
        "needs_manual_entry": extraction.needs_manual_entry,
        "notes": extraction.notes,
        "confirmed": False,
        "fields": fields,
    }
    _log(household, "document_uploaded", detail=f"{document_id} ({document_type or 'unknown type'})")
    save_household(household)
    return household


def update_field(
    household_id: str,
    document_id: str,
    field_name: str,
    value,
    page: int = 1,
    bbox: Optional[list] = None,
) -> dict:
    household = get_household(household_id)
    doc = household["documents"][document_id]
    doc["fields"][field_name] = {
        "value": value,
        "page": page,
        "bbox": bbox or doc["fields"].get(field_name, {}).get("bbox", []),
        "confidence": 1.0,
        "source": "manual",
    }
    _log(household, "field_corrected", detail=f"{document_id}.{field_name}")
    save_household(household)
    return household


def set_document_type(household_id: str, document_id: str, document_type: str) -> dict:
    household = get_household(household_id)
    household["documents"][document_id]["document_type"] = document_type
    save_household(household)
    return household


def confirm_document(household_id: str, document_id: str) -> dict:
    household = get_household(household_id)
    household["documents"][document_id]["confirmed"] = True
    _log(household, "document_confirmed", detail=document_id)
    save_household(household)
    return household


def delete_document(household_id: str, document_id: str) -> dict:
    household = get_household(household_id)
    household["documents"].pop(document_id, None)
    _log(household, "document_deleted", detail=document_id)
    save_household(household)
    return household
