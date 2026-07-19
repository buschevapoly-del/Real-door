"""Follow-up gap closure on the renter-facing packet report:

1. A household-size mismatch between the manually-entered size (used in
   the calculation) and the application_summary's extracted size (shown
   for reference only) must be explained inline, not just shown as two
   silently different numbers that read like a contradiction.
2. A document that ended up "confirmed" with no real data (e.g. legacy
   state from before the confirm-completeness gate existed) must never
   surface as a blank line in the checklist/preview/export -- the report
   must self-heal against stale/corrupted state, not just prevent new
   instances of it.
3. An INCOMPLETE_*_FIELDS review reason should name the specific document
   when it can (e.g. by pay period), and give actionable guidance when it
   can't (a document with literally no extracted data to name it by).
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

HH = "HH-REPORT-ACCURACY-TEST"

with DOCUMENT_GOLD_PATH.open() as _f:
    _GOLD_BY_FILENAME = {g["file_name"]: g for g in (json.loads(l) for l in _f if l.strip())}


def _upload_and_confirm(household_id, filenames, skip_filenames=(), preset_household_size=None):
    """Confirm every file in `filenames` except those in `skip_filenames`,
    which are uploaded but deliberately left blank on the confirm form.

    `preset_household_size`, when given, sets the household size directly
    at the storage layer *before* upload -- simulating a household that
    already had a size on file from an earlier no-application-summary
    session. Household size no longer has an HTTP endpoint of its own: it
    comes from the confirmed application_summary field (propagated after
    confirm, the first time one is confirmed), or the standalone
    "Household size" field on Confirm when there's no application_summary
    at all.
    """
    storage.delete_household(household_id)
    if preset_household_size is not None:
        storage.set_household_size(household_id, preset_household_size)
    files = [("files", (n, (DOCUMENTS_DIR / n).read_bytes(), "application/pdf")) for n in filenames]
    client.post(f"/household/{household_id}/profile/upload", files=files, data={"consent": "1"})

    confirm_page = client.get(f"/household/{household_id}/profile/confirm").text
    doc_ids = re.findall(r'name="document_type__([^"]+)"', confirm_page)
    filename_to_doc_id = dict(zip(filenames, doc_ids))

    form = {}
    for filename, doc_id in filename_to_doc_id.items():
        if filename in skip_filenames:
            continue
        g = _GOLD_BY_FILENAME[filename]
        for f in g["fields"]:
            if f["field"] == "untrusted_instruction_text":
                continue
            form[f"{f['field']}__{doc_id}"] = str(f["value"])
    client.post(f"/household/{household_id}/profile/confirm", data=form)
    return filename_to_doc_id


HH_001_FILES = [
    "hh-001_d01_application_summary.pdf", "hh-001_d02_pay_stub.pdf",
    "hh-001_d03_pay_stub.pdf", "hh-001_d04_employment_letter.pdf",
]


class HouseholdSizeClarityTests(unittest.TestCase):
    """Since household size is now sourced from the confirmed
    application_summary itself, a mismatch can only happen in the one
    residual case the architecture explicitly preserves: a household that
    already had a size on file (e.g. entered by hand in an earlier,
    application-summary-less session) before an application_summary with a
    *different* number gets confirmed. Propagation only fires the first
    time (see profile_confirm_post), so the earlier, already-confirmed
    value is never silently overwritten -- this note is what explains the
    resulting divergence instead of just showing two numbers.
    """

    def test_no_note_when_entered_size_matches_document(self):
        # HH-001's application_summary states household_size 1 -- nothing
        # preset, so it propagates from the document with no divergence.
        _upload_and_confirm(HH, HH_001_FILES)
        body = client.get(f"/household/{HH}/export").text
        self.assertNotIn("RealDoor used the number you entered", body)

    def test_note_appears_when_entered_size_differs_from_document(self):
        _upload_and_confirm(HH, HH_001_FILES, preset_household_size=4)
        body = client.get(f"/household/{HH}/export").text
        self.assertIn("RealDoor used the number you entered -- 4", body)
        self.assertIn("Income limit for a household of 4", body)
        self.assertIn("Number of people in your household: 1", body)

    def test_note_also_shown_in_preview_on_packet_screen(self):
        _upload_and_confirm(HH, HH_001_FILES, preset_household_size=4)
        page = client.get(f"/household/{HH}/packet").text
        page = page.replace("&#39;", "'").replace("&amp;", "&")
        self.assertIn("RealDoor used the number you entered", page)


class BrokenConfirmedDocumentSelfHealingTests(unittest.TestCase):
    """hh-001_d02_pay_stub.pdf has no text layer -- extraction returns
    needs_manual_entry=True with an empty fields dict. Confirming it here
    goes through storage directly (bypassing the route's completeness
    gate) to reproduce exactly what pre-fix code, or a stale synced
    app_data file, would have left on disk."""

    # HH-001 (not the synthetic HH constant) is used here specifically
    # because checklist_status() only has a real "required documents" row
    # for the 6 gold fixture IDs (application_checklists.json) -- a made-up
    # ID always reports everything as "Extra," which would defeat the
    # "still shows Present via the good copy" assertion below.
    def setUp(self):
        self.doc_ids_by_filename = _upload_and_confirm(
            "HH-001", HH_001_FILES, skip_filenames=["hh-001_d02_pay_stub.pdf"]
        )
        broken_doc_id = self.doc_ids_by_filename["hh-001_d02_pay_stub.pdf"]
        storage.confirm_document("HH-001", broken_doc_id)
        household = storage.get_household("HH-001")
        assert household["documents"][broken_doc_id]["confirmed"] is True
        assert household["documents"][broken_doc_id]["fields"] == {}

    def tearDown(self):
        storage.delete_household("HH-001")

    def test_blank_document_never_appears_in_export(self):
        body = client.get("/household/HH-001/export").text
        documents_section = body.split("DOCUMENTS INCLUDED")[1]
        # Only the real, complete pay stub should be listed -- no bare
        # "Pay stub" line with zero fields under it.
        self.assertEqual(documents_section.count("Pay stub"), 1)
        self.assertIn("Pay stub — Jun 3-16", documents_section)

    def test_blank_document_never_appears_in_packet_preview(self):
        page = client.get("/household/HH-001/packet").text
        preview_section = page.split("Preview your packet")[1].split("Download your application packet")[0]
        self.assertEqual(preview_section.count("Pay stub"), 1)

    def test_checklist_still_shows_pay_stub_present_via_the_good_copy(self):
        body = client.get("/household/HH-001/export").text
        self.assertIn("Pay stub: Present", body)


class ReasonSpecificityTests(unittest.TestCase):
    def test_incomplete_reason_names_document_when_a_date_is_available(self):
        # A document confirmed (bypassing the route gate, as legacy state
        # would be) with dates present but gross_pay missing -- income.py
        # requires gross_pay, but doc_preview_label can still read a pay
        # period from the dates that *are* there, so the reason can name it.
        storage.delete_household(HH)
        storage.set_household_size(HH, 1)
        pdf = DOCUMENTS_DIR / "hh-001_d02_pay_stub.pdf"
        client.post(
            f"/household/{HH}/profile/upload",
            files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"consent": "1"},
        )
        household = storage.get_household(HH)
        doc_id = next(iter(household["documents"]))
        storage.set_document_type(HH, doc_id, "pay_stub")
        storage.update_field(HH, doc_id, "pay_period_start", "2026-06-03")
        storage.update_field(HH, doc_id, "pay_period_end", "2026-06-16")
        storage.update_field(HH, doc_id, "pay_date", "2026-06-20")
        storage.update_field(HH, doc_id, "pay_frequency", "biweekly")
        storage.update_field(HH, doc_id, "regular_hours", 76)
        storage.update_field(HH, doc_id, "hourly_rate", 28.5)
        # gross_pay deliberately never set.
        storage.confirm_document(HH, doc_id)

        body = client.get(f"/household/{HH}/export").text
        self.assertIn("The pay stub dated Jun 3-16 is missing some information", body)

    def test_incomplete_reason_gives_actionable_guidance_when_nothing_can_be_named(self):
        self.doc_ids_by_filename = _upload_and_confirm(
            HH, HH_001_FILES, skip_filenames=["hh-001_d02_pay_stub.pdf"]
        )
        broken_doc_id = self.doc_ids_by_filename["hh-001_d02_pay_stub.pdf"]
        storage.confirm_document(HH, broken_doc_id)
        body = client.get(f"/household/{HH}/export").text
        self.assertIn("still needs its details filled in", body)
        self.assertIn("go back and complete it", body)
        # The old vague wording must be gone from the primary report.
        self.assertNotIn("One of your pay stubs is missing some information", body)


if __name__ == "__main__":
    unittest.main()
