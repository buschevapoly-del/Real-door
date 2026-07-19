"""The renter-facing "Download your application packet" button must
return a plain-language PDF (no bbox, rule_id, rule_corpus_version, or
other audit-only technical detail) -- that raw internal record is still
available, but only via a separate "Technical export" link aimed at
judges/verification, never as the primary download.
"""
import io
import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pdfplumber
from fastapi.testclient import TestClient

from app.main import app
from app import storage
from app.config import DOCUMENTS_DIR, DOCUMENT_GOLD_PATH

client = TestClient(app)

HH = "HH-EXPORT-FORMAT-TEST"

with DOCUMENT_GOLD_PATH.open() as _f:
    _GOLD_BY_FILENAME = {g["file_name"]: g for g in (json.loads(l) for l in _f if l.strip())}


def _confirm_household(household_id: str, size: int, filenames: list) -> None:
    """`size` documents the fixture's expected household size (matching
    its application_summary) -- household size itself is no longer set
    separately; it propagates from that confirmed document."""
    storage.delete_household(household_id)
    files = [("files", (n, (DOCUMENTS_DIR / n).read_bytes(), "application/pdf")) for n in filenames]
    client.post(f"/household/{household_id}/profile/upload", files=files, data={"consent": "1"})

    confirm_page = client.get(f"/household/{household_id}/profile/confirm").text
    doc_ids = re.findall(r'name="document_type__([^"]+)"', confirm_page)
    ordered_gold = [_GOLD_BY_FILENAME[n] for n in filenames]
    form = {}
    for doc_id, g in zip(doc_ids, ordered_gold):
        for f in g["fields"]:
            if f["field"] == "untrusted_instruction_text":
                continue
            form[f"{f['field']}__{doc_id}"] = str(f["value"])
    client.post(f"/household/{household_id}/profile/confirm", data=form)
    assert storage.get_household(household_id)["household_size"] == size


def _export_pdf_text(household_id: str) -> str:
    resp = client.get(f"/household/{household_id}/export")
    assert resp.status_code == 200
    assert "application/pdf" in resp.headers["content-type"]
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return " ".join(raw.split())  # collapse PDF line-wrapping for substring checks


class RenterFacingExportTests(unittest.TestCase):
    def setUp(self):
        _confirm_household(
            HH, 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )

    def test_download_button_points_at_pdf_not_json(self):
        page = client.get(f"/household/{HH}/packet").text
        self.assertIn(f'/household/{HH}/export"', page)

    def test_export_is_pdf_with_no_technical_detail(self):
        resp = client.get(f"/household/{HH}/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/pdf", resp.headers["content-type"])
        body = _export_pdf_text(HH)
        self.assertNotIn("bbox", body.lower())
        self.assertNotIn("rule_corpus_version", body)
        self.assertNotIn("confidence", body.lower())
        self.assertNotIn("NEEDS_REVIEW", body)
        self.assertNotIn("READY_TO_REVIEW", body)

    def test_export_shows_plain_language_and_formatted_money(self):
        body = _export_pdf_text(HH)
        self.assertIn("your application packet", body.lower())
        self.assertIn("Income summary", body)
        self.assertRegex(body, r"\$[\d,]+\.\d{2}")

    def test_export_never_submitted_automatically_note_present(self):
        body = _export_pdf_text(HH)
        self.assertIn("never submitted automatically", body)


class TechnicalExportTests(unittest.TestCase):
    def setUp(self):
        _confirm_household(
            HH, 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )

    def test_technical_export_is_json_with_full_detail(self):
        resp = client.get(f"/household/{HH}/export/technical")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/json", resp.headers["content-type"])
        payload = resp.json()
        self.assertIn("profile", payload)
        self.assertIn("submission", payload)
        # The full internal record (bbox, rule_corpus_version) still lives
        # here -- it's just no longer the renter's primary download.
        self.assertIn("rule_corpus_version", json.dumps(payload["submission"]))

    def test_technical_export_link_present_on_packet_screen_but_not_primary(self):
        page = client.get(f"/household/{HH}/packet").text
        self.assertIn("Technical export", page)
        primary_idx = page.find("Download your application packet")
        technical_idx = page.find("Technical export")
        self.assertGreater(technical_idx, primary_idx)

    def test_technical_export_logs_its_own_activity_action(self):
        client.get(f"/household/{HH}/export/technical")
        household = storage.get_household(HH)
        actions = [entry["action"] for entry in household["activity_log"]]
        self.assertIn("technical_export_downloaded", actions)


if __name__ == "__main__":
    unittest.main()
