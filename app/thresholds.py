"""Frozen 60% AMI threshold lookup for Boston-Cambridge-Quincy, MA-NH HMFA."""
import csv
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from app.config import THRESHOLD_CSV_PATH


@dataclass
class ThresholdRow:
    household_size: int
    income_limit_60_percent: float
    effective_date: str
    source_url: str
    source_pdf_page: str
    hud_area: str


@lru_cache(maxsize=1)
def load_thresholds() -> dict:
    rows = {}
    with THRESHOLD_CSV_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            size = int(row["household_size"])
            rows[size] = ThresholdRow(
                household_size=size,
                income_limit_60_percent=float(row["income_limit_60_percent"]),
                effective_date=row["effective_date"],
                source_url=row["source_url"],
                source_pdf_page=row["source_pdf_page"],
                hud_area=row["hud_area"],
            )
    return rows


def lookup_threshold(household_size: int) -> Optional[ThresholdRow]:
    """Returns None when household_size falls outside the frozen 1-8 table."""
    return load_thresholds().get(household_size)
