"""Deterministic DOCX and PDF renderer for structured legal drafts.

The renderer is deliberately separate from the LLM: the model supplies only
structured content, while this module controls paper size, margins, fonts,
alignment, numbering, spacing, and output files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt
from docx.oxml.ns import qn

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER, legal
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    PageBreak,
)
from reportlab.pdfgen.canvas import Canvas


HINDI_WORDS = {
    1: "एक", 2: "दो", 3: "तीन", 4: "चार", 5: "पाँच",
    6: "छह", 7: "सात", 8: "आठ", 9: "नौ", 10: "दस",
    11: "ग्यारह", 12: "बारह", 13: "तेरह", 14: "चौदह",
    15: "पंद्रह", 16: "सोलह", 17: "सत्रह", 18: "अठारह",
    19: "उन्नीस", 20: "बीस",
}


def hindi_number(n: int) -> str:
    return HINDI_WORDS.get(n, str(n))


def numbered(text: str, n: int) -> str:
    text = str(text).strip()
    if not text:
        return ""
    # Preserve an existing source/model numbering convention.
    if re.match(r"^\s*\d+\s*(?:\([^)]*\))?\s*[:.)-]?\s*", text):
        return text
    return f"{n} ({hindi_number(n)}) : {text}"


def normalize_between_label(value: str) -> str:
    # Dava/Plaint party separator is a deterministic formatting convention.
    return "बनाम"


def split_party_blocks(parties: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Return plaintiff entries, the deterministic separator, and defendant entries.

    Primary rule:
      * entries explicitly labelled वादी/plaintiff go to the plaintiff block
      * entries explicitly labelled प्रतिवादी/defendant go to the defendant block

    Safe deterministic fallback:
      * when there are exactly two unlabelled party entries, preserve their order
        and treat the first as plaintiff and the second as defendant. This keeps
        the common Dava party order visually correct instead of putting "बनाम"
        after both parties.

    We never use the model's ``between_label`` for the actual separator; the
    renderer always writes the legal convention "बनाम".
    """
    plaintiff, defendant, other = [], [], []

    # First, handle entries that may contain multiple party lines.
    expanded: list[str] = []
    for raw in parties:
        value = str(raw).strip()
        if not value:
            continue
        lines = [line.strip() for line in re.split(r"\\r?\\n", value) if line.strip()]
        expanded.extend(lines or [value])

    for party in expanded:
        low = party.casefold()
        if any(label in low for label in ("वादीगण", "वादी", "plaintiff", "plaintiffs")):
            plaintiff.append(party)
        elif any(label in low for label in ("प्रतिवादीगण", "प्रतिवादी", "defendant", "defendants")):
            defendant.append(party)
        else:
            other.append(party)

    # If the model supplied exactly two unlabelled parties, use the normal
    # Dava ordering rather than rendering both together and placing "बनाम"
    # afterward.
    if not plaintiff and not defendant and len(other) == 2:
        plaintiff = [other[0]]
        defendant = [other[1]]
        other = []

    return plaintiff, defendant, other


def _font_path() -> str:
    bundled = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansDevanagari-Regular.ttf"
    if bundled.exists():
        return str(bundled)
    for p in (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    ):
        if Path(p).exists():
            return p
    raise RuntimeError("Unicode Devanagari font not found.")


def _font_bold_path() -> str:
    bundled = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansDevanagari-Bold.ttf"
    if bundled.exists():
        return str(bundled)
    for p in (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    ):
        if Path(p).exists():
            return p
    return _font_path()

def _register_pdf_fonts() -> tuple[str, str]:
    regular = "VakilDeva"
    bold = "VakilDevaBold"
    if regular not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular, _font_path()))
    if bold not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold, _font_bold_path()))
    return regular, bold


def _set_run_font(run, name: str, size: float, bold: bool = False):
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    # Force both ASCII and complex-script font slots.
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        from docx.oxml import OxmlElement
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
        rfonts.set(qn(f"w:{attr}"), name)


def _setup_docx(document: Document, paper: str):
    # Legal is the default pleading format. Letter remains available by
    # explicitly passing paper="letter".
    paper = str(paper or "legal").strip().lower()
    if paper not in {"legal", "letter"}:
        paper = "legal"

    section = document.sections[0]
    if paper == "legal":
        section.page_width = Inches(8.5)
        section.page_height = Inches(14)
    else:
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)

    section.left_margin = Inches(1.25)
    section.right_margin = Inches(1.25)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.header_distance = Inches(0.5)
    section.footer_distance = Inches(0.5)


def _add_docx_para(
    document: Document,
    text: str,
    *,
    align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    size=14,
    bold=False,
    underline=False,
    first_indent=True,
    left_indent=0,
    after=12,
):
    p = document.add_paragraph()
    p.alignment = align
    pf = p.paragraph_format
    pf.space_after = Pt(after)
    pf.line_spacing = 1.5
    if first_indent:
        pf.first_line_indent = Inches(0.5)
    if left_indent:
        pf.left_indent = Inches(left_indent)

    run = p.add_run(str(text))
    _set_run_font(run, "Noto Sans Devanagari", size, bold=bold)
    run.underline = underline
    return p


def render_docx(draft: dict[str, Any], output_path: str | Path, paper: str = "legal"):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    _setup_docx(doc, paper)

    court = draft.get("court_heading", "").strip()
    case = draft.get("case_heading", "").strip()
    parties = [str(x).strip() for x in draft.get("parties", []) if str(x).strip()]

    if court:
        _add_docx_para(
            doc, court, align=WD_ALIGN_PARAGRAPH.CENTER, size=16,
            bold=True, first_indent=False, after=8,
        )
    if case:
        _add_docx_para(
            doc, case, align=WD_ALIGN_PARAGRAPH.CENTER, size=16,
            bold=True, first_indent=False, after=12,
        )

    # Parties are rendered in strict legal order:
    # plaintiff block -> centered "बनाम" -> defendant block.
    # "बनाम" is deterministic and is NEVER taken from model text.
    if parties:
        plaintiff, defendant, other = split_party_blocks(parties)
        if plaintiff and defendant:
            for party in plaintiff:
                _add_docx_para(
                    doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14,
                    first_indent=False, after=4,
                )
            _add_docx_para(
                doc, "बनाम",
                align=WD_ALIGN_PARAGRAPH.CENTER, size=14,
                bold=True, first_indent=False, after=6,
            )
            for party in defendant:
                _add_docx_para(
                    doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14,
                    first_indent=False, after=4,
                )
            for party in other:
                _add_docx_para(
                    doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14,
                    first_indent=False, after=4,
                )
        else:
            # No reliable labels: preserve source order and do not invent roles.
            for party in parties:
                _add_docx_para(
                    doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14,
                    first_indent=False, after=4,
                )

    sections = draft.get("sections", [])
    seen_keys: set[str] = set()

    # Prefer semantic sections from the model, but don't duplicate court/parties.
    for section in sections:
        key = str(section.get("key", "")).strip().lower()
        if key in {"court", "parties"}:
            continue
        seen_keys.add(key)

        paragraphs = section.get("paragraphs", []) or []
        if not paragraphs:
            continue

        # Use a centered title only for explicit legal headings other than
        # generic internal metadata titles.
        title = str(section.get("title", "")).strip()
        if key not in {"case"} and title:
            # The renderer uses legal-facing titles rather than exposing
            # internal bilingual labels when the content is a main section.
            _add_docx_para(
                doc, title.split(" / ")[0],
                align=WD_ALIGN_PARAGRAPH.CENTER, size=16,
                bold=True, underline=True, first_indent=False, after=8,
            )

        for idx, paragraph in enumerate(paragraphs, 1):
            if key in {"facts", "material_facts", "cause_of_action"}:
                text = numbered(paragraph, idx)
            elif key in {"reliefs", "prayer"}:
                text = numbered(paragraph, idx)
            else:
                text = str(paragraph).strip()

            if not text:
                continue

            _add_docx_para(
                doc,
                text,
                align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                size=14,
                first_indent=True,
                after=12,
            )

    verification = str(draft.get("verification", "") or "").strip()
    if verification:
        _add_docx_para(
            doc, "सत्यापन", align=WD_ALIGN_PARAGRAPH.CENTER,
            size=16, bold=True, underline=True, first_indent=False, after=8,
        )
        for line in verification.splitlines():
            if line.strip():
                _add_docx_para(
                    doc, line.strip(), size=14, first_indent=True, after=12
                )

    signatures = [
        str(x).strip()
        for x in draft.get("signature_block", []) or []
        if str(x).strip()
    ]
    if signatures:
        for line in signatures:
            _add_docx_para(
                doc, line, align=WD_ALIGN_PARAGRAPH.RIGHT, size=14,
                first_indent=False, after=4,
            )

    doc.save(output_path)
    return output_path


def _escape_xml(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _pdf_story(draft: dict[str, Any], font: str, bold_font: str):
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "VakilBody", parent=styles["BodyText"], fontName=font,
        fontSize=14, leading=21, alignment=TA_JUSTIFY,
        shaping=1,
        firstLineIndent=0.5 * inch, spaceAfter=12,
    )
    center16 = ParagraphStyle(
        "VakilCenter16", parent=body, fontName=bold_font,
        fontSize=16, leading=22, alignment=TA_CENTER,
        shaping=1,
        firstLineIndent=0, spaceAfter=8,
    )
    left = ParagraphStyle(
        "VakilLeft", parent=body, alignment=TA_LEFT,
        firstLineIndent=0, spaceAfter=4,
    )
    center = ParagraphStyle(
        "VakilCenter", parent=body, fontName=bold_font,
        alignment=TA_CENTER, firstLineIndent=0, spaceAfter=4, shaping=1,
    )
    right = ParagraphStyle(
        "VakilRight", parent=body, alignment=TA_RIGHT,
        firstLineIndent=0, spaceAfter=4,
    )
    story = []

    court = str(draft.get("court_heading", "")).strip()
    case = str(draft.get("case_heading", "")).strip()
    parties = [str(x).strip() for x in draft.get("parties", []) if str(x).strip()]

    if court:
        story.append(Paragraph(_escape_xml(court), center16))
    if case:
        story.append(Paragraph(_escape_xml(case), center16))
    if parties:
        plaintiff, defendant, other = split_party_blocks(parties)
        if plaintiff and defendant:
            for party in plaintiff:
                story.append(Paragraph(_escape_xml(party), left))
            story.append(Spacer(1, 2))
            story.append(
                Paragraph(
                    _escape_xml("बनाम"),
                    center,
                )
            )
            story.append(Spacer(1, 6))
            for party in defendant:
                story.append(Paragraph(_escape_xml(party), left))
            for party in other:
                story.append(Paragraph(_escape_xml(party), left))
        else:
            for party in parties:
                story.append(Paragraph(_escape_xml(party), left))

    for section in draft.get("sections", []) or []:
        key = str(section.get("key", "")).strip().lower()
        if key in {"court", "parties"}:
            continue
        paragraphs = section.get("paragraphs", []) or []
        if not paragraphs:
            continue

        title = str(section.get("title", "")).strip()
        if title:
            story.append(
                Paragraph(
                    _escape_xml(title.split(" / ")[0]),
                    ParagraphStyle(
                        "SectionTitle",
                        parent=center16,
                        underline=True,
                    ),
                )
            )

        for idx, paragraph in enumerate(paragraphs, 1):
            if key in {"facts", "material_facts", "cause_of_action", "reliefs", "prayer"}:
                text = numbered(paragraph, idx)
            else:
                text = str(paragraph).strip()
            if text:
                story.append(Paragraph(_escape_xml(text), body))

    verification = str(draft.get("verification", "") or "").strip()
    if verification:
        story.append(Paragraph("सत्यापन", center16))
        for line in verification.splitlines():
            if line.strip():
                story.append(Paragraph(_escape_xml(line.strip()), body))

    signatures = [
        str(x).strip()
        for x in draft.get("signature_block", []) or []
        if str(x).strip()
    ]
    for line in signatures:
        story.append(Paragraph(_escape_xml(line), right))

    return story


class NumberedCanvas(Canvas):
    """Adds page numbers without changing the underlying legal content."""

    def __init__(self, *args, **kwargs):
        Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(page_count)
            Canvas.showPage(self)
        Canvas.save(self)

    def draw_page_number(self, page_count: int):
        self.setFont("VakilDeva", 9)
        self.drawCentredString(4.25 * inch, 0.45 * inch, f"{self._pageNumber}")


def render_pdf(draft: dict[str, Any], output_path: str | Path, paper: str = "legal"):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font, bold_font = _register_pdf_fonts()

    paper = str(paper or "legal").strip().lower()
    if paper not in {"legal", "letter"}:
        paper = "legal"

    page_size = legal if paper == "legal" else LETTER
    width, height = page_size
    frame = Frame(
        1.25 * inch, 1 * inch,
        width - 2.5 * inch, height - 2 * inch,
        id="legal_frame",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    doc = BaseDocTemplate(
        str(output_path),
        pagesize=page_size,
        leftMargin=1.25 * inch,
        rightMargin=1.25 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        title="Legal Draft",
        author="Legal Drafting Bot",
    )
    doc.addPageTemplates([PageTemplate(id="legal", frames=[frame])])
    doc.build(
        _pdf_story(draft, font, bold_font),
        canvasmaker=NumberedCanvas,
    )
    return output_path


def render_both(
    draft: dict[str, Any],
    output_dir: str | Path,
    base_name: str = "Dava_Draft",
    paper: str = "legal",
) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", base_name).strip("_") or "Dava_Draft"
    docx_path = output_dir / f"{safe}.docx"
    pdf_path = output_dir / f"{safe}.pdf"
    render_docx(draft, docx_path, paper=paper)
    render_pdf(draft, pdf_path, paper=paper)
    return docx_path, pdf_path
