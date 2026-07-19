"""Household size architecture: no separate pre-upload step. It's an
extracted+confirmed field like any other (from application_summary),
following the exact same confidence/manual-entry mechanism as gross_pay
or pay_date. A household with no application_summary at all gets a
standalone "Household size" input on the Confirm screen instead --
same visual/messaging pattern as a rasterized document needing manual
entry, not a special early gate. build_submission only ever sees a
household size after it's been confirmed on this screen, never before.
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

HH = "HH-SIZE-ARCHITECTURE-TEST"


class NoStandalonePreUploadStepTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)

    def test_upload_screen_has_no_household_size_form(self):
        page = client.get(f"/household/{HH}/profile").text
        self.assertNotIn("How many people are in your household", page)
        self.assertNotIn("Household size on file", page)

    def test_upload_is_not_gated_on_household_size(self):
        # Uploading works with zero household_size set anywhere yet.
        pdf = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        resp = client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        self.assertIn(resp.status_code, (200, 303))
        self.assertIsNone(storage.get_household(HH)["household_size"])


class ExtractedFieldPathTests(unittest.TestCase):
    """A household that uploads an application_summary gets its size
    exactly like any other field: shown with a confidence badge on
    Review, editable on Confirm, and propagated to the calculation only
    once confirmed."""

    def setUp(self):
        storage.delete_household(HH)
        pdf = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )

    def test_household_size_shown_on_review_like_any_other_field(self):
        page = client.get(f"/household/{HH}/profile/review").text
        self.assertIn("Number of people in your household", page)

    def test_household_size_editable_on_confirm_like_any_other_field(self):
        page = client.get(f"/household/{HH}/profile/confirm").text
        self.assertIn('name="household_size__', page)
        # No separate standalone box needed -- an application_summary
        # exists, so the per-document field covers it.
        self.assertNotIn('name="household_size"', page.replace('name="household_size__', ""))

    def test_build_submission_sees_household_size_only_after_confirm(self):
        before = client.get(f"/household/{HH}/submission.json").json()["submission"]
        self.assertIn("error", before)
        self.assertIsNone(storage.get_household(HH)["household_size"])

        confirm_page = client.get(f"/household/{HH}/profile/confirm").text
        doc_id = re.findall(r'name="document_type__([^"]+)"', confirm_page)[0]
        client.post(
            f"/household/{HH}/profile/confirm",
            data={
                f"person_name__{doc_id}": "Mara North",
                f"household_size__{doc_id}": "1",
                f"address__{doc_id}": "14 Lantern Way, Boston, MA 02118",
                f"application_date__{doc_id}": "2026-07-10",
            },
        )
        self.assertEqual(storage.get_household(HH)["household_size"], 1)

    def test_household_size_not_extracted_is_treated_as_manual_entry_needed(self):
        # hh-002's application_summary is rasterized -- zero auto-extracted
        # fields, so household_size (like every other field) must be typed
        # in by hand; confirming without it blocks exactly like any other
        # missing required field.
        storage.delete_household(HH)
        pdf = DOCUMENTS_DIR / "hh-002_d01_application_summary.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        review_page = client.get(f"/household/{HH}/profile/review").text
        self.assertIn("We couldn't read this automatically", review_page)

        confirm_page = client.get(f"/household/{HH}/profile/confirm").text
        doc_id = re.findall(r'name="document_type__([^"]+)"', confirm_page)[0]
        resp = client.post(f"/household/{HH}/profile/confirm", data={f"person_name__{doc_id}": "Ren Okafor"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Please fill in every field", resp.text)
        self.assertFalse(storage.get_household(HH)["documents"][doc_id]["confirmed"])


class StandaloneFallbackTests(unittest.TestCase):
    """No application_summary at all -- household size gets one
    standalone field on Confirm, same messaging as any unreadable field."""

    def setUp(self):
        storage.delete_household(HH)
        pdf = DOCUMENTS_DIR / "hh-003_d02_pay_stub.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )

    def test_standalone_field_appears_on_confirm(self):
        page = client.get(f"/household/{HH}/profile/confirm").text
        self.assertIn("Household size", page)
        self.assertIn('name="household_size"', page)
        self.assertIn("We couldn't read this automatically", page)

    def test_submitting_standalone_field_sets_household_size(self):
        doc_id = re.findall(
            r'name="document_type__([^"]+)"', client.get(f"/household/{HH}/profile/confirm").text
        )[0]
        client.post(
            f"/household/{HH}/profile/confirm",
            data={
                "household_size": "3",
                f"document_type__{doc_id}": "pay_stub",
                f"person_name__{doc_id}": "Avery Moss",
                f"pay_date__{doc_id}": "2026-06-20",
                f"pay_frequency__{doc_id}": "biweekly",
                f"regular_hours__{doc_id}": "40",
                f"hourly_rate__{doc_id}": "20",
                f"gross_pay__{doc_id}": "800",
                f"pay_period_start__{doc_id}": "2026-06-01",
                f"pay_period_end__{doc_id}": "2026-06-14",
                f"net_pay__{doc_id}": "650",
            },
        )
        self.assertEqual(storage.get_household(HH)["household_size"], 3)

    def test_standalone_field_disappears_once_an_application_summary_exists(self):
        pdf = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        page = client.get(f"/household/{HH}/profile/confirm").text
        self.assertNotIn("We don't have an application summary to read this from", page)


if __name__ == "__main__":
    unittest.main()
