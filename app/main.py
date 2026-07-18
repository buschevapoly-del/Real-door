"""RealDoor: a LIHTC applicant-assistant web app.

Three modules, one FastAPI app, no build step:
  1. Profile     -- upload a document, see extracted fields with page/bbox
                     and confidence, confirm or correct them.
  2. Understanding -- annualized income vs. the frozen 60% AMI threshold,
                     readiness status with reasons, and a rule Q&A box.
  3. Preparation  -- checklist of required vs. present documents, and an
                     explicit view/download/delete-only package export.

This app never decides eligibility and never sends a package anywhere on
its own (governance/DATA_USE_AND_SAFETY.md).
"""
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import storage
from app.checklist import checklist_status
from app.config import DATA_DIR, EVENT_DATE, DOCUMENT_CURRENCY_WINDOW_DAYS
from app.extraction import extract_document, FIELD_MAP, _parse_value
from app.income import build_submission
from app.qa import answer_question
from app.safety import DISCLAIMER_60_DAY_CONVENTION, DISCLAIMER_NO_DECISION
from app.schema_validate import validate_submission

FIELD_LABELS = {
    "person_name": "Person name",
    "household_size": "Household size",
    "address": "Mailing address",
    "application_date": "Application date",
    "pay_date": "Pay date",
    "pay_period_start": "Pay period start",
    "pay_period_end": "Pay period end",
    "pay_frequency": "Pay frequency",
    "regular_hours": "Regular hours",
    "hourly_rate": "Hourly rate",
    "gross_pay": "Gross pay",
    "net_pay": "Net pay",
    "document_date": "Document date",
    "weekly_hours": "Hours per week",
    "monthly_benefit": "Monthly benefit amount",
    "benefit_frequency": "Benefit frequency",
    "statement_month": "Statement month",
    "gross_receipts": "Gross receipts",
    "platform_fees": "Platform fees",
}

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="RealDoor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

EXPORTS_DIR = DATA_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)


def _submission_for(household: dict):
    if not household["household_size"]:
        return None
    documents = [d for d in household["documents"].values() if d["confirmed"]]
    if not documents:
        return None
    return build_submission(household["household_id"], household["household_size"], documents)


def _render_household(request: Request, household_id: str, **extra):
    household = storage.get_household(household_id)
    doc_types_present = [d["document_type"] for d in household["documents"].values() if d["document_type"]]
    checklist = checklist_status(household_id, doc_types_present) if household["household_size"] else None
    submission = _submission_for(household)
    context = {
        "request": request,
        "household": household,
        "checklist": checklist,
        "submission": submission,
        "event_date": EVENT_DATE,
        "currency_window_days": DOCUMENT_CURRENCY_WINDOW_DAYS,
        "no_decision_disclaimer": DISCLAIMER_NO_DECISION,
        "sixty_day_disclaimer": DISCLAIMER_60_DAY_CONVENTION,
        "all_households": storage.list_households(),
        "field_map": FIELD_MAP,
        "field_labels": FIELD_LABELS,
        "document_types": list(FIELD_MAP.keys()) + ["gig_income_corroboration"],
        **extra,
    }
    return templates.TemplateResponse(request, "household.html", context)


@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse(url="/household/HH-NEW-1")


@app.get("/household/{household_id}", response_class=HTMLResponse)
def household_page(request: Request, household_id: str):
    return _render_household(request, household_id)


@app.post("/household/{household_id}/size")
def set_size(household_id: str, household_size: int = Form(...)):
    storage.set_household_size(household_id, household_size)
    return RedirectResponse(url=f"/household/{household_id}", status_code=303)


@app.post("/household/{household_id}/documents")
async def upload_document(household_id: str, file: UploadFile, document_type_hint: str = Form("")):
    document_id = f"{household_id}-{uuid.uuid4().hex[:8].upper()}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        extraction = extract_document(tmp_path, filename=file.filename)
    finally:
        tmp_path.unlink(missing_ok=True)
    doc_type = extraction.document_type or document_type_hint or None
    storage.add_document(household_id, document_id, doc_type, file.filename, extraction)
    return RedirectResponse(url=f"/household/{household_id}", status_code=303)


@app.post("/household/{household_id}/documents/{document_id}/type")
def set_doc_type(household_id: str, document_id: str, document_type: str = Form(...)):
    storage.set_document_type(household_id, document_id, document_type)
    return RedirectResponse(url=f"/household/{household_id}", status_code=303)


@app.post("/household/{household_id}/documents/{document_id}/save")
async def save_document(request: Request, household_id: str, document_id: str):
    form = await request.form()
    if form.get("document_type"):
        storage.set_document_type(household_id, document_id, form["document_type"])
    household = storage.get_household(household_id)
    doc = household["documents"][document_id]
    doc_type = doc["document_type"]
    known_fields = FIELD_MAP.get(doc_type, {}).values()
    for field_name, kind in known_fields:
        submitted = form.get(field_name)
        if submitted is None or submitted == "":
            continue
        existing = doc["fields"].get(field_name, {})
        parsed = _parse_value(submitted, kind)
        if existing.get("value") != parsed:
            storage.update_field(household_id, document_id, field_name, parsed, bbox=existing.get("bbox"))
    if form.get("confirm") == "1":
        storage.confirm_document(household_id, document_id)
    return RedirectResponse(url=f"/household/{household_id}", status_code=303)


@app.post("/household/{household_id}/documents/{document_id}/delete")
def delete_document(household_id: str, document_id: str):
    storage.delete_document(household_id, document_id)
    return RedirectResponse(url=f"/household/{household_id}", status_code=303)


@app.post("/household/{household_id}/qa", response_class=HTMLResponse)
def ask_question(request: Request, household_id: str, question: str = Form(...)):
    qa_answer = answer_question(question)
    return _render_household(request, household_id, qa_question=question, qa_answer=qa_answer)


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
    }
    return payload


@app.get("/household/{household_id}/submission.json")
def submission_json(household_id: str):
    payload = _submission_json(household_id)
    errors = validate_submission(payload) if "error" not in payload else []
    return JSONResponse({"submission": payload, "schema_errors": errors})


@app.get("/household/{household_id}/export")
def export_package(household_id: str):
    household = storage.get_household(household_id)
    submission = _submission_json(household_id)
    package = {"profile": household, "submission": submission}
    out_path = EXPORTS_DIR / f"{household_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(package, f, indent=2)
    return FileResponse(out_path, filename=f"{household_id}_package.json", media_type="application/json")


@app.post("/household/{household_id}/delete_package")
def delete_package(household_id: str):
    storage.delete_household(household_id)
    export_path = EXPORTS_DIR / f"{household_id}.json"
    export_path.unlink(missing_ok=True)
    return RedirectResponse(url="/household/HH-NEW-1", status_code=303)
