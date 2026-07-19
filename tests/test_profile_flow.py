"""Module 1's 3-screen flow (PRD: Upload -> Review -> Confirm):
multi-file upload in one action, plain-language labels (no raw field keys
or bbox coordinates shown), confidence as a visual state by default, and
the hard block that keeps unconfirmed data out of Module 2.
"""
import html
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

HH = "HH-PROFILE-FLOW-TEST"


class MultiFileUploadTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)

    def test_multiple_files_upload_in_one_action(self):
        names = ["hh-001_d01_application_summary.pdf", "hh-003_d02_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"]
        files = [
            ("files", (n, (DOCUMENTS_DIR / n).read_bytes(), "application/pdf")) for n in names
        ]
        resp = client.post(f"/household/{HH}/profile/upload", files=files, data={"consent": "1"})
        self.assertIn(resp.status_code, (200, 303))
        household = storage.get_household(HH)
        self.assertEqual(len(household["documents"]), 3)

    def test_unconfirmed_documents_never_feed_module_2(self):
        names = ["hh-001_d01_application_summary.pdf", "hh-003_d02_pay_stub.pdf"]
        files = [("files", (n, (DOCUMENTS_DIR / n).read_bytes(), "application/pdf")) for n in names]
        client.post(f"/household/{HH}/profile/upload", files=files, data={"consent": "1"})
        # Uploaded but not yet confirmed -- Module 2 must see nothing.
        payload = client.get(f"/household/{HH}/submission.json").json()["submission"]
        self.assertIn("error", payload)


class PlainLanguageTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)
        pdf = DOCUMENTS_DIR / "hh-003_d02_pay_stub.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )

    def test_review_screen_uses_plain_labels_not_raw_keys(self):
        page = html.unescape(client.get(f"/household/{HH}/profile/review").text)
        self.assertIn("Income before taxes", page)
        self.assertIn("How often you're paid", page)
        # Raw internal keys should not appear as visible label text.
        self.assertNotIn(">gross_pay<", page)
        self.assertNotIn(">pay_frequency<", page)

    def test_review_screen_never_shows_raw_bbox_coordinates(self):
        page = client.get(f"/household/{HH}/profile/review").text
        self.assertNotIn("bbox", page.lower())
        self.assertFalse(re.search(r"\[\d+\.?\d*,\s*\d+\.?\d*,\s*\d+\.?\d*,\s*\d+\.?\d*\]", page))

    def test_confirm_screen_never_shows_raw_bbox_coordinates(self):
        page = client.get(f"/household/{HH}/profile/confirm").text
        self.assertNotIn("bbox", page.lower())

    def test_review_screen_shows_confidence_as_state_not_bare_number_by_default(self):
        page = client.get(f"/household/{HH}/profile/review").text
        self.assertIn("Looks good", page)
        # The raw number still exists (inside "Show details"), but the
        # primary/default label is the plain-language state, not "0.92" bare.
        self.assertIn("show-details", page)

    def test_step_indicator_shows_step_not_module_name(self):
        # All three upload sub-screens belong to the outer "Step 1" stage
        # (Upload your documents); the sub-step itself is distinguished by
        # the screen's own heading, not a second numeric indicator.
        page = client.get(f"/household/{HH}/profile").text
        self.assertIn("Step 1 of 3", page)
        review = client.get(f"/household/{HH}/profile/review").text
        self.assertIn("Step 1 of 3", review)
        confirm = client.get(f"/household/{HH}/profile/confirm").text
        self.assertIn("Step 1 of 3", confirm)


class ViewSourceImageTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)
        pdf = DOCUMENTS_DIR / "hh-003_d02_pay_stub.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        self.document_id = next(iter(storage.get_household(HH)["documents"]))

    def test_plain_image_is_served(self):
        resp = client.get(f"/household/{HH}/documents/{self.document_id}/image")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")

    def test_highlighted_source_image_differs_from_plain_image(self):
        plain = client.get(f"/household/{HH}/documents/{self.document_id}/image").content
        highlighted = client.get(
            f"/household/{HH}/documents/{self.document_id}/fields/gross_pay/source"
        ).content
        self.assertEqual(200, client.get(f"/household/{HH}/documents/{self.document_id}/fields/gross_pay/source").status_code)
        self.assertNotEqual(plain, highlighted)


class ConfirmGateTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)

    def test_confirming_moves_data_into_module_2(self):
        # No application_summary uploaded here -- household size comes
        # through the standalone "Household size" field on Confirm instead
        # (the same fallback a no-application-summary household always gets).
        pdf = DOCUMENTS_DIR / "hh-003_d02_pay_stub.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        before = client.get(f"/household/{HH}/submission.json").json()["submission"]
        self.assertIn("error", before)

        confirm_page = client.get(f"/household/{HH}/profile/confirm").text
        self.assertIn('name="household_size"', confirm_page)
        doc_id = re.findall(r'name="document_type__([^"]+)"', confirm_page)[0]
        resp = client.post(
            f"/household/{HH}/profile/confirm",
            data={f"confirm__{doc_id}": "1", "household_size": "1"},
        )
        self.assertIn(resp.status_code, (200, 303))

        after = client.get(f"/household/{HH}/submission.json").json()["submission"]
        self.assertNotIn("error", after)
        self.assertIn(after["readiness_status"], ("READY_TO_REVIEW", "NEEDS_REVIEW"))


if __name__ == "__main__":
    unittest.main()
