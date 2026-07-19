"""RealDoor: a LIHTC applicant-assistant web app.

One screen at a time, four screens total, no build step:
  0. Welcome  -- what this is, what happens next, and a case-number entry.
  1. Upload your documents -- itself a 3-step Upload -> Review -> Confirm
                     flow: upload documents, see what was extracted in
                     plain language, correct and confirm before anything
                     is used elsewhere.
  2. Your income summary -- annualized income vs. the frozen 60% AMI
                     threshold, plain-language readiness status with
                     reasons, and a rule Q&A box.
  3. Your application packet -- checklist of required vs. present
                     documents, and an explicit view/download/delete-only
                     package export.

This app never decides eligibility and never sends a package anywhere on
its own (governance/DATA_USE_AND_SAFETY.md).
"""
import json
import re
import shutil
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import storage
from app.checklist import checklist_status
from app.config import DATA_DIR, EVENT_DATE, DOCUMENT_CURRENCY_WINDOW_DAYS, RULE_CORPUS_VERSION
from app.extraction import extract_document, draw_highlight, FIELD_MAP, _parse_value
from app.income import build_submission
from app.labels import (
    FIELD_LABELS,
    DOCUMENT_TYPE_LABELS,
    COMPARISON_LABELS,
    READINESS_LABELS,
    confidence_state,
    reason_code_label,
    activity_label,
    format_timestamp,
    format_money,
    format_hourly_rate,
    doc_preview_label,
)
from app.qa import answer_question
from app.safety import DISCLAIMER_60_DAY_CONVENTION, DISCLAIMER_NO_DECISION
from app.schema_validate import validate_submission

FIELD_PURPOSES = {
    "application_summary": {
        "person_name": "Shown for your own review; not used in the income calculation.",
        "household_size": (
            "Shown for your own review only. The household size actually used in the "
            "calculation is the value you enter separately, not this extracted field."
        ),
        "address": "Shown for your own review; not used in the income calculation.",
        "application_date": "Used to check this document's 60-day currency window.",
    },
    "pay_stub": {
        "person_name": "Shown for your own review; not used in the income calculation.",
        "pay_date": "Used to pick the most recent pay stub and check its 60-day currency window.",
        "pay_period_start": "Shown for your own review; not used in the income calculation.",
        "pay_period_end": "Shown for your own review; not used in the income calculation.",
        "pay_frequency": "Used to annualize gross pay (e.g. weekly x 52 pay periods/year).",
        "regular_hours": "Used only to check gross pay reconciles (hours x rate); flags a conflict if it doesn't.",
        "hourly_rate": "Used only to check gross pay reconciles (hours x rate); flags a conflict if it doesn't.",
        "gross_pay": "Annualized using the pay frequency to become part of total income.",
        "net_pay": "Shown for your own review; not used in the income calculation.",
    },
    "employment_letter": {
        "person_name": "Shown for your own review; not used in the income calculation.",
        "document_date": "Used to check this document's 60-day currency window.",
        "weekly_hours": (
            "Shown for your own review; not used in the income calculation. This document "
            "type is used only as documentary corroboration of employment."
        ),
        "hourly_rate": (
            "Shown for your own review; not used in the income calculation. This document "
            "type is used only as documentary corroboration of employment."
        ),
    },
    "benefit_letter": {
        "person_name": "Shown for your own review; not used in the income calculation.",
        "document_date": "Used to check this document's 60-day currency window.",
        "monthly_benefit": "Annualized using the benefit frequency to become part of total income.",
        "benefit_frequency": "Used to annualize the monthly benefit amount.",
    },
    "gig_statement": {
        "person_name": "Shown for your own review; not used in the income calculation.",
        "statement_month": "Shown for reference; this document type has no 60-day currency check today.",
        "gross_receipts": "Annualized (treated as a monthly figure x 12) to become part of total income.",
        "platform_fees": "Shown for your own review; not subtracted from gross receipts in the current calculation.",
    },
}

FLASH_MESSAGES = {
    "size_saved": "Household size saved.",
    "doc_deleted": "Document deleted.",
    "confirmed": "Documents confirmed.",
}

DOCUMENT_TYPES = list(FIELD_MAP.keys()) + ["gig_income_corroboration"]

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="RealDoor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(
    event_date=EVENT_DATE,
    currency_window_days=DOCUMENT_CURRENCY_WINDOW_DAYS,
    no_decision_disclaimer=DISCLAIMER_NO_DECISION,
    sixty_day_disclaimer=DISCLAIMER_60_DAY_CONVENTION,
    field_labels=FIELD_LABELS,
    document_type_labels=DOCUMENT_TYPE_LABELS,
    comparison_labels=COMPARISON_LABELS,
    readiness_labels=READINESS_LABELS,
    reason_code_label=reason_code_label,
    activity_label=activity_label,
    format_timestamp=format_timestamp,
    format_money=format_money,
    format_hourly_rate=format_hourly_rate,
    doc_preview_label=doc_preview_label,
)

EXPORTS_DIR = DATA_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)


def _unconfirmed(household: dict) -> list:
    return [d for d in household["documents"].values() if not d["confirmed"]]


def _confirmed(household: dict) -> list:
    return [d for d in household["documents"].values() if d["confirmed"]]


def _label_income_lines(income_lines: list) -> list:
    """Replace raw document IDs with a plain type label for the primary
    view (PRD Module 2, section 3.1) -- numbered only when a household has
    more than one document of the same type."""
    type_counts = Counter(line.document_type for line in income_lines)
    seen = Counter()
    labeled = []
    for line in income_lines:
        seen[line.document_type] += 1
        base_label = DOCUMENT_TYPE_LABELS.get(line.document_type, line.document_type.replace("_", " ").title())
        display_label = f"{base_label} {seen[line.document_type]}" if type_counts[line.document_type] > 1 else base_label
        labeled.append({"display_label": display_label, "formula": line.formula, "annual_amount": line.annual_amount})
    return labeled


def _expired_document_types(submission) -> set:
    if submission is None:
        return set()
    expired = set()
    for reason in submission.review_reasons:
        match = re.match(r"(.+)_EXPIRED$", reason)
        if match:
            expired.add(match.group(1).lower())
    return expired


def _submission_for(household: dict):
    if not household["household_size"]:
        return None
    documents = _confirmed(household)
    if not documents:
        return None
    return build_submission(household["household_id"], household["household_size"], documents)


def _common_context(request: Request, household_id: str, **extra) -> dict:
    household = storage.get_household(household_id)
    flash_message = FLASH_MESSAGES.get(request.query_params.get("flash"))
    return {
        "request": request,
        "household": household,
        "all_households": storage.list_households(),
        "flash_message": flash_message,
        **extra,
    }


def _render_profile_screen(request: Request, household_id: str, template_name: str, **extra):
    household = storage.get_household(household_id)
    context = _common_context(
        request,
        household_id,
        stage=1,
        unconfirmed_documents=_unconfirmed(household),
        confirmed_documents=_confirmed(household),
        confirmed_count=len(_confirmed(household)),
        document_types=DOCUMENT_TYPES,
        field_map=FIELD_MAP,
        confidence_state=confidence_state,
        **extra,
    )
    return templates.TemplateResponse(request, template_name, context)


def _render_summary(request: Request, household_id: str, **extra):
    household = storage.get_household(household_id)
    submission = _submission_for(household)
    context = _common_context(
        request,
        household_id,
        stage=2,
        submission=submission,
        income_lines_display=_label_income_lines(submission.income_lines) if submission else [],
        **extra,
    )
    return templates.TemplateResponse(request, "summary.html", context)


def _render_packet(request: Request, household_id: str, **extra):
    household = storage.get_household(household_id)
    doc_types_present = [d["document_type"] for d in household["documents"].values() if d["document_type"]]
    checklist = checklist_status(household_id, doc_types_present) if household["household_size"] else None
    submission = _submission_for(household)
    context = _common_context(
        request,
        household_id,
        stage=3,
        checklist=checklist,
        expired_document_types=_expired_document_types(submission),
        confirmed_documents=_confirmed(household),
        field_map=FIELD_MAP,
        **extra,
    )
    return templates.TemplateResponse(request, "packet.html", context)


@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse(url="/welcome")


@app.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    return templates.TemplateResponse(
        request, "welcome.html", {"request": request, "all_households": storage.list_households()}
    )


@app.get("/household/{household_id}", response_class=HTMLResponse)
def household_page(household_id: str):
    """Not a screen of its own -- dispatches a case to wherever it should
    pick up: a returning case with confirmed documents goes straight to its
    income summary, everyone else starts at Upload."""
    household = storage.get_household(household_id)
    if household["household_size"] and _confirmed(household):
        return RedirectResponse(url=f"/household/{household_id}/summary")
    return RedirectResponse(url=f"/household/{household_id}/profile")


@app.post("/household/{household_id}/size")
def set_size(household_id: str, household_size: int = Form(...)):
    storage.set_household_size(household_id, household_size)
    return RedirectResponse(url=f"/household/{household_id}/profile?flash=size_saved", status_code=303)


# ---------------------------------------------------------------------------
# Screen 1 -- Upload your documents: itself Upload -> Review -> Confirm.
# Nothing here is used by Screen 2 until Confirm's confirmation happens
# (enforced by _submission_for/_confirmed, not just hidden by the UI).
# ---------------------------------------------------------------------------


@app.get("/household/{household_id}/profile", response_class=HTMLResponse)
def profile_upload(request: Request, household_id: str):
    return _render_profile_screen(request, household_id, "profile_upload.html")


@app.post("/household/{household_id}/profile/upload")
async def profile_upload_post(
    request: Request,
    household_id: str,
    files: list[UploadFile],
    consent: str = Form(""),
):
    household = storage.get_household(household_id)
    if not household["consent_given"] and consent != "1":
        return _render_profile_screen(
            request,
            household_id,
            "profile_upload.html",
            step=1,
            consent_error="Please check the consent box before uploading a document.",
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

    return RedirectResponse(url=f"/household/{household_id}/profile", status_code=303)


@app.get("/household/{household_id}/profile/review", response_class=HTMLResponse)
def profile_review(request: Request, household_id: str):
    return _render_profile_screen(request, household_id, "profile_review.html")


@app.get("/household/{household_id}/profile/confirm", response_class=HTMLResponse)
def profile_confirm(request: Request, household_id: str):
    return _render_profile_screen(request, household_id, "profile_confirm.html")


@app.post("/household/{household_id}/profile/confirm")
async def profile_confirm_post(request: Request, household_id: str):
    form = await request.form()
    household = storage.get_household(household_id)
    incomplete_documents = {}
    for doc_id in list(household["documents"].keys()):
        doc = household["documents"][doc_id]
        if doc["confirmed"]:
            continue
        doc_type = form.get(f"document_type__{doc_id}")
        if doc_type:
            storage.set_document_type(household_id, doc_id, doc_type)
            doc_type_for_fields = doc_type
        else:
            doc_type_for_fields = doc["document_type"]
        required_fields = list(FIELD_MAP.get(doc_type_for_fields, {}).values())
        for field_name, kind in required_fields:
            submitted = form.get(f"{field_name}__{doc_id}")
            if submitted is None or submitted == "":
                continue
            existing = doc["fields"].get(field_name, {})
            parsed = _parse_value(submitted, kind)
            if existing.get("value") != parsed:
                storage.update_field(household_id, doc_id, field_name, parsed, bbox=existing.get("bbox"))
        # Re-fetch after any field updates above to check what's actually on
        # file now -- a document (especially one needing manual entry) must
        # never be marked confirmed while a required field is still empty.
        doc = storage.get_household(household_id)["documents"][doc_id]
        missing = [
            field_name for field_name, kind in required_fields
            if doc["fields"].get(field_name, {}).get("value") in (None, "")
        ]
        if missing:
            incomplete_documents[doc_id] = missing
            continue
        storage.confirm_document(household_id, doc_id)
    if incomplete_documents:
        return _render_profile_screen(
            request,
            household_id,
            "profile_confirm.html",
            confirm_error="Please fill in every field before confirming -- one or more documents below still need a value.",
            incomplete_documents=incomplete_documents,
        )
    return RedirectResponse(url=f"/household/{household_id}/summary?flash=confirmed", status_code=303)


@app.get("/household/{household_id}/summary", response_class=HTMLResponse)
def summary_screen(request: Request, household_id: str):
    household = storage.get_household(household_id)
    if not _confirmed(household):
        return RedirectResponse(url=f"/household/{household_id}/profile")
    return _render_summary(request, household_id)


@app.get("/household/{household_id}/packet", response_class=HTMLResponse)
def packet_screen(request: Request, household_id: str):
    household = storage.get_household(household_id)
    if not _confirmed(household):
        return RedirectResponse(url=f"/household/{household_id}/profile")
    return _render_packet(request, household_id)


@app.get("/household/{household_id}/documents/{document_id}/image")
def document_image(household_id: str, document_id: str):
    image = storage.get_document_image(household_id, document_id)
    if image is None:
        return Response(status_code=404)
    return Response(content=image, media_type="image/png")


@app.get("/household/{household_id}/documents/{document_id}/fields/{field_name}/source")
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


@app.post("/household/{household_id}/documents/{document_id}/delete")
def delete_document(household_id: str, document_id: str, redirect_to: str = Form("")):
    storage.delete_document(household_id, document_id)
    target = redirect_to or f"/household/{household_id}/profile"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{separator}flash=doc_deleted", status_code=303)


@app.post("/household/{household_id}/qa", response_class=HTMLResponse)
def ask_question(request: Request, household_id: str, question: str = Form(...)):
    qa_answer = answer_question(question)
    return _render_summary(request, household_id, qa_question=question, qa_answer=qa_answer)


def _submission_json(household_id: str) -> dict:
    household = storage.get_household(household_id)
    result = _submission_for(household)
    if result is None:
        return {"error": "Household size and at least one confirmed document are required."}
    payload = {
        "household_id": result.household_id,
        "annualized_income": result.annualized_income,
        "comparison": result.comparison,
        "readiness_status": result.readiness_status,
        "citations": result.citations,
        "review_reasons": result.review_reasons,
        "rule_corpus_version": result.rule_corpus_version,
        "income_lines": [
            {
                "document_id": line.document_id,
                "document_type": line.document_type,
                "amount": line.amount,
                "frequency": line.frequency,
                "periods_per_year": line.periods_per_year,
                "annual_amount": line.annual_amount,
                "formula": line.formula,
            }
            for line in result.income_lines
        ],
    }
    return payload


@app.get("/household/{household_id}/submission.json")
def submission_json(household_id: str):
    payload = _submission_json(household_id)
    errors = validate_submission(payload) if "error" not in payload else []
    return JSONResponse({"submission": payload, "schema_errors": errors})


def _packet_text(household_id: str) -> str:
    """The renter-facing download: plain language, formatted amounts, no
    bbox/rule_id/rule_corpus_version or other audit-only technical detail.
    That detail stays reachable separately via the technical export."""
    household = storage.get_household(household_id)
    submission = _submission_for(household)
    doc_types_present = [d["document_type"] for d in household["documents"].values() if d["document_type"]]
    checklist = checklist_status(household_id, doc_types_present) if household["household_size"] else None
    expired_types = _expired_document_types(submission)

    lines = [
        "REALDOOR -- YOUR APPLICATION PACKET",
        f"Household: {household_id}",
        f"Prepared: {format_timestamp(datetime.now(timezone.utc).isoformat())}",
        "",
        DISCLAIMER_NO_DECISION,
        "",
    ]

    if submission:
        lines.append("INCOME SUMMARY")
        lines.append(f"  Annualized income: {format_money(submission.annualized_income)} a year")
        if submission.threshold:
            lines.append(
                f"  Income limit for a household of {submission.threshold['household_size']}: "
                f"{format_money(submission.threshold['income_limit_60_percent'])} a year"
            )
        lines.append(f"  {COMPARISON_LABELS.get(submission.comparison, submission.comparison)}.")
        lines.append(f"  Status: {READINESS_LABELS.get(submission.readiness_status, submission.readiness_status)}")
        for reason in submission.review_reasons:
            lines.append(f"    - {reason_code_label(reason)}")
        lines.append("")

    if checklist:
        lines.append("REQUIRED DOCUMENTS")
        for dt in checklist["required"]:
            label = DOCUMENT_TYPE_LABELS.get(dt, dt)
            if dt in checklist["missing"]:
                status = "Missing -- we don't have this yet"
            elif dt in expired_types:
                status = "Expired -- older than 60 days, needs a newer copy"
            else:
                status = "Present"
            lines.append(f"  {label}: {status}")
        for dt in checklist["extra"]:
            lines.append(f"  {DOCUMENT_TYPE_LABELS.get(dt, dt)}: Extra (not on required list)")
        lines.append("")

    confirmed_documents = _confirmed(household)
    if confirmed_documents:
        lines.append("DOCUMENTS INCLUDED")
        for doc in confirmed_documents:
            lines.append(f"  {doc_preview_label(doc)}")
            for label, (field_name, kind) in FIELD_MAP.get(doc["document_type"], {}).items():
                record = doc["fields"].get(field_name)
                if not record or record.get("value") in (None, ""):
                    continue
                value = record["value"]
                if field_name == "hourly_rate":
                    display_value = format_hourly_rate(value)
                elif kind == "money":
                    display_value = format_money(value)
                else:
                    display_value = value
                lines.append(f"    {FIELD_LABELS.get(field_name, field_name)}: {display_value}")
            lines.append("")

    lines.append(
        "This package is only ever viewed, downloaded, or deleted by you. "
        "It is never submitted automatically."
    )
    return "\n".join(lines)


@app.get("/household/{household_id}/export")
def export_package(household_id: str):
    storage.log_activity(household_id, "package_exported")
    text = _packet_text(household_id)
    out_path = EXPORTS_DIR / f"{household_id}_packet.txt"
    out_path.write_text(text, encoding="utf-8")
    return FileResponse(
        out_path, filename=f"{household_id}_application_packet.txt", media_type="text/plain"
    )


@app.get("/household/{household_id}/export/technical")
def export_package_technical(household_id: str):
    """The full internal record (bbox, confidence, activity log, raw
    submission) -- for judges/verification against submission.schema.json,
    never the button a renter is pointed at."""
    storage.log_activity(household_id, "technical_export_downloaded")
    household = storage.get_household(household_id)
    submission = _submission_json(household_id)
    package = {"profile": household, "submission": submission}
    out_path = EXPORTS_DIR / f"{household_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(package, f, indent=2)
    return FileResponse(
        out_path, filename=f"{household_id}_technical_export.json", media_type="application/json"
    )


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works(request: Request):
    return templates.TemplateResponse(
        request,
        "how_it_works.html",
        {
            "request": request,
            "field_map": FIELD_MAP,
            "field_labels": FIELD_LABELS,
            "field_purposes": FIELD_PURPOSES,
            "rule_corpus_version": RULE_CORPUS_VERSION,
        },
    )


@app.post("/household/{household_id}/delete_package")
def delete_package(household_id: str):
    storage.delete_household(household_id)
    export_path = EXPORTS_DIR / f"{household_id}.json"
    export_path.unlink(missing_ok=True)
    return RedirectResponse(url="/welcome", status_code=303)
