"""Module 3 PRD (packet-screen gap closure): missing/expired status shown
with an icon (never color alone), a packet preview of confirmed field
values before download, an "edit" path back to Upload, a one-line consent
reminder, and internal test-fixture household IDs moved out of the
renter-facing household switcher into a clearly separated dev/test area.
"""
import html
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

with DOCUMENT_GOLD_PATH.open() as _f:
    _GOLD_BY_FILENAME = {g["file_name"]: g for g in (json.loads(l) for l in _f if l.strip())}


def _confirm_household(household_id: str, size: int, filenames: list) -> str:
    storage.delete_household(household_id)
    client.post(f"/household/{household_id}/size", data={"household_size": size})
    files = [("files", (n, (DOCUMENTS_DIR / n).read_bytes(), "application/pdf")) for n in filenames]
    client.post(f"/household/{household_id}/profile/upload", files=files, data={"consent": "1"})

    confirm_page = client.get(f"/household/{household_id}/profile/confirm").text
    doc_ids = re.findall(r'name="document_type__([^"]+)"', confirm_page)
    ordered_gold = [_GOLD_BY_FILENAME[n] for n in filenames]
    assert len(doc_ids) == len(ordered_gold)

    form = {}
    for doc_id, g in zip(doc_ids, ordered_gold):
        for f in g["fields"]:
            if f["field"] == "untrusted_instruction_text":
                continue
            form[f"{f['field']}__{doc_id}"] = str(f["value"])
    client.post(f"/household/{household_id}/profile/confirm", data=form)
    return html.unescape(client.get(f"/household/{household_id}/packet").text)


class ChecklistStatusDisplayTests(unittest.TestCase):
    def test_present_item_has_checkmark_icon(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        self.assertIn("✓ Present", page)

    def test_missing_item_has_icon_and_reason_text(self):
        # HH-003 is missing its employment letter per the checklist fixture.
        page = _confirm_household(
            "HH-003", 3,
            ["hh-003_d01_application_summary.pdf", "hh-003_d02_pay_stub.pdf",
             "hh-003_d03_pay_stub.pdf", "hh-003_d04_benefit_letter.pdf"],
        )
        self.assertIn("⚠ Missing", page)
        self.assertIn("We don't have this yet", page)

    def test_expired_item_has_icon_and_reason_text(self):
        page = _confirm_household(
            "HH-005", 5,
            ["hh-005_d01_application_summary.pdf", "hh-005_d02_pay_stub.pdf",
             "hh-005_d03_pay_stub.pdf", "hh-005_d04_employment_letter.pdf"],
        )
        self.assertIn("⚠ Expired", page)
        self.assertIn("older than 60 days", page)
        self.assertIn("needs a newer copy", page)

    def test_edit_link_appears_when_something_needs_fixing(self):
        page = _confirm_household(
            "HH-003", 3,
            ["hh-003_d01_application_summary.pdf", "hh-003_d02_pay_stub.pdf",
             "hh-003_d03_pay_stub.pdf", "hh-003_d04_benefit_letter.pdf"],
        )
        self.assertIn("Go back and edit", page)

    def test_no_edit_hint_clutter_when_everything_is_present(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        self.assertNotIn("fix-hint", page.split("Preview your packet")[0])


class PacketPreviewTests(unittest.TestCase):
    def test_preview_section_is_collapsed_and_shows_plain_field_values(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        self.assertIn("Preview your packet", page)
        self.assertIn("<details", page)
        # A plain field label from the confirmed pay stub should be visible.
        self.assertIn("Income before taxes", page)

    def test_preview_shows_no_raw_document_ids(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        preview_section = page.split("Preview your packet")[1].split("Download your application packet")[0]
        self.assertNotIn("HH-001-", preview_section)


class ConsentReminderTests(unittest.TestCase):
    def test_consent_reminder_shown_after_consent_given(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        self.assertIn("You agreed to how your data is used", page)
        self.assertIn("review again", page)


class PacketPreviewFormattingTests(unittest.TestCase):
    """Follow-up gap closure: plain document labels (not file names), money
    formatting, an in-table expired warning, and a post-download
    confirmation region."""

    def test_pay_stub_labels_distinguish_by_pay_period_not_file_name(self):
        # HH-001 has two pay stubs covering different periods -- the preview
        # must read as two distinct pay stubs, not two identical file names.
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        preview_section = page.split("Preview your packet")[1].split("Download your application packet")[0]
        self.assertIn("Pay stub — Jun 10-23", preview_section)
        self.assertIn("Pay stub — Jun 3-16", preview_section)
        self.assertNotIn(".pdf", preview_section)

    def test_money_fields_show_dollar_sign_and_two_decimals(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        preview_section = page.split("Preview your packet")[1].split("Download your application packet")[0]
        self.assertIn("$2,166.00", preview_section)
        self.assertNotIn("2166.0<", preview_section)

    def test_hourly_rate_shows_per_hour_suffix(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        preview_section = page.split("Preview your packet")[1].split("Download your application packet")[0]
        self.assertIn("$28.50/hour", preview_section)

    def test_expired_document_gets_inline_warning_in_preview_table(self):
        page = _confirm_household(
            "HH-005", 5,
            ["hh-005_d01_application_summary.pdf", "hh-005_d02_pay_stub.pdf",
             "hh-005_d03_pay_stub.pdf", "hh-005_d04_employment_letter.pdf"],
        )
        preview_section = page.split("Preview your packet")[1].split("Download your application packet")[0]
        self.assertIn("This document is expired", preview_section)

    def test_download_confirmation_region_present_and_announced(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        self.assertIn('aria-live="polite"', page)
        self.assertIn("Your packet has been downloaded", page)
        self.assertIn("RealDoor never sends it for you", page)
        # Hidden until the download link is actually clicked.
        self.assertIn('id="download-confirm"', page)


class DevToolsSeparationTests(unittest.TestCase):
    def test_household_test_fixture_list_not_next_to_switcher_field(self):
        page = _confirm_household(
            "HH-001", 1,
            ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
             "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"],
        )
        switcher_idx = page.find("switcher")
        dev_tools_idx = page.find("dev-tools")
        self.assertGreater(dev_tools_idx, -1)
        # The dev/test household list must sit well after the switcher form,
        # not immediately beneath the household field a renter interacts with.
        self.assertGreater(dev_tools_idx, switcher_idx)
        self.assertIn("Developer / test tools", page)

    def test_welcome_screen_does_not_duplicate_the_dev_tools_footer(self):
        page = client.get("/welcome").text
        self.assertEqual(page.count("Developer / test tools"), 0)


if __name__ == "__main__":
    unittest.main()
