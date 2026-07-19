"""Visual/UX redesign (PRD v2): warm palette + layout changes are pure
CSS and not worth asserting on directly, but this redesign also changed
concrete, testable behavior:
  - a small FAQ accordion on Summary, backed by the real rule Q&A engine
    (grounded/citation-backed, not hand-written copy)
  - the 60-day currency banner moved out of Summary/Packet's primary view
    into the collapsed "Show calculation details" section
  - Upload's document lists show plain-language labels, not raw filenames
None of this touches calculation logic, extraction logic, citations, or
safety behavior -- only how/where things are displayed.
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from app.main import app
from app import storage
from app.config import DOCUMENTS_DIR, DOCUMENT_GOLD_PATH

client = TestClient(app)

HH = "HH-VISUAL-REDESIGN-TEST"

with DOCUMENT_GOLD_PATH.open() as _f:
    _GOLD_BY_FILENAME = {g["file_name"]: g for g in (json.loads(l) for l in _f if l.strip())}

HH_001_FILES = [
    "hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
    "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf",
]


def _confirm_household(household_id: str, filenames: list) -> None:
    storage.delete_household(household_id)
    files = [("files", (n, (DOCUMENTS_DIR / n).read_bytes(), "application/pdf")) for n in filenames]
    client.post(f"/household/{household_id}/profile/upload", files=files, data={"consent": "1"})
    confirm_page = client.get(f"/household/{household_id}/profile/confirm").text
    doc_ids = re.findall(r'name="document_type__([^"]+)"', confirm_page)
    form = {}
    for doc_id, filename in zip(doc_ids, filenames):
        g = _GOLD_BY_FILENAME[filename]
        for f in g["fields"]:
            if f["field"] == "untrusted_instruction_text":
                continue
            form[f"{f['field']}__{doc_id}"] = str(f["value"])
    client.post(f"/household/{household_id}/profile/confirm", data=form)


class FaqAccordionTests(unittest.TestCase):
    def setUp(self):
        _confirm_household(HH, HH_001_FILES)

    def test_faq_section_present_with_expected_questions(self):
        page = client.get(f"/household/{HH}/summary").text
        self.assertIn("Common questions about the rules", page)
        self.assertIn("How is my recurring income annualized?", page)
        self.assertIn("Can RealDoor decide if I qualify?", page)

    def test_faq_answers_are_grounded_with_citations_not_fabricated(self):
        page = client.get(f"/household/{HH}/summary").text
        # The "Can I qualify" FAQ answer should be the same governance
        # disclaimer text the QA engine gives everywhere else -- not a
        # separately hand-written paraphrase.
        self.assertIn("RealDoor does not determine eligibility, approval, denial, or priority", page)
        self.assertIn("Source", page)

    def test_free_text_ask_box_still_present_below_faq(self):
        page = client.get(f"/household/{HH}/summary").text
        self.assertIn("Don't see your question? Ask below.", page)
        self.assertIn('name="question"', page)


class SixtyDayBannerRelocationTests(unittest.TestCase):
    def setUp(self):
        _confirm_household(HH, HH_001_FILES)

    def test_sixty_day_banner_not_in_summary_primary_view(self):
        page = client.get(f"/household/{HH}/summary").text
        primary_view = page.split("Show calculation details")[0]
        self.assertNotIn("60-day", primary_view)
        full_page = page
        self.assertIn("60-day", full_page)  # still present, just tucked away

    def test_sixty_day_banner_not_present_on_packet_at_all(self):
        page = client.get(f"/household/{HH}/packet").text
        self.assertNotIn("60-day", page)


class PlainLabelsOnUploadTests(unittest.TestCase):
    def setUp(self):
        _confirm_household(HH, HH_001_FILES)

    def test_document_list_shows_plain_labels_not_raw_filenames(self):
        page = client.get(f"/household/{HH}/profile").text
        self.assertIn("Application summary", page)
        self.assertIn("Pay stub", page)
        self.assertNotIn(".pdf", page)

    def test_no_household_size_form_on_upload_screen(self):
        page = client.get(f"/household/{HH}/profile").text
        self.assertNotIn("How many people are in your household", page)


if __name__ == "__main__":
    unittest.main()
