"""The final submission JSON must validate against
starter/schemas/submission.schema.json for every household fixture, and
schema violations (missing required keys, wrong enum value) must be caught."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.schema_validate import validate_submission

VALID_PAYLOAD = {
    "household_id": "HH-001",
    "annualized_income": 56316.0,
    "comparison": "below_or_equal",
    "readiness_status": "READY_TO_REVIEW",
    "citations": [{"type": "rule", "rule_id": "CH-INCOME-001"}],
}


class SchemaValidationTests(unittest.TestCase):
    def test_valid_payload_has_no_errors(self):
        self.assertEqual(validate_submission(VALID_PAYLOAD), [])

    def test_missing_required_field_is_rejected(self):
        payload = dict(VALID_PAYLOAD)
        del payload["citations"]
        self.assertTrue(validate_submission(payload))

    def test_decision_word_in_readiness_status_is_rejected(self):
        payload = dict(VALID_PAYLOAD)
        payload["readiness_status"] = "APPROVED"
        self.assertTrue(validate_submission(payload))

    def test_negative_income_is_rejected(self):
        payload = dict(VALID_PAYLOAD)
        payload["annualized_income"] = -1
        self.assertTrue(validate_submission(payload))


if __name__ == "__main__":
    unittest.main()
