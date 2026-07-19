"""Renders the packet data app/main.py already assembles (_packet_data)
as a formatted PDF -- a presentation layer over already confirmed/
validated data, not a second calculation path. Every label and value
here comes from the exact same plain-language dictionaries and
formatting helpers Screens 2 and 3 use (app/labels.py via main.py); this
module only lays them out on a page.

Bundles DejaVu Sans (app/fonts/, Bitstream Vera license -- see
DEJAVU-LICENSE.txt) instead of relying on a base-14 PDF font, since
Helvetica's built-in encoding silently mangles the checkmark/warning
glyphs used for status (checked once against pdfplumber's text
extraction -- base-14 turns "✓"/"⚠" into unrelated Latin characters).
"""
import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
pdfmetrics.registerFont(TTFont("DejaVuSans", str(FONTS_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(FONTS_DIR / "DejaVuSans-Bold.ttf")))

FG = "#2b3a2e"
FG_SECONDARY = "#586155"
MUTED = "#7a8577"
ACCENT = "#3e6b4a"
CARD_BG = colors.HexColor("#f3f1e9")
BORDER = colors.HexColor("#e8e4d8")
OK_BG = colors.HexColor("#eaf3de")
WARN_BG = colors.HexColor("#faeeda")
WARN_FG = "#854f0b"

# DejaVu Sans has these glyphs; base-14 PDF fonts silently mangle them
# (verified against pdfplumber text extraction), so no emoji/pictographs.
CHECK = "✓"
WARN = "⚠"

_STATUS_TEXT = {"present": "Present", "missing": "Missing", "expired": "Expired", "extra": "Extra"}
_STATUS_COLOR = {"present": (OK_BG, ACCENT), "missing": (WARN_BG, WARN_FG), "expired": (WARN_BG, WARN_FG), "extra": (CARD_BG, MUTED)}
_STATUS_ICON = {"present": CHECK, "missing": WARN, "expired": WARN, "extra": ""}


def _styles() -> dict:
    return {
        "title": ParagraphStyle("title", fontName="DejaVuSans-Bold", fontSize=18, textColor=colors.HexColor(FG), spaceAfter=2),
        "meta": ParagraphStyle("meta", fontName="DejaVuSans", fontSize=9, textColor=colors.HexColor(MUTED), spaceAfter=2),
        "h2": ParagraphStyle("h2", fontName="DejaVuSans-Bold", fontSize=13, textColor=colors.HexColor(FG), spaceBefore=16, spaceAfter=6),
        "h3": ParagraphStyle("h3", fontName="DejaVuSans-Bold", fontSize=11, textColor=colors.HexColor(FG), spaceBefore=10, spaceAfter=3),
        "body": ParagraphStyle("body", fontName="DejaVuSans", fontSize=10, textColor=colors.HexColor(FG_SECONDARY), leading=14),
        "label": ParagraphStyle("label", fontName="DejaVuSans", fontSize=9.5, textColor=colors.HexColor(MUTED), leading=13),
        "value": ParagraphStyle("value", fontName="DejaVuSans", fontSize=9.5, textColor=colors.HexColor(FG), leading=13),
    }


def _status_badge(status: str, styles: dict) -> Table:
    bg, fg = _STATUS_COLOR[status]
    text = f"{_STATUS_ICON[status]} {_STATUS_TEXT[status]}".strip()
    style = ParagraphStyle("badge", parent=styles["value"], textColor=colors.HexColor(fg), fontName="DejaVuSans-Bold", fontSize=9)
    t = Table([[Paragraph(text, style)]], colWidths=[1.3 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def build_packet_pdf(data: dict) -> bytes:
    styles = _styles()
    buf = io.BytesIO()
    pdf_doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        title=f"{data['household_id']} application packet",
    )
    story = []

    story.append(Paragraph("RealDoor — your application packet", styles["title"]))
    story.append(Paragraph(f"Household: {data['household_id']}", styles["meta"]))
    story.append(Paragraph(f"Prepared: {data['prepared_at']}", styles["meta"]))
    story.append(Spacer(1, 10))

    disclaimer = Table([[Paragraph(data["disclaimer"], styles["body"])]], colWidths=[5.8 * inch])
    disclaimer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(disclaimer)

    s = data["income_summary"]
    if s:
        story.append(Paragraph("Income summary", styles["h2"]))
        story.append(Paragraph(f"Annualized income: <b>{s['annualized_income_display']} a year</b>", styles["body"]))
        if s["threshold"]:
            story.append(Paragraph(
                f"Income limit for a household of {s['threshold']['household_size']}: "
                f"{s['threshold_display']} a year",
                styles["body"],
            ))
        story.append(Paragraph(s["comparison_label"] + ".", styles["body"]))
        story.append(Spacer(1, 4))
        badge_status = "present" if s["readiness_status"] == "READY_TO_REVIEW" else "missing"
        badge_text = f"{CHECK if badge_status == 'present' else WARN} Status: {s['readiness_label']}"
        bg, fg = _STATUS_COLOR[badge_status]
        style = ParagraphStyle("status", parent=styles["value"], textColor=colors.HexColor(fg), fontName="DejaVuSans-Bold")
        t = Table([[Paragraph(badge_text, style)]], colWidths=[2.6 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        if s["reasons"]:
            story.append(Spacer(1, 5))
            for reason in s["reasons"]:
                story.append(Paragraph(f"• {reason}", styles["body"]))

    if data["checklist"]:
        story.append(Paragraph("Required documents", styles["h2"]))
        rows = [[Paragraph(row["label"], styles["value"]), _status_badge(row["status"], styles)] for row in data["checklist"]]
        t = Table(rows, colWidths=[4 * inch, 1.8 * inch])
        t.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t)

    if data["documents"]:
        story.append(Paragraph("Documents included", styles["h2"]))
        for doc in data["documents"]:
            story.append(Paragraph(doc["label"], styles["h3"]))
            rows = []
            for f in doc["fields"]:
                value_text = str(f["value"])
                if f["note"]:
                    value_text += f'<br/><font size="8" color="{MUTED}">{f["note"]}</font>'
                rows.append([Paragraph(f["label"] + ":", styles["label"]), Paragraph(value_text, styles["value"])])
            t = Table(rows, colWidths=[3 * inch, 2.8 * inch])
            t.setStyle(TableStyle([
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(t)
            story.append(Spacer(1, 8))

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", color=BORDER, thickness=0.75))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "This package is only ever viewed, downloaded, or deleted by you. It's never submitted "
        "automatically. Bring it to your appointment or send it to your housing agency yourself.",
        styles["body"],
    ))

    pdf_doc.build(story)
    return buf.getvalue()
