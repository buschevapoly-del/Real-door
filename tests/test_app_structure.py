"""App Structure PRD: 4 separate screens (Welcome, Upload, Summary, Packet)
navigated forward/back with a visible step indicator, instead of one long
scrolling page with "Module 1/2/3" headers. Covers: the Welcome screen
itself, case-number entry routing, forward-nav gating when a required
screen action isn't done yet, the stage indicator's presence/absence, and
that no screen leaks another screen's content or "Module N" language.
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

HH = "HH-APP-STRUCTURE-TEST"


def _confirm_one_document(household_id: str) -> None:
    storage.delete_household(household_id)
    client.post(f"/household/{household_id}/size", data={"household_size": 1})
    pdf = DOCUMENTS_DIR / "hh-001_d01_application_summary.pdf"
    client.post(
        f"/household/{household_id}/profile/upload",
        files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
        data={"consent": "1"},
    )
    confirm_page = client.get(f"/household/{household_id}/profile/confirm").text
    doc_id = re.findall(r'name="document_type__([^"]+)"', confirm_page)[0]
    client.post(f"/household/{household_id}/profile/confirm", data={f"confirm__{doc_id}": "1"})


class WelcomeScreenTests(unittest.TestCase):
    def test_root_redirects_to_welcome(self):
        resp = client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertTrue(resp.headers["location"].endswith("/welcome"))

    def test_welcome_page_is_reachable_and_has_no_stage_indicator(self):
        resp = client.get("/welcome")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Welcome to RealDoor", resp.text)
        self.assertIn("RealDoor never decides", resp.text)
        self.assertNotIn("Step 1 of 3", resp.text)
        self.assertNotIn("Step 2 of 3", resp.text)
        self.assertNotIn("Step 3 of 3", resp.text)

    def test_no_screen_uses_module_language(self):
        for path in ("/welcome",):
            page = client.get(path).text
            self.assertNotIn("Module 1", page)
            self.assertNotIn("Module 2", page)
            self.assertNotIn("Module 3", page)


class StageIndicatorTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)
        client.post(f"/household/{HH}/size", data={"household_size": 1})

    def test_stage_indicator_present_on_upload_review_confirm_as_stage_one(self):
        for path in ("/profile", "/profile/review", "/profile/confirm"):
            page = client.get(f"/household/{HH}{path}").text
            self.assertIn("Step 1 of 3", page)
            self.assertNotIn("Module 1", page)

    def test_stage_indicator_shows_stage_two_and_three_on_summary_and_packet(self):
        _confirm_one_document(HH)
        summary = client.get(f"/household/{HH}/summary").text
        self.assertIn("Step 2 of 3", summary)
        self.assertNotIn("Module 2", summary)

        packet = client.get(f"/household/{HH}/packet").text
        self.assertIn("Step 3 of 3", packet)
        self.assertNotIn("Module 3", packet)


class ForwardNavGatingTests(unittest.TestCase):
    def setUp(self):
        storage.delete_household(HH)
        client.post(f"/household/{HH}/size", data={"household_size": 1})

    def test_summary_is_blocked_with_zero_confirmed_documents(self):
        resp = client.get(f"/household/{HH}/summary", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertTrue(resp.headers["location"].endswith(f"/household/{HH}/profile"))

    def test_packet_is_blocked_with_zero_confirmed_documents(self):
        resp = client.get(f"/household/{HH}/packet", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertTrue(resp.headers["location"].endswith(f"/household/{HH}/profile"))

    def test_summary_and_packet_reachable_once_a_document_is_confirmed(self):
        _confirm_one_document(HH)
        self.assertEqual(client.get(f"/household/{HH}/summary").status_code, 200)
        self.assertEqual(client.get(f"/household/{HH}/packet").status_code, 200)


class CaseDispatchTests(unittest.TestCase):
    def test_new_case_dispatches_to_upload(self):
        storage.delete_household("HH-DISPATCH-NEW")
        resp = client.get("/household/HH-DISPATCH-NEW", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertTrue(resp.headers["location"].endswith("/household/HH-DISPATCH-NEW/profile"))

    def test_returning_case_with_confirmed_documents_dispatches_to_summary(self):
        _confirm_one_document(HH)
        resp = client.get(f"/household/{HH}", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 303, 307))
        self.assertTrue(resp.headers["location"].endswith(f"/household/{HH}/summary"))


if __name__ == "__main__":
    unittest.main()
