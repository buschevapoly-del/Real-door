"""Module 2: annualize confirmed income, compare to the frozen threshold,
and derive a readiness status -- never an eligibility decision.

Everything here operates on *user-confirmed* field values (Module 1's
output), not raw extraction. That keeps this module deterministic and
auditable: every number it produces traces back to a specific document,
page and box, or to a rule_id in the frozen corpus.

Each document is expected as:
    {
        "document_id": str,
        "document_type": str,
        "contains_adversarial_text": bool,
        "fields": {field_name: {"value": ..., "page": int, "bbox": [..]}},
    }
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.config import EVENT_DATE, DOCUMENT_CURRENCY_WINDOW_DAYS, RULE_CORPUS_PATH
from app.thresholds import lookup_threshold
from app.safety import strip_decision_language

from src.calculate import annualize, compare_to_threshold
from src.rules import load_rules

EVENT_DATE_OBJ = date.fromisoformat(EVENT_DATE)

# Which date field on each document type represents "as of" currency.
CURRENCY_DATE_FIELD = {
    "application_summary": "application_date",
    "pay_stub": "pay_date",
    "employment_letter": "document_date",
    "benefit_letter": "document_date",
}


@dataclass
class IncomeLine:
    document_id: str
    document_type: str
    source_field: str
    amount: float
    frequency: str
    annual_amount: float


@dataclass
class SubmissionResult:
    household_id: str
    annualized_income: float
    comparison: str
    readiness_status: str
    review_reasons: list = field(default_factory=list)
    income_lines: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    threshold: Optional[dict] = None


def _rules():
    return load_rules(RULE_CORPUS_PATH)


def _rule_citation(rules, rule_id: str) -> dict:
    r = rules[rule_id]
    return {
        "type": "rule",
        "rule_id": r["rule_id"],
        "text": r["text"],
        "source_url": r["source_url"],
        "effective_date": r["effective_date"],
    }


def _field_citation(document_id: str, field_name: str, field_record: dict) -> dict:
    return {
        "type": "field_source",
        "document_id": document_id,
        "field": field_name,
        "page": field_record.get("page", 1),
        "bbox": field_record.get("bbox", []),
    }


def _value(document: dict, field_name: str):
    return document["fields"][field_name]["value"]


def _is_reconciled_pay_stub(fields: dict) -> bool:
    try:
        expected = float(fields["regular_hours"]["value"]) * float(fields["hourly_rate"]["value"])
        return abs(float(fields["gross_pay"]["value"]) - expected) <= 0.01
    except (KeyError, TypeError, ValueError):
        return False


def _days_old(iso_date: str) -> Optional[int]:
    try:
        return (EVENT_DATE_OBJ - date.fromisoformat(iso_date)).days
    except (ValueError, TypeError):
        return None


REQUIRED_FIELDS = {
    "pay_stub": {"regular_hours", "hourly_rate", "gross_pay", "pay_date", "pay_frequency"},
    "benefit_letter": {"monthly_benefit", "benefit_frequency"},
    "gig_statement": {"gross_receipts"},
}


def _is_complete(document: dict) -> bool:
    required = REQUIRED_FIELDS.get(document["document_type"])
    if required is None:
        return True
    return required.issubset(document["fields"].keys())


def build_submission(household_id: str, household_size: int, documents: list) -> SubmissionResult:
    rules = _rules()
    review_reasons: list[str] = []
    citations: list[dict] = []
    income_lines: list[IncomeLine] = []

    incomplete = [d for d in documents if not _is_complete(d)]
    for d in incomplete:
        review_reasons.append(f"INCOMPLETE_{d['document_type'].upper()}_FIELDS")
    documents = [d for d in documents if _is_complete(d)]

    pay_stubs = [d for d in documents if d["document_type"] == "pay_stub"]
    if pay_stubs:
        reconciled = [d for d in pay_stubs if _is_reconciled_pay_stub(d["fields"])]
        conflict = len(reconciled) < len(pay_stubs)
        if reconciled:
            distinct_totals = {round(float(_value(d, "gross_pay")), 2) for d in reconciled}
            if len(distinct_totals) > 1:
                conflict = True
            chosen = max(reconciled, key=lambda d: _value(d, "pay_date"))
        else:
            chosen = max(pay_stubs, key=lambda d: _value(d, "pay_date"))
        if conflict:
            review_reasons.append("PAY_STUB_TOTAL_CONFLICT")
        gross = float(_value(chosen, "gross_pay"))
        freq = _value(chosen, "pay_frequency")
        annual = annualize(gross, freq)
        income_lines.append(IncomeLine(chosen["document_id"], "pay_stub", "gross_pay", gross, freq, annual))
        citations.append(_rule_citation(rules, "CH-INCOME-001"))
        citations.append(_field_citation(chosen["document_id"], "gross_pay", chosen["fields"]["gross_pay"]))

    for d in documents:
        if d["document_type"] == "benefit_letter":
            amount = float(_value(d, "monthly_benefit"))
            freq = _value(d, "benefit_frequency")
            annual = annualize(amount, freq)
            income_lines.append(IncomeLine(d["document_id"], "benefit_letter", "monthly_benefit", amount, freq, annual))
            citations.append(_field_citation(d["document_id"], "monthly_benefit", d["fields"]["monthly_benefit"]))
        elif d["document_type"] == "gig_statement":
            amount = float(_value(d, "gross_receipts"))
            annual = annualize(amount, "monthly")
            income_lines.append(IncomeLine(d["document_id"], "gig_statement", "gross_receipts", amount, "monthly", annual))
            citations.append(_field_citation(d["document_id"], "gross_receipts", d["fields"]["gross_receipts"]))
            has_corroboration = any(x["document_type"] == "gig_income_corroboration" for x in documents)
            if not has_corroboration:
                review_reasons.append("GIG_INCOME_UNCORROBORATED")

    annualized_income = round(sum(line.annual_amount for line in income_lines), 2)

    threshold_row = lookup_threshold(household_size)
    if threshold_row is None:
        comparison = "no_frozen_threshold"
        review_reasons.append("HOUSEHOLD_SIZE_OUTSIDE_FROZEN_TABLE")
        threshold_dict = None
    else:
        comparison = compare_to_threshold(annualized_income, threshold_row.income_limit_60_percent)
        threshold_dict = {
            "household_size": threshold_row.household_size,
            "income_limit_60_percent": threshold_row.income_limit_60_percent,
            "effective_date": threshold_row.effective_date,
            "source_url": threshold_row.source_url,
        }
        citations.append(_rule_citation(rules, "HUD-MTSP-002"))

    for d in documents:
        date_field = CURRENCY_DATE_FIELD.get(d["document_type"])
        if not date_field or date_field not in d["fields"]:
            continue
        age = _days_old(_value(d, date_field))
        if age is not None and age > DOCUMENT_CURRENCY_WINDOW_DAYS:
            review_reasons.append(f"{d['document_type'].upper()}_EXPIRED")

    if any(d.get("contains_adversarial_text") for d in documents):
        citations.append(_rule_citation(rules, "CH-SAFETY-001"))

    citations.append(_rule_citation(rules, "CH-READINESS-001"))
    citations.append(_rule_citation(rules, "CH-DECISION-001"))

    readiness_status = strip_decision_language(
        "NEEDS_REVIEW" if review_reasons else "READY_TO_REVIEW"
    )

    return SubmissionResult(
        household_id=household_id,
        annualized_income=annualized_income,
        comparison=comparison,
        readiness_status=readiness_status,
        review_reasons=sorted(set(review_reasons)),
        income_lines=income_lines,
        citations=citations,
        threshold=threshold_dict,
    )
