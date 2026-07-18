import json

import jsonschema

from app.config import SUBMISSION_SCHEMA_PATH


def load_submission_schema() -> dict:
    with SUBMISSION_SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def validate_submission(payload: dict) -> list:
    """Returns a list of validation error messages (empty if valid)."""
    schema = load_submission_schema()
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(payload)]
