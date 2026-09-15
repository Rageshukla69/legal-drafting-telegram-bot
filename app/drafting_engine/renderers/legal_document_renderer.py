"""Deterministic DOCX and PDF renderer for structured legal drafts.

The renderer is deliberately separate from the LLM: the model supplies only
structured content, while this module controls paper size, margins, fonts,
alignment, numbering, spacing, and output files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
import unicodedata

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt
from docx.oxml.ns import qn
from ..legal_number_formatter import format_draft_numbers

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
    # The model is forbidden from numbering. As a defensive measure, remove
    # one accidental leading numeric/Hindi-letter prefix before rendering.
    text = re.sub(r"^\s*\d+\s*(?:\([^)]*\))?\s*[:.)-]?\s*", "", text)
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

def _latin_font_path() -> str:
    bundled = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSans-Regular.ttf"
    if bundled.exists():
        return str(bundled)
    for p in (
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(p).exists():
            return p
    raise RuntimeError("Unicode Latin fallback font not found.")

def _symbol_font_path() -> str:
    bundled = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "DejaVuSans.ttf"
    if bundled.exists():
        return str(bundled)
    p = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if Path(p).exists():
        return p
    return _latin_font_path()

def _register_pdf_fonts() -> tuple[str, str, str, str]:
    regular = "VakilDeva"
    bold = "VakilDevaBold"
    latin = "VakilLatin"
    symbols = "VakilSymbols"
    if regular not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular, _font_path()))
    if bold not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold, _font_bold_path()))
    if latin not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(latin, _latin_font_path()))
    if symbols not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(symbols, _symbol_font_path()))
    return regular, bold, latin, symbols


def _pdf_font_coverage(font_name: str) -> set[int]:
    font = pdfmetrics.getFont(font_name)
    return set(getattr(font.face, "charWidths", {}).keys())


def _pdf_inline_font_markup(text: str, primary: str, fallback: str, symbols: str) -> str:
    """Create shaping-safe contiguous font runs.

    Devanagari is a complex script. Splitting it character-by-character (or
    switching fonts for every punctuation mark) can break HarfBuzz shaping and
    produce cramped/malformed glyphs in PDF viewers. Keep complete Devanagari
    runs together and switch only for actual Latin/numeric runs or symbols.
    """
    primary_cov = _pdf_font_coverage(primary)
    fallback_cov = _pdf_font_coverage(fallback)
    symbol_cov = _pdf_font_coverage(symbols)
    out: list[str] = []
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current, buf
        if not buf or current is None:
            return
        escaped = _escape_xml("".join(buf))
        if current == primary:
            out.append(escaped)
        else:
            out.append(f'<font name="{current}">{escaped}</font>')
        buf = []

    def choose_font(ch: str) -> str | None:
        cp = ord(ch)
        # Keep Devanagari letters and combining marks in one continuous run.
        # Spaces/common punctuation remain with the surrounding Hindi run.
        if (0x0900 <= cp <= 0x097F) or unicodedata.category(ch) in {"Mn", "Mc", "Me"}:
            return primary if cp in primary_cov else (fallback if cp in fallback_cov else symbols if cp in symbol_cov else None)
        if ch.isspace():
            return current or primary
        # ASCII letters/digits are deliberately routed to Noto Sans. This keeps
        # A/B/C/D map labels and legal identifiers readable without disrupting
        # the surrounding Devanagari shaping run.
        if cp < 128 and ch.isalnum():
            return fallback if cp in fallback_cov else primary if cp in primary_cov else symbols if cp in symbol_cov else None
        # Keep ordinary punctuation with the current script whenever possible.
        if unicodedata.category(ch).startswith("P"):
            return current or (primary if cp in primary_cov else fallback if cp in fallback_cov else symbols if cp in symbol_cov else None)
        if cp in primary_cov:
            return primary
        if cp in fallback_cov:
            return fallback
        if cp in symbol_cov:
            return symbols
        return None

    for ch in str(text):
        chosen = choose_font(ch)
        if chosen is None:
            continue
        if chosen != current:
            flush()
            current = chosen
        buf.append(ch)
    flush()
    return "".join(out)


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

    text = str(text)
    # Word can perform font fallback, but explicit run-level fallback is more
    # reliable across Android viewers, LibreOffice and Microsoft Word.
    primary = _font_path()
    latin = _latin_font_path()
    symbol = _symbol_font_path()
    from fontTools.ttLib import TTFont as _FTFont
    coverages = {}
    for key, path in (("primary", primary), ("latin", latin), ("symbol", symbol)):
        ft = _FTFont(path, lazy=True)
        cmap = set()
        for table in ft["cmap"].tables:
            cmap.update(table.cmap.keys())
        coverages[key] = cmap
        ft.close()

    runs = []
    current_font = None
    buf = []
    for ch in text:
        cp = ord(ch)
        if cp < 128 and (ch.isalnum() or unicodedata.category(ch).startswith("P")):
            font_name = "Noto Sans"
        elif cp in coverages["primary"]:
            font_name = "Noto Sans Devanagari"
        elif cp in coverages["latin"]:
            font_name = "Noto Sans"
        elif cp in coverages["symbol"]:
            font_name = "DejaVu Sans"
        elif unicodedata.category(ch) in {"Cf", "Mn", "Me"}:
            font_name = current_font or "Noto Sans Devanagari"
        else:
            continue
        if font_name != current_font and buf:
            runs.append((current_font, "".join(buf)))
            buf = []
        current_font = font_name
        buf.append(ch)
    if buf:
        runs.append((current_font, "".join(buf)))
    for font_name, chunk in runs:
        run = p.add_run(chunk)
        _set_run_font(run, font_name, size, bold=bold)
        run.underline = underline
    return p


LAYOUT_SECTIONS = {"court_heading","case_heading","parties","title","opening_averment","pleadings","prayer","signature_block","verification"}

def _layout_break(draft: dict[str, Any], kind: str, section: str) -> bool:
    layout = draft.get("layout", {}) or {}
    if not isinstance(layout, dict):
        return False
    values = layout.get(kind, []) or []
    if not isinstance(values, list):
        values = [values]
    return section in values

def _docx_page_break_if_needed(doc: Document, draft: dict[str, Any], section: str, *, before: bool = True):
    if _layout_break(draft, "page_break_before" if before else "page_break_after", section):
        doc.add_page_break()


def _ordered_pleading_items(draft: dict[str, Any]) -> list[str]:
    """Return the single approved pleading sequence.

    Phase 7.1 intentionally does not flatten separate cause/jurisdiction/etc.
    arrays because doing so caused duplicated or restarted numbering. The AI
    structural planner decides the order before drafting.
    """
    return [str(x).strip() for x in (draft.get("pleadings", []) or []) if str(x).strip()]


def _title_text(draft: dict[str, Any]) -> str:
    title = str(draft.get("title", "")).strip()
    if title and title != "वाद पत्र":
        return title
    return "वाद पत्र"


def _signature_lines(draft: dict[str, Any]) -> list[str]:
    lines = [str(x).strip() for x in draft.get("signature_block", []) or [] if str(x).strip()]
    if lines:
        return lines
    # Fixed advocate profile for this project. It is presentation metadata,
    # not a case fact, and therefore is inserted deterministically only when
    # the draft does not already contain a signature block.
    plaintiff = ""
    for party in draft.get("parties", []) or []:
        if "वादी" in str(party):
            plaintiff = str(party).strip()
            break
    if plaintiff:
        return [f"वादी\n{plaintiff}", "द्वारा अधिवक्ता-", "वी०डी० शुक्ला एडवोकेट", "बिधूना, औरैया"]
    return ["वादी", "द्वारा अधिवक्ता-", "वी०डी० शुक्ला एडवोकेट", "बिधूना, औरैया"]


def render_docx(draft: dict[str, Any], output_path: str | Path, paper: str = "legal"):
    draft = format_draft_numbers(draft)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    _setup_docx(doc, paper)

    court = str(draft.get("court_heading", "")).strip()
    case = str(draft.get("case_heading", "")).strip()
    parties = [str(x).strip() for x in draft.get("parties", []) if str(x).strip()]

    _docx_page_break_if_needed(doc, draft, "court_heading")
    if court:
        _add_docx_para(doc, court, align=WD_ALIGN_PARAGRAPH.CENTER, size=16, bold=True, first_indent=False, after=8)
    if case:
        _docx_page_break_if_needed(doc, draft, "case_heading")
        _add_docx_para(doc, case, align=WD_ALIGN_PARAGRAPH.CENTER, size=14, bold=True, first_indent=False, after=10)

    _docx_page_break_if_needed(doc, draft, "parties")
    plaintiff, defendant, other = split_party_blocks(parties)
    if plaintiff and defendant:
        for party in plaintiff:
            _add_docx_para(doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14, first_indent=False, after=3)
        _add_docx_para(doc, "बनाम", align=WD_ALIGN_PARAGRAPH.CENTER, size=14, bold=True, first_indent=False, after=3)
        for party in defendant:
            _add_docx_para(doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14, first_indent=False, after=4)
        for party in other:
            _add_docx_para(doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14, first_indent=False, after=4)
    else:
        for party in parties:
            _add_docx_para(doc, party, align=WD_ALIGN_PARAGRAPH.LEFT, size=14, first_indent=False, after=4)

    _docx_page_break_if_needed(doc, draft, "title")
    _add_docx_para(doc, _title_text(draft), align=WD_ALIGN_PARAGRAPH.CENTER, size=16, bold=True, underline=True, first_indent=False, after=10)

    opening = str(draft.get("opening_averment", "")).strip()
    if opening:
        _docx_page_break_if_needed(doc, draft, "opening_averment")
        _add_docx_para(doc, opening, align=WD_ALIGN_PARAGRAPH.JUSTIFY, size=14, first_indent=False, after=10)

    _docx_page_break_if_needed(doc, draft, "pleadings")
    for idx, paragraph in enumerate(_ordered_pleading_items(draft), 1):
        _add_docx_para(doc, numbered(paragraph, idx), align=WD_ALIGN_PARAGRAPH.JUSTIFY, size=14, first_indent=True, after=12)

    prayer = [str(x).strip() for x in draft.get("prayer", []) or [] if str(x).strip()]
    if prayer:
        _docx_page_break_if_needed(doc, draft, "prayer")
        _add_docx_para(doc, "प्रार्थना", align=WD_ALIGN_PARAGRAPH.CENTER, size=16, bold=True, underline=True, first_indent=False, after=8)
        _add_docx_para(doc, "अतः वादी माननीय न्यायालय से प्रार्थना करता है कि:-", align=WD_ALIGN_PARAGRAPH.JUSTIFY, size=14, first_indent=False, after=10)
        for idx, item in enumerate(prayer):
            # Prayer items use Hindi-lettered clauses where practical.
            labels = ["(क)", "(ख)", "(ग)", "(घ)", "(ङ)", "(च)"]
            label = labels[idx] if idx < len(labels) else f"({idx + 1})"
            _add_docx_para(doc, f"{label} {item}", align=WD_ALIGN_PARAGRAPH.JUSTIFY, size=14, first_indent=False, after=10)

    signatures = _signature_lines(draft)
    if signatures:
        _docx_page_break_if_needed(doc, draft, "signature_block")
        for line in signatures:
            _add_docx_para(doc, line, align=WD_ALIGN_PARAGRAPH.RIGHT, size=14, first_indent=False, after=4)

    verification = str(draft.get("verification", "") or "").strip()
    if verification:
        _docx_page_break_if_needed(doc, draft, "verification")
        _add_docx_para(doc, "सत्यापन", align=WD_ALIGN_PARAGRAPH.CENTER, size=16, bold=True, underline=True, first_indent=False, after=8)
        for line in verification.splitlines():
            if line.strip():
                _add_docx_para(doc, line.strip(), align=WD_ALIGN_PARAGRAPH.JUSTIFY, size=14, first_indent=True, after=10)

    doc.save(output_path)
    return output_path


def _escape_xml(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _pdf_story(draft: dict[str, Any], font: str, bold_font: str, latin_font: str, symbol_font: str):
    styles = getSampleStyleSheet()
    body = ParagraphStyle("VakilBody", parent=styles["BodyText"], fontName=font, fontSize=14, leading=21, alignment=TA_JUSTIFY, shaping=1, firstLineIndent=0.5 * inch, spaceAfter=12)
    center16 = ParagraphStyle("VakilCenter16", parent=body, fontName=bold_font, fontSize=16, leading=22, alignment=TA_CENTER, shaping=1, firstLineIndent=0, spaceAfter=8)
    left = ParagraphStyle("VakilLeft", parent=body, alignment=TA_LEFT, firstLineIndent=0, spaceAfter=3)
    center = ParagraphStyle("VakilCenter", parent=body, fontName=bold_font, alignment=TA_CENTER, firstLineIndent=0, spaceAfter=3, shaping=1)
    right = ParagraphStyle("VakilRight", parent=body, alignment=TA_RIGHT, firstLineIndent=0, spaceAfter=4)
    noindent = ParagraphStyle("VakilNoIndent", parent=body, firstLineIndent=0, spaceAfter=10)
    story = []

    court = str(draft.get("court_heading", "")).strip()
    case = str(draft.get("case_heading", "")).strip()
    parties = [str(x).strip() for x in draft.get("parties", []) if str(x).strip()]
    if _layout_break(draft, "page_break_before", "court_heading"):
        story.append(PageBreak())
    if court:
        story.append(Paragraph(_pdf_inline_font_markup(court, font, latin_font, symbol_font), center16))
    if case:
        if _layout_break(draft, "page_break_before", "case_heading"):
            story.append(PageBreak())
        story.append(Paragraph(_pdf_inline_font_markup(case, font, latin_font, symbol_font), ParagraphStyle("Case", parent=center16, fontSize=14, leading=20, spaceAfter=10)))

    if _layout_break(draft, "page_break_before", "parties"):
        story.append(PageBreak())
    plaintiff, defendant, other = split_party_blocks(parties)
    if plaintiff and defendant:
        for party in plaintiff:
            story.append(Paragraph(_pdf_inline_font_markup(party, font, latin_font, symbol_font), left))
        story.append(Paragraph(_pdf_inline_font_markup("बनाम", font, latin_font, symbol_font), center))
        for party in defendant:
            story.append(Paragraph(_pdf_inline_font_markup(party, font, latin_font, symbol_font), left))
        for party in other:
            story.append(Paragraph(_pdf_inline_font_markup(party, font, latin_font, symbol_font), left))
    else:
        for party in parties:
            story.append(Paragraph(_pdf_inline_font_markup(party, font, latin_font, symbol_font), left))

    if _layout_break(draft, "page_break_before", "title"):
        story.append(PageBreak())
    story.append(Paragraph(_pdf_inline_font_markup(_title_text(draft), font, latin_font, symbol_font), ParagraphStyle("Title", parent=center16, underline=True, spaceBefore=4, spaceAfter=10)))

    opening = str(draft.get("opening_averment", "")).strip()
    if opening:
        if _layout_break(draft, "page_break_before", "opening_averment"):
            story.append(PageBreak())
        story.append(Paragraph(_pdf_inline_font_markup(opening, font, latin_font, symbol_font), noindent))

    if _layout_break(draft, "page_break_before", "pleadings"):
        story.append(PageBreak())
    for idx, paragraph in enumerate(_ordered_pleading_items(draft), 1):
        story.append(Paragraph(_pdf_inline_font_markup(numbered(paragraph, idx), font, latin_font, symbol_font), body))

    prayer = [str(x).strip() for x in draft.get("prayer", []) or [] if str(x).strip()]
    if prayer:
        if _layout_break(draft, "page_break_before", "prayer"):
            story.append(PageBreak())
        story.append(Paragraph("प्रार्थना", ParagraphStyle("PrayerTitle", parent=center16, underline=True, spaceBefore=2, spaceAfter=8)))
        story.append(Paragraph(_pdf_inline_font_markup("अतः वादी माननीय न्यायालय से प्रार्थना करता है कि:-", font, latin_font, symbol_font), noindent))
        labels = ["(क)", "(ख)", "(ग)", "(घ)", "(ङ)", "(च)"]
        for idx, item in enumerate(prayer):
            label = labels[idx] if idx < len(labels) else f"({idx + 1})"
            story.append(Paragraph(_pdf_inline_font_markup(f"{label} {item}", font, latin_font, symbol_font), ParagraphStyle(f"Prayer{idx}", parent=body, firstLineIndent=0, spaceAfter=10)))

    if _layout_break(draft, "page_break_before", "signature_block"):
        story.append(PageBreak())
    for line in _signature_lines(draft):
        story.append(Paragraph(_pdf_inline_font_markup(line, font, latin_font, symbol_font), right))

    verification = str(draft.get("verification", "") or "").strip()
    if verification:
        if _layout_break(draft, "page_break_before", "verification"):
            story.append(PageBreak())
        story.append(Paragraph("सत्यापन", ParagraphStyle("VerificationTitle", parent=center16, underline=True, spaceBefore=8, spaceAfter=8)))
        for line in verification.splitlines():
            if line.strip():
                story.append(Paragraph(_pdf_inline_font_markup(line.strip(), font, latin_font, symbol_font), body))
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
    draft = format_draft_numbers(draft)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font, bold_font, latin_font, symbol_font = _register_pdf_fonts()

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
        _pdf_story(draft, font, bold_font, latin_font, symbol_font),
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
