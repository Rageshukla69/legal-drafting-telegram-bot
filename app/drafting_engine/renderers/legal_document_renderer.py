"""Deterministic DOCX and PDF renderer for structured legal drafts.

The renderer is deliberately separate from the LLM: the model supplies only
structured content, while this module controls paper size, margins, fonts,
alignment, numbering, spacing, and output files.

Canonical pipeline
------------------
The DOCX is the single layout source::

    Dava JSON -> python-docx (canonical DOCX) -> LibreOffice -> PDF

so the PDF is a faithful conversion of the document the advocate can edit and
sign. Only one layout engine decides line wrapping, paragraph heights, page
breaks, and Devanagari glyph runs, which is what keeps the two files in step.

The former independent ReportLab PDF layout is retained as
:func:`render_pdf_reportlab`. It is used only when LibreOffice is not available
on the host (or when ``LEGAL_PDF_ENGINE=reportlab`` is set explicitly), because
it can paginate differently from the DOCX and its text layer loses Devanagari
conjuncts to private-use codepoints. Set ``LEGAL_PDF_REQUIRE_CANONICAL=1`` to
fail loudly instead of falling back.

When the fallback *is* used it must still draw Hindi correctly. It therefore

* routes every character to the same font the DOCX uses for it
  (:func:`_font_runs`), so glyph metrics match the canonical document, and
* shapes each font run separately through :mod:`reportlab_shaping`, which
  fixes the ReportLab defect that turned ``(एक)`` into ``(□□)``.

:func:`render_both` reports which engine produced the PDF through its optional
``report`` argument so the bot can tell the advocate when a PDF is not the
canonical DOCX conversion.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
import unicodedata

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

from . import docx_to_pdf
from . import reportlab_shaping


log = logging.getLogger(__name__)

# Engine names recorded in the ``report`` dict of render_both()/render_pdf().
PDF_ENGINE_LIBREOFFICE = "libreoffice"
PDF_ENGINE_REPORTLAB = "reportlab"

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

    The defendant labels are tested first on purpose: "प्रतिवादी" contains the
    substring "वादी", so testing the plaintiff labels first classified labelled
    defendants as plaintiffs and dropped the "बनाम" separator entirely.
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

    defendant_labels = ("प्रतिवादीगण", "प्रतिवादी", "defendant", "defendants")
    plaintiff_labels = ("वादीगण", "वादी", "plaintiff", "plaintiffs")

    for party in expanded:
        low = party.casefold()
        if any(label in low for label in defendant_labels):
            defendant.append(party)
        elif any(label in low for label in plaintiff_labels):
            plaintiff.append(party)
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


def _bundled_font(name: str) -> Path | None:
    candidate = Path(__file__).resolve().parents[1] / "assets" / "fonts" / name
    return candidate if candidate.exists() else None


def _font_path() -> str:
    bundled = _bundled_font("NotoSansDevanagari-Regular.ttf")
    if bundled:
        return str(bundled)
    for p in (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    ):
        if Path(p).exists():
            return p
    raise RuntimeError("Unicode Devanagari font not found.")


def _font_bold_path() -> str:
    bundled = _bundled_font("NotoSansDevanagari-Bold.ttf")
    if bundled:
        return str(bundled)
    for p in (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    ):
        if Path(p).exists():
            return p
    return _font_path()

def _latin_font_path() -> str:
    bundled = _bundled_font("NotoSans-Regular.ttf")
    if bundled:
        return str(bundled)
    # ``assets/fonts/DejaVuSans.ttf`` is the Latin/symbol face the repository
    # already expected to ship; prefer the bundled copy so the renderer never
    # depends on the font packages of the host.
    bundled_dejavu = _bundled_font("DejaVuSans.ttf")
    if bundled_dejavu:
        return str(bundled_dejavu)
    for p in (
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(p).exists():
            return p
    raise RuntimeError("Unicode Latin fallback font not found.")

def _symbol_font_path() -> str:
    bundled = _bundled_font("DejaVuSans.ttf")
    if bundled:
        return str(bundled)
    p = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if Path(p).exists():
        return p
    return _latin_font_path()


@functools.lru_cache(maxsize=None)
def _font_coverage(path: str) -> frozenset[int]:
    """Return the set of Unicode codepoints a font file can actually render.

    Parsing a font is comparatively expensive, and this used to happen three
    times per paragraph. The result is immutable and cached for the life of the
    process.
    """
    from fontTools.ttLib import TTFont as _FTFont

    font = _FTFont(path, lazy=True)
    try:
        coverage: set[int] = set()
        for table in font["cmap"].tables:
            coverage.update(table.cmap.keys())
    finally:
        font.close()
    return frozenset(coverage)


@functools.lru_cache(maxsize=None)
def _font_family_name(path: str) -> str:
    """Return the real family name declared inside a font file.

    The DOCX must name the font that is actually going to be on the machine
    performing layout; naming a family we do not ship makes Word/LibreOffice
    silently substitute and then the DOCX and PDF drift apart again.
    """
    from fontTools.ttLib import TTFont as _FTFont

    font = _FTFont(path, lazy=True)
    try:
        for record in font["name"].names:
            if record.nameID == 1 and record.platformID == 3:
                return str(record.toUnicode())
        for record in font["name"].names:
            if record.nameID == 1:
                return str(record.toUnicode())
    finally:
        font.close()
    return Path(path).stem

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


def _is_devanagari(cp: int) -> bool:
    """Return True for Devanagari letters/marks used by Hindi text."""
    return (
        0x0900 <= cp <= 0x097F
        or 0xA8E0 <= cp <= 0xA8FF
        or 0x11B00 <= cp <= 0x11B5F
    )


# Font slots shared by the DOCX and the fallback PDF. The DOCX names real font
# families; the PDF maps the same slots onto its registered ReportLab fonts.
FONT_SLOT_PRIMARY = "primary"
FONT_SLOT_LATIN = "latin"
FONT_SLOT_SYMBOL = "symbol"


def _font_runs(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(slot, chunk)`` runs, one font per run.

    This is the *same* decision procedure the DOCX renderer applies in
    :func:`_add_docx_para`, evaluated against the same bundled font files:

    * a character covered by Noto Sans Devanagari stays in the primary slot
      (this includes ASCII digits and punctuation such as ``1``, ``(``, ``,``,
      ``:``, so ``1 (एक) :`` is a single run, exactly as in the DOCX);
    * otherwise the Latin face, otherwise the symbol face;
    * format characters and combining marks (ZWJ/ZWNJ, nukta, candrabindu …)
      never start a new run — they must stay in the run of their base letter or
      the shaper cannot form the cluster;
    * a character no bundled font can draw is dropped rather than rendered as
      a tofu box.

    Keeping the PDF fallback on the same routing as the DOCX means the same
    glyphs, and therefore the same advance widths, are used for the same
    characters, which keeps line wrapping as close to the canonical document as
    an independent layout engine allows.
    """
    _primary, _latin, _symbol, coverages = _docx_font_plan()
    runs: list[tuple[str, str]] = []
    current: str | None = None
    buf: list[str] = []
    for ch in str(text):
        cp = ord(ch)
        if cp in coverages["primary"]:
            slot = FONT_SLOT_PRIMARY
        elif cp in coverages["latin"]:
            slot = FONT_SLOT_LATIN
        elif cp in coverages["symbol"]:
            slot = FONT_SLOT_SYMBOL
        elif unicodedata.category(ch) in {"Cf", "Mn", "Me"}:
            slot = current or FONT_SLOT_PRIMARY
        else:
            continue
        if slot != current and buf:
            runs.append((current or FONT_SLOT_PRIMARY, "".join(buf)))
            buf = []
        current = slot
        buf.append(ch)
    if buf:
        runs.append((current or FONT_SLOT_PRIMARY, "".join(buf)))
    return runs


def _pdf_inline_font_markup(text: str, primary: str, fallback: str, symbols: str) -> str:
    """Create ReportLab inline-font markup that mirrors the DOCX font runs.

    ``primary`` runs carry no tag so they inherit the paragraph style's font
    (regular or bold Devanagari); Latin and symbol runs are wrapped in
    ``<font name=...>``.

    Devanagari is never split character by character: a Hindi word is one run,
    and ASCII punctuation or digits adjacent to it stay in the same run because
    Noto Sans Devanagari covers them. A word can still legitimately contain a
    font switch (``Section-धारा``, ``₹10,00,000`` in a symbol-less draft), which
    is exactly the case ReportLab's own shaper mishandled; the corrected shaper
    in :mod:`reportlab_shaping` shapes each run with its own font, so such
    words render correctly too.

    The result is still Unicode text; no transliteration or content rewriting
    occurs here.
    """
    names = {
        FONT_SLOT_PRIMARY: primary,
        FONT_SLOT_LATIN: fallback,
        FONT_SLOT_SYMBOL: symbols,
    }
    out: list[str] = []
    for slot, chunk in _font_runs(text):
        escaped = _escape_xml(chunk)
        if slot == FONT_SLOT_PRIMARY:
            out.append(escaped)
        else:
            out.append(f'<font name="{names[slot]}">{escaped}</font>')
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


def _docx_font_plan() -> tuple[str, str, str, dict[str, frozenset[int]]]:
    """Resolve the Devanagari / Latin / symbol fonts and their real names."""
    primary_path = _font_path()
    latin_path = _latin_font_path()
    symbol_path = _symbol_font_path()
    coverages = {
        "primary": _font_coverage(primary_path),
        "latin": _font_coverage(latin_path),
        "symbol": _font_coverage(symbol_path),
    }
    return (
        _font_family_name(primary_path),
        _font_family_name(latin_path),
        _font_family_name(symbol_path),
        coverages,
    )


def _add_docx_page_number_footer(document: Document):
    """Put the page number in the DOCX footer as a real ``PAGE`` field.

    The legacy ReportLab PDF drew its own page number. Now that the PDF is a
    conversion of the canonical DOCX, the page number must live in the DOCX too,
    otherwise the two files would still differ on every page.
    """
    from docx.oxml import OxmlElement

    section = document.sections[0]
    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    for existing in list(paragraph.runs):
        existing._element.getparent().remove(existing._element)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    run = paragraph.add_run()
    _set_run_font(run, _font_family_name(_font_path()), 9)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    result = OxmlElement("w:t")
    result.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instruction, separate, result, end):
        run._element.append(element)
    return paragraph


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

    _add_docx_page_number_footer(document)


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
    # reliable across Android viewers, LibreOffice and Microsoft Word. Font
    # metrics are cached across calls (a legal draft has many paragraphs).
    primary, latin, symbol, coverages = _docx_font_plan()

    runs = []
    current_font = None
    buf = []
    for ch in text:
        cp = ord(ch)
        if cp in coverages["primary"]:
            font_name = primary
        elif cp in coverages["latin"]:
            font_name = latin
        elif cp in coverages["symbol"]:
            font_name = symbol
        elif unicodedata.category(ch) in {"Cf", "Mn", "Me"}:
            font_name = current_font or primary
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


def render_pdf_reportlab(draft: dict[str, Any], output_path: str | Path, paper: str = "legal"):
    """Legacy PDF layout, produced independently of the DOCX by ReportLab.

    Retained only as a fallback for hosts without LibreOffice and for the
    historical regression tests. Prefer :func:`render_pdf`, which converts the
    canonical DOCX instead.

    Devanagari is shaped with HarfBuzz through the corrected per-font shaper in
    :mod:`reportlab_shaping`; without it ReportLab draws ``(एक)`` as ``(□□)``.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font, bold_font, latin_font, symbol_font = _register_pdf_fonts()
    # Refuse to draw unshaped Devanagari: install the fixed shaper (raises a
    # clear error when uharfbuzz is missing or ReportLab changed internally).
    reportlab_shaping.install()
    if not pdfmetrics.getFont(font).shapable:
        raise reportlab_shaping.ShapingUnavailable(
            f"ReportLab font {font!r} is not shapable; Devanagari would render unshaped."
        )

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


def pdf_engine() -> str:
    """Return the configured PDF engine: ``canonical`` (default) or ``reportlab``."""
    engine = (os.getenv("LEGAL_PDF_ENGINE", "canonical") or "canonical").strip().lower()
    return engine if engine in {"canonical", "reportlab"} else "canonical"


def _canonical_pdf_required() -> bool:
    return (os.getenv("LEGAL_PDF_REQUIRE_CANONICAL", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def canonical_pdf_available() -> bool:
    """True when the PDF can be produced from the canonical DOCX."""
    return pdf_engine() == "canonical" and docx_to_pdf.converter_available()


def pdf_pipeline_status() -> dict[str, Any]:
    """Describe how PDFs will be produced on this host (for logs/diagnostics).

    Nothing here renders a document; it only reports the configuration so an
    operator can see *before* the first draft whether the PDF will be the
    canonical LibreOffice conversion of the DOCX or the ReportLab fallback.
    """
    soffice = docx_to_pdf.find_soffice()
    engine_setting = pdf_engine()
    fonts = {
        "devanagari_regular": _font_path(),
        "devanagari_bold": _font_bold_path(),
        "latin": _latin_font_path(),
        "symbol": _symbol_font_path(),
    }
    if engine_setting == "reportlab":
        expected = PDF_ENGINE_REPORTLAB
        reason = "LEGAL_PDF_ENGINE=reportlab forces the legacy independent layout."
    elif soffice is None:
        expected = PDF_ENGINE_REPORTLAB
        reason = (
            "LibreOffice (soffice) was not found, so the PDF cannot be converted from the "
            "canonical DOCX. On Heroku add the heroku-community/apt buildpack (Aptfile "
            "installs libreoffice-writer) or set SOFFICE_BIN."
        )
    else:
        expected = PDF_ENGINE_LIBREOFFICE
        reason = "PDF is converted from the canonical DOCX by LibreOffice."
    return {
        "engine_setting": engine_setting,
        "expected_engine": expected,
        "canonical": expected == PDF_ENGINE_LIBREOFFICE,
        "require_canonical": _canonical_pdf_required(),
        "soffice": soffice,
        "fonts": fonts,
        "uharfbuzz": reportlab_shaping.uharfbuzz is not None,
        "reason": reason,
    }


def _fallback_report(report: dict[str, Any], reason: str) -> None:
    report["engine"] = PDF_ENGINE_REPORTLAB
    report["canonical"] = False
    report["warning"] = reason


def _write_pdf(
    draft: dict[str, Any],
    pdf_path: str | Path,
    paper: str,
    docx_path: str | Path | None = None,
    report: dict[str, Any] | None = None,
) -> Path:
    """Write the PDF, preferring a conversion of the canonical DOCX.

    ``report`` (optional dict) is filled with ``engine`` (``"libreoffice"`` or
    ``"reportlab"``), ``canonical`` (bool) and ``warning`` (str or None) so the
    caller can tell the user when the PDF is not the canonical conversion.
    """
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if report is None:
        report = {}
    report.update({"engine": None, "canonical": False, "warning": None, "soffice": None})

    if pdf_engine() == "reportlab":
        log.info("LEGAL_PDF_ENGINE=reportlab: using the legacy independent PDF layout.")
        _fallback_report(report, "LEGAL_PDF_ENGINE=reportlab forces the legacy PDF layout.")
        return render_pdf_reportlab(draft, pdf_path, paper=paper)

    soffice = docx_to_pdf.find_soffice()
    if soffice is None:
        message = (
            "LibreOffice (soffice) is not installed, so the PDF cannot be converted from "
            "the canonical DOCX."
        )
        if _canonical_pdf_required():
            raise docx_to_pdf.PdfConverterUnavailable(message)
        log.warning(
            "%s Falling back to the legacy ReportLab layout. That layout is produced "
            "independently of the DOCX, so pagination may differ and Devanagari text "
            "extraction is not reliable. Install LibreOffice, or set SOFFICE_BIN to a "
            "LibreOffice binary, to restore DOCX/PDF parity.",
            message,
        )
        _fallback_report(report, message)
        return render_pdf_reportlab(draft, pdf_path, paper=paper)

    try:
        if docx_path is not None and Path(docx_path).is_file():
            result = docx_to_pdf.convert(docx_path, pdf_path)
        else:
            # No canonical DOCX was supplied, so build one solely for conversion.
            with tempfile.TemporaryDirectory(prefix="legal-canonical-") as tmp:
                staged = Path(tmp) / "canonical.docx"
                render_docx(draft, staged, paper=paper)
                result = docx_to_pdf.convert(staged, pdf_path)
    except docx_to_pdf.PdfConversionError as exc:
        log.exception("Canonical DOCX->PDF conversion failed")
        if _canonical_pdf_required():
            raise
        log.warning("Falling back to the legacy ReportLab PDF layout for this document.")
        _fallback_report(
            report,
            f"LibreOffice conversion of the canonical DOCX failed ({exc}); the legacy layout was used.",
        )
        return render_pdf_reportlab(draft, pdf_path, paper=paper)

    report.update({"engine": PDF_ENGINE_LIBREOFFICE, "canonical": True, "soffice": soffice})
    return result


def render_pdf(
    draft: dict[str, Any],
    output_path: str | Path,
    paper: str = "legal",
    report: dict[str, Any] | None = None,
):
    """Render the PDF for ``draft``.

    The PDF is produced from the canonical DOCX, so the interface is unchanged
    while the layout source is now shared with :func:`render_docx`.
    """
    return _write_pdf(draft, output_path, paper, report=report)


def render_both(
    draft: dict[str, Any],
    output_dir: str | Path,
    base_name: str = "Dava_Draft",
    paper: str = "legal",
    report: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Render the DOCX and the PDF, with the PDF converted from that DOCX.

    Pass a dict as ``report`` to learn which engine produced the PDF (see
    :func:`_write_pdf`).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", base_name).strip("_") or "Dava_Draft"
    docx_path = output_dir / f"{safe}.docx"
    pdf_path = output_dir / f"{safe}.pdf"
    render_docx(draft, docx_path, paper=paper)
    _write_pdf(draft, pdf_path, paper=paper, docx_path=docx_path, report=report)
    return docx_path, pdf_path
