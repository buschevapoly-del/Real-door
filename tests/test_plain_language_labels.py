"""Module 2 (Understand) and Module 3 (Prepare) PRDs: the primary view must
read in plain language -- no enums, status codes, reason codes, document
IDs, or a raw activity log by default. Full audit detail (rule IDs,
citations, rule-corpus version, raw submission JSON, activity log) must
still be reachable, just tucked behind a single collapsed section, and
none of the underlying calculation/logging behavior may change.
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

from app.labels import reason_code_label, activity_label, format_timestamp
from app.main import app
from app import storage
from app.config import DOCUMENTS_DIR, DOCUMENT_GOLD_PATH

client = TestClient(app)

HH = "HH-PLAIN-LANGUAGE-TEST"

with DOCUMENT_GOLD_PATH.open() as _f:
    _GOLD_BY_FILENAME = {g["file_name"]: g for g in (json.loads(l) for l in _f if l.strip())}


def _confirm_household(household_id: str, size: int, filenames: list) -> dict:
    """Upload + confirm using each file's real gold field values (including
    manually-entered ones for rasterized documents) -- not just clicking
    through with blank inputs, so scenarios like an expired document are
    reproduced faithfully rather than silently skipped."""
    storage.delete_household(household_id)
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
    # `size` documents the fixture's expected household size (matching its
    # application_summary) -- household size propagates from that
    # confirmed document rather than being set separately.
    assert storage.get_household(household_id)["household_size"] == size
    return html.unescape(client.get(f"/household/{household_id}/summary").text)


def _packet_page(household_id: str) -> str:
    return html.unescape(client.get(f"/household/{household_id}/packet").text)


class ReasonCodeLabelTests(unittest.TestCase):
    def test_known_codes_match_prd_table(self):
        self.assertEqual(
            reason_code_label("INCOMPLETE_PAY_STUB_FIELDS"),
            "One of your pay stubs is missing some information",
        )
        self.assertEqual(
            reason_code_label("PAY_STUB_TOTAL_CONFLICT"),
            "The amounts on your pay stubs don't match",
        )
        self.assertEqual(
            reason_code_label("GIG_INCOME_UNCORROBORATED"),
            "Your freelance/gig income needs an extra document to confirm it",
        )
        self.assertEqual(
            reason_code_label("EMPLOYMENT_LETTER_EXPIRED"),
            "Your employment letter is older than we can accept -- please provide a recent one",
        )

    def test_unlisted_expired_code_falls_back_to_a_readable_sentence(self):
        label = reason_code_label("BENEFIT_LETTER_EXPIRED")
        self.assertIn("benefit letter", label.lower())
        self.assertNotIn("_", label)

    def test_completely_unknown_code_never_crashes(self):
        label = reason_code_label("SOME_FUTURE_CODE")
        self.assertNotIn("_", label)


class ActivityLabelTests(unittest.TestCase):
    def test_upload_and_household_size_actions_read_in_plain_english(self):
        self.assertEqual(activity_label({"action": "household_size_set"}), "You entered your household size")
        self.assertIn(
            "pay stub",
            activity_label({"action": "document_uploaded", "detail": "HH-001-ABC (pay_stub)"}),
        )

    def test_timestamp_is_human_readable(self):
        text = format_timestamp("2026-07-18T22:55:49.902865+00:00")
        self.assertNotIn("T", text)
        self.assertIn("2026", text)


class Module2PlainViewTests(unittest.TestCase):
    def test_needs_review_household_shows_plain_language_by_default(self):
        # HH-002: pay stub totals disagree -> NEEDS_REVIEW / PAY_STUB_TOTAL_CONFLICT.
        page = _confirm_household(
            HH, 2, ["hh-002_d01_application_summary.pdf", "hh-002_d02_pay_stub.pdf",
                    "hh-002_d03_pay_stub.pdf", "hh-002_d04_employment_letter.pdf"]
        )
        self.assertIn("Needs a closer look", page)
        self.assertIn("The amounts on your pay stubs don't match", page)
        self.assertIn("Your income is at or below the threshold", page)
        # Raw internals must not leak onto the primary (non-collapsed) view.
        # (The collapsed audit section legitimately quotes the frozen rule
        # text, which itself contains these words -- e.g. CH-READINESS-001's
        # text literally says "...return NEEDS_REVIEW with reasons." -- so
        # the check is scoped to what's visible before that section.)
        primary_view = page.split("Show calculation details")[0]
        self.assertNotIn("NEEDS_REVIEW", primary_view)
        self.assertNotIn("PAY_STUB_TOTAL_CONFLICT", primary_view)
        self.assertNotIn("below_or_equal", primary_view)

    def test_document_ids_not_shown_in_income_lines_table(self):
        page = _confirm_household(HH, 1, ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
                                           "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"])
        self.assertNotIn("HH-001-", page.split("Show calculation details")[0])

    def test_audit_detail_is_collapsed_but_still_present(self):
        page = _confirm_household(HH, 1, ["hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
                                           "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf"])
        self.assertIn("Show calculation details", page)
        self.assertIn("CH-INCOME-001", page)  # still there, just inside <details>
        self.assertIn("View raw submission JSON", page)
        self.assertIn("frozen-2026-07-18", page)  # rule corpus version


class Module3PlainViewTests(unittest.TestCase):
    def test_activity_log_is_collapsed_and_humanized(self):
        _confirm_household(HH, 1, ["hh-001_d01_application_summary.pdf"])
        page = _packet_page(HH)
        self.assertIn("Show activity history", page)
        self.assertIn("You entered your household size", page)
        self.assertIn("You uploaded your application summary", page)
        # No raw ISO timestamps or snake_case action names on the page.
        self.assertNotIn("household_size_set", page)
        self.assertFalse(re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", page))

    def test_export_button_has_plain_label(self):
        _confirm_household(HH, 1, ["hh-001_d01_application_summary.pdf"])
        page = _packet_page(HH)
        self.assertIn("Download your application packet", page)
        self.assertNotIn("(JSON)", page)

    def test_expired_document_shows_plain_status(self):
        # HH-005 is one of the 6 gold fixtures with a real checklist entry
        # (application_checklists.json) -- required_document_types is only
        # populated for those exact household IDs, so this test must reuse
        # one rather than a synthetic ID. Its employment letter is >60 days old.
        _confirm_household(
            "HH-005", 5, ["hh-005_d01_application_summary.pdf", "hh-005_d02_pay_stub.pdf",
                          "hh-005_d03_pay_stub.pdf", "hh-005_d04_employment_letter.pdf"]
        )
        page = _packet_page("HH-005")
        self.assertIn("Expired", page)
        self.assertIn("needs a newer copy", page)


if __name__ == "__main__":
    unittest.main()
