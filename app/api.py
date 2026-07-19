"""JSON API for RealDoor.

A thin layer over the exact same business logic the server-rendered HTML
screens use (app/main.py) -- built so a separately-hosted frontend (e.g.
a Lovable-built React app, calling this API cross-origin) can drive the
same Welcome -> Upload -> Review -> Confirm -> Summary -> Packet flow.

No calculation, safety, consent, or completeness logic lives here. Every
endpoint below calls straight into the same storage/extraction/income/
checklist/labels functions app/main.py's HTML routes call -- this module
only reshapes their results as JSON. That's why the imports below come
from app.main itself (see the bottom of app/main.py, which imports this
module last so those names already exist by the time this file loads).

Nothing here changes what's stored, logged, or calculated -- only how a
remote frontend can read and drive it.
"""
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from app import storage
from app.checklist import checklist_status
from app.config import DATA_DIR, DOCUMENT_CURRENCY_WINDOW_DAYS, EVENT_DATE, RULE_CORPUS_VERSION
from app.extraction import FIELD_MAP, draw_highlight, extract_document
from app.labels import (
    COMPARISON_LABELS,
    DOCUMENT_TYPE_LABELS,
    FIELD_LABELS,
    READINESS_LABELS,
    activity_label,
    confidence_state,
    format_timestamp,
)
from app.qa import answer_question
from app.safety import DISCLAIMER_60_DAY_CONVENTION, DISCLAIMER_NO_DECISION
from app.schema_validate import validate_submission

router = APIRouter(prefix="/api", tags=["api"])

EXPORTS_DIR = DATA_DIR / "exports"


# ---------------------------------------------------------------------------
# Serialization helpers -- turn internal dicts/dataclasses into the JSON
# shapes a frontend actually wants (labels alongside codes, confidence
# states precomputed, no bbox/rule internals unless explicitly asked for).
# ---------------------------------------------------------------------------


def _document_json(doc: dict) -> dict:
    fields = {}
    for field_name, record in doc["fields"].items():
        fields[field_name] = {
            "value": record.get("value"),
            "confidence": record.get("confidence"),
            "source": record.get("source"),
            "page": record.get("page"),
            "state": confidence_state(record),
        }
    return {
        "document_id": doc["document_id"],
        "document_type": doc["document_type"],
        "document_type_label": DOCUMENT_TYPE_LABELS.get(doc["document_type"], doc["document_type"]),
        "file_name": doc["file_name"],
        "confirmed": doc["confirmed"],
        "needs_manual_entry": doc["needs_manual_entry"],
        "contains_adversarial_text": doc["contains_adversarial_text"],
        "untrusted_instruction_text": doc["untrusted_instruction_text"],
        "has_image": doc.get("has_image", False),
        "fields": fields,
    }


def _household_json(household_id: str) -> dict:
    from app import main as m

    household = storage.get_household(household_id)
    return {
        "household_id": household["household_id"],
        "household_size": household["household_size"],
        "consent_given": household["consent_given"],
        "has_application_summary": m._has_application_summary(household),
        "show_standalone_household_size": not m._has_application_summary(household),
        "documents": {doc_id: _document_json(doc) for doc_id, doc in household["documents"].items()},
    }


def _summary_json(household_id: str) -> dict:
    from app import main as m

    household = storage.get_household(household_id)
    submission = m._submission_for(household)
    if submission is None:
        return {"ready": False}
    return {
        "ready": True,
        "annualized_income": submission.annualized_income,
        "threshold": submission.threshold,
        "comparison": submission.comparison,
        "comparison_label": COMPARISON_LABELS.get(submission.comparison, submission.comparison),
        "readiness_status": submission.readiness_status,
        "readiness_label": READINESS_LABELS.get(submission.readiness_status, submission.readiness_status),
        "review_reasons": [
            {"code": code, "display_text": m.reason_display_text(code, household)}
            for code in submission.review_reasons
        ],
        "income_lines": m._label_income_lines(submission.income_lines),
        "rule_corpus_version": submission.rule_corpus_version,
        "citations": submission.citations,
    }


def _checklist_json(household_id: str) -> dict:
    from app import main as m

    household = storage.get_household(household_id)
    if not household["household_size"]:
        return {"ready": False}
    completed_documents = m._completed_documents(household)
    doc_types_present = [d["document_type"] for d in completed_documents]
    checklist = checklist_status(household_id, doc_types_present)
    submission = m._submission_for(household)
    expired_types = m._expired_document_types(submission)

    def _status(dt: str) -> dict:
        if dt in checklist["missing"]:
            return {"status": "missing", "reason": "We don't have this yet."}
        if dt in expired_types:
            return {"status": "expired", "reason": "This is older than 60 days and needs a newer copy."}
        return {"status": "present", "reason": None}

    return {
        "ready": True,
        "required": [
            {"document_type": dt, "label": DOCUMENT_TYPE_LABELS.get(dt, dt), **_status(dt)}
            for dt in checklist["required"]
        ],
        "extra": [
            {"document_type": dt, "label": DOCUMENT_TYPE_LABELS.get(dt, dt)} for dt in checklist["extra"]
        ],
    }


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@router.get("/meta")
def get_meta():
    return {
        "event_date": EVENT_DATE,
        "currency_window_days": DOCUMENT_CURRENCY_WINDOW_DAYS,
        "rule_corpus_version": RULE_CORPUS_VERSION,
        "no_decision_disclaimer": DISCLAIMER_NO_DECISION,
        "sixty_day_disclaimer": DISCLAIMER_60_DAY_CONVENTION,
        "document_types": list(FIELD_MAP.keys()) + ["gig_income_corroboration"],
        "document_type_labels": DOCUMENT_TYPE_LABELS,
        "field_labels": FIELD_LABELS,
        "field_map": {
            doc_type: {field_name: kind for _, (field_name, kind) in fields.items()}
            for doc_type, fields in FIELD_MAP.items()
        },
    }


@router.get("/households")
def list_households():
    return {"household_ids": storage.list_households()}


# ---------------------------------------------------------------------------
# Household + documents (Upload / Review screens)
# ---------------------------------------------------------------------------


@router.get("/household/{household_id}")
def get_household(household_id: str):
    return _household_json(household_id)


@router.post("/household/{household_id}/documents")
async def upload_documents(household_id: str, files: list[UploadFile], consent: str = Form("")):
    household = storage.get_household(household_id)
    if not household["consent_given"] and consent != "1":
        return JSONResponse(
            {"ok": False, "error": "Please check the consent box before uploading a document."},
            status_code=400,
        )
    if consent == "1":
        storage.give_consent(household_id)

    for file in files:
        if not file.filename:
            continue
        document_id = f"{household_id}-{uuid.uuid4().hex[:8].upper()}"
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = Path(tmp.name)
        try:
            extraction = extract_document(tmp_path, filename=file.filename)
        finally:
            tmp_path.unlink(missing_ok=True)
        storage.add_document(household_id, document_id, extraction.document_type, file.filename, extraction)

    return {"ok": True, "household": _household_json(household_id)}


@router.delete("/household/{household_id}/documents/{document_id}")
def delete_document(household_id: str, document_id: str):
    storage.delete_document(household_id, document_id)
    return {"ok": True}


@router.get("/household/{household_id}/documents/{document_id}/image")
def document_image(household_id: str, document_id: str):
    image = storage.get_document_image(household_id, document_id)
    if image is None:
        return Response(status_code=404)
    return Response(content=image, media_type="image/png")


@router.get("/household/{household_id}/documents/{document_id}/fields/{field_name}/source")
def field_source_image(household_id: str, document_id: str, field_name: str):
    household = storage.get_household(household_id)
    doc = household["documents"].get(document_id)
    image = storage.get_document_image(household_id, document_id)
    if image is None or doc is None:
        return Response(status_code=404)
    field_record = doc["fields"].get(field_name)
    if not field_record or not field_record.get("bbox"):
        return Response(content=image, media_type="image/png")
    highlighted = draw_highlight(image, field_record["bbox"])
    return Response(content=highlighted, media_type="image/png")


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


@router.post("/household/{household_id}/confirm")
async def confirm(household_id: str, body: dict):
    """Body: {"household_size": "4", "documents": {doc_id: {"document_type": str|None, "fields": {name: value}}}}"""
    from app import main as m

    household_size_input = str(body.get("household_size", "") or "")
    document_updates = body.get("documents", {}) or {}
    result = m._apply_confirm(household_id, household_size_input, document_updates)
    if not result["ok"]:
        return JSONResponse(result, status_code=422)
    return {"ok": True, "household": _household_json(household_id)}


# ---------------------------------------------------------------------------
# Summary / checklist / packet (Summary + Packet screens)
# ---------------------------------------------------------------------------


@router.get("/household/{household_id}/summary")
def get_summary(household_id: str):
    return _summary_json(household_id)


@router.get("/household/{household_id}/checklist")
def get_checklist(household_id: str):
    return _checklist_json(household_id)


@router.get("/household/{household_id}/packet")
def get_packet(household_id: str):
    from app import main as m

    household = storage.get_household(household_id)
    completed_documents = m._completed_documents(household)
    submission = m._submission_for(household)
    expired_types = m._expired_document_types(submission)
    documents = []
    for doc in completed_documents:
        entry = _document_json(doc)
        entry["label"] = m.doc_preview_label(doc)
        entry["expired"] = doc["document_type"] in expired_types
        documents.append(entry)
    return {
        "checklist": _checklist_json(household_id),
        "documents": documents,
        "consent_given": household["consent_given"],
    }


@router.get("/household/{household_id}/export")
def export_packet_text(household_id: str):
    from app import main as m

    storage.log_activity(household_id, "package_exported")
    text = m._packet_text(household_id)
    out_path = EXPORTS_DIR / f"{household_id}_packet.txt"
    out_path.write_text(text, encoding="utf-8")
    return FileResponse(
        out_path, filename=f"{household_id}_application_packet.txt", media_type="text/plain"
    )


@router.get("/household/{household_id}/export/technical")
def export_technical_json(household_id: str):
    from app import main as m

    storage.log_activity(household_id, "technical_export_downloaded")
    household = storage.get_household(household_id)
    submission = m._submission_json(household_id)
    return {"profile": household, "submission": submission}


@router.get("/household/{household_id}/submission-validation")
def submission_validation(household_id: str):
    from app import main as m

    payload = m._submission_json(household_id)
    errors = validate_submission(payload) if "error" not in payload else []
    return {"submission": payload, "schema_errors": errors}


# ---------------------------------------------------------------------------
# Q&A / activity / delete
# ---------------------------------------------------------------------------


@router.post("/household/{household_id}/qa")
async def qa(household_id: str, body: dict):
    question = body.get("question", "")
    result = answer_question(question)
    return {"answer": result.answer, "citations": result.citations, "refused": result.refused}


@router.get("/household/{household_id}/activity")
def get_activity(household_id: str):
    household = storage.get_household(household_id)
    return {
        "entries": [
            {
                "timestamp": entry["timestamp"],
                "timestamp_display": format_timestamp(entry["timestamp"]),
                "action": entry["action"],
                "display_text": activity_label(entry),
            }
            for entry in household["activity_log"]
        ]
    }


@router.post("/household/{household_id}/delete")
def delete_package(household_id: str):
    storage.delete_household(household_id)
    export_path = EXPORTS_DIR / f"{household_id}.json"
    export_path.unlink(missing_ok=True)
    return {"ok": True}
