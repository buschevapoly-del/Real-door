"""One-off helper for demo prep: wipe household state (documents, activity
log, page images) so a walkthrough in front of judges starts from a clean
slate instead of carrying dozens of leftover entries from automated test
runs (uploads/deletes of the same fixtures, repeated many times).

Wiping a household here is exactly what the in-app "Delete package" button
does (storage.delete_household) -- a single file removal with nothing left
behind, including the activity log. This script just does it for several
households at once from the command line, before recording.

Usage:
    python3 scripts/reset_demo_households.py HH-001 HH-002
    python3 scripts/reset_demo_households.py --all-test-fixtures
    python3 scripts/reset_demo_households.py --all
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app import storage
from app.config import DATA_DIR


def _all_household_ids() -> list:
    return sorted(p.stem for p in DATA_DIR.glob("*.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("household_ids", nargs="*", help="Specific household IDs to wipe.")
    parser.add_argument(
        "--all-test-fixtures", action="store_true",
        help="Wipe every household whose ID ends in -TEST (leftover automated-test runs).",
    )
    parser.add_argument("--all", action="store_true", help="Wipe every household on disk. Use with care.")
    args = parser.parse_args()

    if args.all:
        targets = _all_household_ids()
    elif args.all_test_fixtures:
        targets = [hh for hh in _all_household_ids() if hh.endswith("-TEST")]
    else:
        targets = args.household_ids

    if not targets:
        parser.error("Specify household IDs, or pass --all-test-fixtures / --all.")

    for household_id in targets:
        deleted = storage.delete_household(household_id)
        print(f"{'wiped' if deleted else 'nothing to wipe for'}: {household_id}")


if __name__ == "__main__":
    main()
