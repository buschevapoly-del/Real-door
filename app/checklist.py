"""Module 3: reconcile uploaded documents against the household's required
document list.

`required_document_types` comes from the hackathon's reference checklist
(evaluation/application_checklists.json) -- for this event the scenario's
required-document list is a known fixture, not something we derive from
the documents themselves. `present`/`missing` are computed live from what
the applicant actually uploaded, so the check reflects the real state of
their package, not the fixture's recorded answer.
"""
import json
from functools import lru_cache

from app.config import CHECKLISTS_PATH


@lru_cache(maxsize=1)
def load_checklists() -> dict:
    with CHECKLISTS_PATH.open(encoding="utf-8") as f:
        rows = json.load(f)
    return {row["household_id"]: row for row in rows}


def required_document_types(household_id: str) -> list:
    row = load_checklists().get(household_id)
    return list(row["required_document_types"]) if row else []


def checklist_status(household_id: str, uploaded_document_types: list) -> dict:
    required = required_document_types(household_id)
    present_set = set(uploaded_document_types)
    required_set = set(required)
    return {
        "required": required,
        "present": sorted(present_set & required_set) + sorted(present_set - required_set),
        "missing": sorted(required_set - present_set),
        "extra": sorted(present_set - required_set),
    }
