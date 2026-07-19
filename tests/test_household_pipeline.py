"""End-to-end regression test: drive the real FastAPI app (upload -> review
-> confirm -> calculate) for all 6 household fixtures in the hackathon pack
and check the result against evaluation/application_checklists.json.

This runs the actual HTTP routes (not the internal functions directly) so
it exercises the same code path a browser would, through Module 1's 3
screens (Upload / Review / Confirm).
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
from app.config import PACK_ROOT, DOCUMENTS_DIR, DOCUMENT_GOLD_PATH, CHECKLISTS_PATH

client = TestClient(app)


def _load_gold_by_household():
    with DOCUMENT_GOLD_PATH.open() as f:
        gold = [json.loads(l) for l in f if l.strip()]
    by_hh = {}
    for g in gold:
        by_hh.setdefault(g["household_id"], []).append(g)
    return by_hh


def _run_household(household_id: str, size: int, documents: list) -> dict:
    # No separate household-size step: household size is now confirmed
    # like any other extracted field, from the application_summary
    # document included in `documents` below -- the "size" gold value
    # (checked below) must match what that document's own field states.
    ordered_gold = sorted(documents, key=lambda d: d["document_id"])
    files = [
        ("files", (g["file_name"], (DOCUMENTS_DIR / g["file_name"]).read_bytes(), "application/pdf"))
        for g in ordered_gold
    ]
    resp = client.post(
        f"/household/{household_id}/profile/upload",
        files=files,
        data={"consent": "1"},
    )
    assert resp.status_code in (200, 303)

    page = client.get(f"/household/{household_id}/profile/confirm").text
    doc_ids = re.findall(r'name="document_type__([^"]+)"', page)
    assert len(doc_ids) == len(ordered_gold), (doc_ids, [g["document_id"] for g in ordered_gold])

    form = {}
    for doc_id, g in zip(doc_ids, ordered_gold):
        for f in g["fields"]:
            if f["field"] == "untrusted_instruction_text":
                continue
            form[f"{f['field']}__{doc_id}"] = str(f["value"])
    resp = client.post(f"/household/{household_id}/profile/confirm", data=form)
    assert resp.status_code in (200, 303)

    assert storage.get_household(household_id)["household_size"] == size, (
        "household size should have propagated from the confirmed application_summary"
    )

    return client.get(f"/household/{household_id}/submission.json").json()


class HouseholdPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with CHECKLISTS_PATH.open() as f:
            cls.checklists = {row["household_id"]: row for row in json.load(f)}
        cls.gold_by_hh = _load_gold_by_household()

    def setUp(self):
        for household_id in self.checklists:
            storage.delete_household(household_id)

    def test_all_six_households_match_gold(self):
        for household_id, expected in self.checklists.items():
            with self.subTest(household=household_id):
                result = _run_household(
                    household_id, expected["household_size"], self.gold_by_hh[household_id]
                )
                payload = result["submission"]
                self.assertEqual(result["schema_errors"], [])
                self.assertAlmostEqual(
                    payload["annualized_income"], expected["expected_annualized_income"], places=2
                )
                self.assertEqual(payload["comparison"], expected["comparison"])
                self.assertEqual(payload["readiness_status"], expected["expected_readiness_status"])
                self.assertEqual(
                    sorted(payload["review_reasons"]), sorted(expected["expected_review_reasons"])
                )


if __name__ == "__main__":
    unittest.main()
