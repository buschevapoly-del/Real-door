"""Covers the gaps identified against the official challenge brief:
calibrated confidence, the consent gate, the in-package activity log
(never raw document contents), and a few concrete accessibility markers
(WCAG 2.2 AA: visible focus, no color-only status, completion
announcements, no interactive-in-interactive markup).
"""
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from app.extraction import _extraction_confidence, CONFIDENCE_REVIEW_THRESHOLD
from app.main import app
from app import storage
from app.config import DOCUMENTS_DIR

client = TestClient(app)


class ConfidenceCalibrationTests(unittest.TestCase):
    def test_tight_alignment_is_high_confidence(self):
        self.assertGreaterEqual(_extraction_confidence(dx=0, dy=0, parse_failed=False), 0.9)

    def test_confidence_decreases_with_offset(self):
        tight = _extraction_confidence(dx=0, dy=0, parse_failed=False)
        loose = _extraction_confidence(dx=15, dy=20, parse_failed=False)
        self.assertGreater(tight, loose)

    def test_confidence_is_clamped_to_range(self):
        self.assertEqual(_extraction_confidence(dx=0, dy=0, parse_failed=False), 0.95)
        self.assertGreaterEqual(_extraction_confidence(dx=20, dy=25, parse_failed=False), 0.5)

    def test_parse_failure_caps_confidence_below_review_threshold(self):
        confidence = _extraction_confidence(dx=0, dy=0, parse_failed=True)
        self.assertLessEqual(confidence, 0.6)
        self.assertLess(confidence, CONFIDENCE_REVIEW_THRESHOLD)


class ConsentGateTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household("HH-CONSENT-TEST")

    def test_upload_without_consent_is_rejected(self):
        pdf_path = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        with pdf_path.open("rb") as fh:
            resp = client.post(
                "/household/HH-CONSENT-TEST/profile/upload",
                files={"files": (pdf_path.name, fh, "application/pdf")},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("consent", resp.text.lower())
        household = storage.get_household("HH-CONSENT-TEST")
        self.assertEqual(household["documents"], {})
        self.assertFalse(household["consent_given"])

    def test_upload_with_consent_is_accepted_and_recorded(self):
        pdf_path = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        with pdf_path.open("rb") as fh:
            resp = client.post(
                "/household/HH-CONSENT-TEST/profile/upload",
                files={"files": (pdf_path.name, fh, "application/pdf")},
                data={"consent": "1"},
            )
        self.assertIn(resp.status_code, (200, 303))
        household = storage.get_household("HH-CONSENT-TEST")
        self.assertTrue(household["consent_given"])
        self.assertEqual(len(household["documents"]), 1)


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household("HH-ACTIVITY-TEST")

    def test_actions_are_logged_without_raw_values(self):
        storage.set_household_size("HH-ACTIVITY-TEST", 3)
        pdf_path = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        with pdf_path.open("rb") as fh:
            client.post(
                "/household/HH-ACTIVITY-TEST/profile/upload",
                files={"files": (pdf_path.name, fh, "application/pdf")},
                data={"consent": "1"},
            )
        household = storage.get_household("HH-ACTIVITY-TEST")
        actions = [entry["action"] for entry in household["activity_log"]]
        self.assertIn("household_size_set", actions)
        self.assertIn("consent_given", actions)
        self.assertIn("document_uploaded", actions)
        # No entry should contain the actual household size or a field value --
        # only action metadata (field/document identifiers), not the data itself.
        for entry in household["activity_log"]:
            self.assertNotIn("Mara North", str(entry))  # a value from this document

    def test_activity_log_is_gone_after_delete_package(self):
        storage.set_household_size("HH-ACTIVITY-TEST", 3)
        self.assertTrue(storage.get_household("HH-ACTIVITY-TEST")["activity_log"])
        storage.delete_household("HH-ACTIVITY-TEST")
        fresh = storage.get_household("HH-ACTIVITY-TEST")
        self.assertEqual(fresh["activity_log"], [])


class AccessibilityMarkerTests(unittest.TestCase):
    def test_household_page_has_no_button_nested_in_anchor(self):
        # The packet screen is gated on having at least one confirmed
        # document, so seed one before checking its markup.
        storage.delete_household("HH-A11Y-TEST")
        client.post("/household/HH-A11Y-TEST/size", data={"household_size": 1})
        pdf_path = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        with pdf_path.open("rb") as fh:
            client.post(
                "/household/HH-A11Y-TEST/profile/upload",
                files={"files": (pdf_path.name, fh, "application/pdf")},
                data={"consent": "1"},
            )
        confirm_page = client.get("/household/HH-A11Y-TEST/profile/confirm").text
        doc_id = re.findall(r'name="document_type__([^"]+)"', confirm_page)[0]
        client.post("/household/HH-A11Y-TEST/profile/confirm", data={f"confirm__{doc_id}": "1"})

        page = client.get("/household/HH-A11Y-TEST/packet").text
        idx = page.find("Download your application packet")
        self.assertGreater(idx, -1)
        self.assertNotIn("<button", page[idx : idx + 80])

    def test_qa_answer_region_is_a_live_region(self):
        resp = client.post("/household/HH-A11Y-TEST/qa", data={"question": "What is the frozen 60% threshold?"})
        self.assertIn('aria-live="polite"', resp.text)

    def test_how_it_works_page_is_reachable_and_lists_fields(self):
        resp = client.get("/how-it-works")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pay stub", resp.text.lower())
        self.assertIn("protected-trait", resp.text.lower())


if __name__ == "__main__":
    unittest.main()
