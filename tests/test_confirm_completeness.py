"""Bug fix: a document needing manual entry (extraction returned zero
fields, e.g. a rasterized pay stub with no text layer) must never be
marked `confirmed` while its required fields are still empty. Before this
fix, clicking "Confirm and continue" with blank inputs silently confirmed
the document anyway, feeding an empty pay stub into the income
calculation and producing a confusing INCOMPLETE_PAY_STUB_FIELDS result.
"""
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from app.main import app
from app import storage
from app.config import DOCUMENTS_DIR

client = TestClient(app)

HH = "HH-CONFIRM-COMPLETENESS-TEST"

# Has no text layer -- extraction returns needs_manual_entry=True and an
# empty fields dict, so every pay_stub field must be typed in by hand.
MANUAL_ENTRY_PDF = "hh-001_d02_pay_stub.pdf"


class ConfirmCompletenessTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)
        pdf = DOCUMENTS_DIR / MANUAL_ENTRY_PDF
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        confirm_page = client.get(f"/household/{HH}/profile/confirm").text
        self.doc_id = re.findall(r'name="document_type__([^"]+)"', confirm_page)[0]

    def test_confirming_with_blank_fields_does_not_mark_document_confirmed(self):
        resp = client.post(
            f"/household/{HH}/profile/confirm",
            data={f"document_type__{self.doc_id}": "pay_stub"},
        )
        self.assertEqual(resp.status_code, 200)
        household = storage.get_household(HH)
        self.assertFalse(household["documents"][self.doc_id]["confirmed"])

    def test_confirming_with_blank_fields_shows_an_error_and_highlights_the_document(self):
        resp = client.post(
            f"/household/{HH}/profile/confirm",
            data={f"document_type__{self.doc_id}": "pay_stub"},
        )
        self.assertIn("Please fill in every field", resp.text)
        self.assertIn("field-missing", resp.text)
        self.assertIn('role="alert"', resp.text)

    def test_unconfirmed_document_still_blocks_income_summary(self):
        client.post(
            f"/household/{HH}/profile/confirm",
            data={f"document_type__{self.doc_id}": "pay_stub"},
        )
        payload = client.get(f"/household/{HH}/submission.json").json()["submission"]
        self.assertIn("error", payload)

    def test_confirming_with_all_fields_filled_succeeds(self):
        form = {
            f"document_type__{self.doc_id}": "pay_stub",
            f"person_name__{self.doc_id}": "Mara North",
            f"pay_date__{self.doc_id}": "2026-06-27",
            f"pay_period_start__{self.doc_id}": "2026-06-10",
            f"pay_period_end__{self.doc_id}": "2026-06-23",
            f"pay_frequency__{self.doc_id}": "biweekly",
            f"regular_hours__{self.doc_id}": "76",
            f"hourly_rate__{self.doc_id}": "28.5",
            f"gross_pay__{self.doc_id}": "2166.0",
            f"net_pay__{self.doc_id}": "1689.48",
        }
        resp = client.post(f"/household/{HH}/profile/confirm", data=form, follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303, 307))
        household = storage.get_household(HH)
        self.assertTrue(household["documents"][self.doc_id]["confirmed"])

    def test_partial_fill_still_blocks_confirmation(self):
        form = {
            f"document_type__{self.doc_id}": "pay_stub",
            f"person_name__{self.doc_id}": "Mara North",
            # gross_pay and the rest left blank on purpose.
        }
        client.post(f"/household/{HH}/profile/confirm", data=form)
        household = storage.get_household(HH)
        self.assertFalse(household["documents"][self.doc_id]["confirmed"])


if __name__ == "__main__":
    unittest.main()
