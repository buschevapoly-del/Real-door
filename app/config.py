"""Paths into the hackathon starter pack and frozen challenge constants."""
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = REPO_ROOT / "realdoor-hackathon-starter-pack"
STARTER_ROOT = PACK_ROOT / "starter"

# Make the starter pack's reference implementation importable as `src.*`
# without copying or modifying it.
if str(STARTER_ROOT) not in sys.path:
    sys.path.insert(0, str(STARTER_ROOT))

RULE_CORPUS_PATH = PACK_ROOT / "rules" / "rule_corpus.jsonl"
THRESHOLD_CSV_PATH = PACK_ROOT / "data" / "mtsp_2026_boston_cambridge_quincy.csv"
CHECKLISTS_PATH = PACK_ROOT / "evaluation" / "application_checklists.json"
QA_GOLD_PATH = PACK_ROOT / "evaluation" / "qa_gold.jsonl"
SUBMISSION_SCHEMA_PATH = STARTER_ROOT / "schemas" / "submission.schema.json"
DOCUMENT_GOLD_PATH = PACK_ROOT / "synthetic_documents" / "gold" / "document_gold.jsonl"
DOCUMENTS_DIR = PACK_ROOT / "synthetic_documents" / "documents"

# Hackathon convention, not a universal LIHTC rule: a document is "current"
# if dated no more than this many days before the frozen event date.
DOCUMENT_CURRENCY_WINDOW_DAYS = 60
EVENT_DATE = "2026-07-18"

DATA_DIR = REPO_ROOT / "app_data"
DATA_DIR.mkdir(exist_ok=True)

# Rendered page images (see app/extraction.py:_render_page_image) live here,
# one per document, under a per-household subfolder so deleting a household
# also deletes its images (never a separate, longer-lived store).
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(exist_ok=True)


def _rule_corpus_version() -> str:
    """An honest fingerprint of the actual frozen rule corpus file -- changes
    if and only if that file changes, rather than a hand-maintained number."""
    digest = hashlib.sha256(RULE_CORPUS_PATH.read_bytes()).hexdigest()[:12]
    return f"frozen-{EVENT_DATE}-{digest}"


RULE_CORPUS_VERSION = _rule_corpus_version()
