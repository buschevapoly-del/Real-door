"""Deterministic PDF field extraction.

Real text/position based extraction (no OCR, no LLM, no lookup into the
gold answer key). It works because the synthetic documents in this pack
render field labels and field values in two distinct, consistent text
styles (bold gray-blue labels vs. plain black values) laid out as a
label-above-value form grid. We use those style + position signals to
pair each label with its value, and completely discard the decorative
diagonal watermark (identified by its skewed text matrix) and the
sample-specific injected instruction text (identified by its own labeled
block) rather than ever treating either as data or as an instruction.

Every returned field carries a page number, a bounding box and a
confidence score so the UI can show its provenance and let the applicant
confirm or correct it before it is used anywhere else (Module 1
requirement) -- this code never decides anything on its own.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber

LABEL_COLOR = (0.365, 0.404, 0.439)
VALUE_COLOR = (0.0, 0.0, 0.0)
UNTRUSTED_MARKER = "UNTRUSTED DOCUMENT TEXT"

# Fields below this confidence get an explicit review note in the UI.
CONFIDENCE_REVIEW_THRESHOLD = 0.7

# Resolution (pixels per inch) used to render the "view source" page image.
# The original uploaded PDF is still deleted immediately after processing;
# this rendered image is the only visual record kept, and only so the
# applicant (or a reviewer) can see exactly where a value came from.
IMAGE_RESOLUTION = 150

# label text (as rendered, whitespace-normalized) -> canonical field name,
# and a value parser hint, keyed by document_type.
FIELD_MAP: dict[str, dict[str, tuple[str, str]]] = {
    "application_summary": {
        "APPLICANT": ("person_name", "str"),
        "HOUSEHOLD SIZE": ("household_size", "int"),
        "MAILING ADDRESS": ("address", "str"),
        "APPLICATION DATE": ("application_date", "date"),
    },
    "pay_stub": {
        "EMPLOYEE": ("person_name", "str"),
        "PAY DATE": ("pay_date", "date"),
        "PAY PERIOD": ("pay_period_start", "date"),
        "THROUGH": ("pay_period_end", "date"),
        "PAY FREQUENCY": ("pay_frequency", "str"),
        "REGULAR HOURS": ("regular_hours", "float"),
        "HOURLY RATE": ("hourly_rate", "money"),
        "GROSS PAY": ("gross_pay", "money"),
        "NET PAY": ("net_pay", "money"),
    },
    "employment_letter": {
        "EMPLOYEE": ("person_name", "str"),
        "LETTER DATE": ("document_date", "date"),
        "HOURS PER WEEK": ("weekly_hours", "float"),
        "HOURLY RATE": ("hourly_rate", "money"),
    },
    "benefit_letter": {
        "RECIPIENT": ("person_name", "str"),
        "LETTER DATE": ("document_date", "date"),
        "MONTHLY AMOUNT": ("monthly_benefit", "money"),
        "FREQUENCY": ("benefit_frequency", "str"),
    },
    "gig_statement": {
        "WORKER": ("person_name", "str"),
        "STATEMENT MONTH": ("statement_month", "str"),
        "GROSS RECEIPTS": ("gross_receipts", "money"),
        "PLATFORM FEES": ("platform_fees", "money"),
    },
}

DOC_TYPE_HEADER_ALIASES = {
    "application summary": "application_summary",
    "pay stub": "pay_stub",
    "employment letter": "employment_letter",
    "benefit letter": "benefit_letter",
    "gig statement": "gig_statement",
}

FILENAME_HINTS = {
    "application_summary": "application_summary",
    "pay_stub": "pay_stub",
    "employment_letter": "employment_letter",
    "benefit_letter": "benefit_letter",
    "gig_statement": "gig_statement",
}


@dataclass
class ExtractedField:
    field: str
    value: object
    page: int
    bbox: list
    bbox_units: str
    confidence: float
    source: str  # "auto" | "manual"


@dataclass
class ExtractionResult:
    document_type: Optional[str]
    page_size_points: list
    fields: list = field(default_factory=list)
    untrusted_instruction_text: Optional[str] = None
    contains_adversarial_text: bool = False
    needs_manual_entry: bool = False
    notes: list = field(default_factory=list)
    page_image_png: Optional[bytes] = None


def _color_match(color, target, tol=0.03) -> bool:
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        return False
    return all(abs(a - b) < tol for a, b in zip(color, target))


def _undistorted(char) -> bool:
    matrix = char.get("matrix", (1, 0, 0, 1, 0, 0))
    return abs(matrix[0] - 1.0) < 0.05


def _is_label_char(char) -> bool:
    return (
        "Bold" in char.get("fontname", "")
        and 4 < char.get("size", 0) < 8
        and _color_match(char.get("non_stroking_color"), LABEL_COLOR)
        and _undistorted(char)
    )


def _is_value_char(char) -> bool:
    return _color_match(char.get("non_stroking_color"), VALUE_COLOR) and _undistorted(char)


def _cluster_cells(words, y_tol=3, x_gap=25):
    """Group words into visual rows by top-coordinate, then split each row
    into cells wherever there is a wide horizontal gap (a new form field)."""
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[list] = []
    for w in words:
        for row in rows:
            if abs(row[0]["top"] - w["top"]) <= y_tol:
                row.append(w)
                break
        else:
            rows.append([w])
    cells = []
    for row in rows:
        row = sorted(row, key=lambda w: w["x0"])
        current = [row[0]]
        for w in row[1:]:
            if w["x0"] - current[-1]["x1"] > x_gap:
                cells.append(current)
                current = [w]
            else:
                current.append(w)
        cells.append(current)
    out = []
    for cell in cells:
        text = " ".join(w["text"] for w in cell)
        out.append(
            {
                "text": text,
                "x0": min(w["x0"] for w in cell),
                "x1": max(w["x1"] for w in cell),
                "top": min(w["top"] for w in cell),
                "bottom": max(w["bottom"] for w in cell),
            }
        )
    return out


def _pair_labels_to_values(label_cells, value_cells):
    """Returns a list of (label_cell, value_cell_or_None, dx, dy) -- dx/dy are
    the winning pairing's offsets, used downstream to calibrate confidence."""
    used = set()
    pairs = []
    for label in label_cells:
        best, best_i, best_dist = None, None, None
        best_dx, best_dy = None, None
        for i, value in enumerate(value_cells):
            if i in used or value["top"] <= label["bottom"] - 2:
                continue
            dx = abs(value["x0"] - label["x0"])
            dy = value["top"] - label["bottom"]
            if dx > 20 or dy > 25:
                continue
            dist = dy * 3 + dx
            if best is None or dist < best_dist:
                best, best_i, best_dist = value, i, dist
                best_dx, best_dy = dx, dy
        if best is not None:
            used.add(best_i)
        pairs.append((label, best, best_dx, best_dy))
    return pairs


def _extraction_confidence(dx: float, dy: float, parse_failed: bool) -> float:
    """Calibrate confidence from how tightly the value cell aligns under its
    label (tighter alignment = more likely we grabbed the right cell) and
    whether the value could actually be parsed into its expected type."""
    confidence = max(0.5, min(0.95, 0.95 - dx * 0.01 - dy * 0.01))
    if parse_failed:
        confidence = min(confidence, 0.6)
    return round(confidence, 2)


def _parse_value(raw: str, kind: str):
    raw = raw.strip()
    if kind == "money":
        cleaned = raw.replace("$", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return raw
    if kind == "int":
        try:
            return int(re.sub(r"[^\d-]", "", raw))
        except ValueError:
            return raw
    if kind == "float":
        try:
            return float(re.sub(r"[^\d.\-]", "", raw))
        except ValueError:
            return raw
    return raw


def _bbox_from_cell(cell) -> list:
    return [round(cell["x0"], 2), round(cell["top"], 2), round(cell["x1"], 2), round(cell["bottom"], 2)]


def _detect_document_type(page, filename: str) -> Optional[str]:
    header_words = page.filter(
        lambda o: o.get("object_type") != "char"
        or (o.get("size", 0) >= 9.5 and _color_match(o.get("non_stroking_color"), (1, 1, 1)))
    ).extract_words()
    header_text = " ".join(w["text"] for w in header_words).lower()
    for phrase, doc_type in DOC_TYPE_HEADER_ALIASES.items():
        if phrase in header_text:
            return doc_type
    lower_name = filename.lower()
    for hint, doc_type in FILENAME_HINTS.items():
        if hint in lower_name:
            return doc_type
    return None


def _extract_untrusted_text(page) -> Optional[str]:
    text = page.extract_text() or ""
    marker_idx = text.find(UNTRUSTED_MARKER)
    if marker_idx == -1:
        return None
    tail = text[marker_idx + len(UNTRUSTED_MARKER):]
    tail = tail.lstrip(" -—\n")
    # Stop at the fixture footer line if present.
    footer_idx = tail.find("Fixture ")
    if footer_idx != -1:
        tail = tail[:footer_idx]
    return tail.strip() or None


def draw_highlight(image_png: bytes, bbox: list) -> bytes:
    """Draw a highlight box on a stored page image for a field's bbox (in
    PDF points, top-left origin) -- powers the "view source" link without
    ever showing the coordinates themselves to the applicant."""
    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(image_png)).convert("RGB")
    scale = IMAGE_RESOLUTION / 72
    x0, y0, x1, y1 = (round(c * scale) for c in bbox)
    pad = 4
    ImageDraw.Draw(image).rectangle(
        [x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=(214, 40, 40), width=4
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _render_page_image(page) -> bytes:
    """A plain (unhighlighted) rendering of the page, kept so the applicant
    or a reviewer can see exactly where a value came from later ("view
    source"). This is the only visual record kept -- the uploaded PDF
    itself is still deleted immediately after processing."""
    buffer = io.BytesIO()
    page.to_image(resolution=IMAGE_RESOLUTION).original.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def extract_document(pdf_path: Path, filename: Optional[str] = None) -> ExtractionResult:
    filename = filename or pdf_path.name
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        page_size = [round(page.width, 2), round(page.height, 2)]
        doc_type = _detect_document_type(page, filename)
        untrusted_text = _extract_untrusted_text(page)
        page_image_png = _render_page_image(page)

        if not page.chars:
            return ExtractionResult(
                document_type=doc_type,
                page_size_points=page_size,
                needs_manual_entry=True,
                notes=[
                    "This page has no extractable text layer (scanned image). "
                    "Automatic extraction is not available for this page; please "
                    "enter its values manually."
                ],
                untrusted_instruction_text=untrusted_text,
                contains_adversarial_text=untrusted_text is not None,
                page_image_png=page_image_png,
            )

        label_page = page.filter(lambda o: o.get("object_type") != "char" or _is_label_char(o))
        value_page = page.filter(lambda o: o.get("object_type") != "char" or _is_value_char(o))
        label_cells = _cluster_cells(label_page.extract_words())
        value_cells = _cluster_cells(value_page.extract_words())
        pairs = _pair_labels_to_values(label_cells, value_cells)

        field_map = FIELD_MAP.get(doc_type, {})
        fields = []
        notes = []
        for label_cell, value_cell, dx, dy in pairs:
            label_text = re.sub(r"\s+", " ", label_cell["text"]).strip().upper()
            mapping = field_map.get(label_text)
            if mapping is None:
                continue
            field_name, kind = mapping
            if value_cell is None:
                notes.append(f"Could not locate a value for '{label_text}'.")
                continue
            value = _parse_value(value_cell["text"], kind)
            parse_failed = kind in ("money", "int", "float") and isinstance(value, str)
            confidence = _extraction_confidence(dx, dy, parse_failed)
            if confidence < CONFIDENCE_REVIEW_THRESHOLD:
                notes.append(
                    f"Low confidence ({confidence:.0%}) on '{label_text}' — please double-check this value."
                )
            fields.append(
                ExtractedField(
                    field=field_name,
                    value=value,
                    page=1,
                    bbox=_bbox_from_cell(value_cell),
                    bbox_units="pdf_points_top_left_origin",
                    confidence=confidence,
                    source="auto",
                )
            )

        return ExtractionResult(
            document_type=doc_type,
            page_size_points=page_size,
            fields=fields,
            untrusted_instruction_text=untrusted_text,
            contains_adversarial_text=untrusted_text is not None,
            needs_manual_entry=False,
            notes=notes,
            page_image_png=page_image_png,
        )
