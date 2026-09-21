"""Regression: Hindi glyphs must survive PDF generation.

The DOCX is canonical. These tests fail if the PDF font is missing, if
Unicode is replaced with □ / U+FFFD, or if required Devanagari words are
absent from the PDF text layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.drafting_engine.renderers import docx_to_pdf
from app.drafting_engine.renderers import legal_document_renderer as ldr
from dava_fixtures import TEST_1_SHORT, TEST_5_MIXED

REQUIRED_WORDS = [
    "न्यायालय",
    "श्रीमान",
    "सिविल",
    "जज",
    "जूनियर",
    "डिवीजन",
    "बिधूना",
    "एक",
    "दो",
    "तीन",
    "चार",
    "पाँच",
    "छह",
    "क",
    "ख",
    "ग",
    "प्रार्थना",
    "सत्यापन",
]


def _pdf_text(path: Path) -> str:
    import fitz

    with fitz.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


def _assert_hindi_layer(pdf_path: Path) -> str:
    assert pdf_path.is_file() and pdf_path.stat().st_size > 1000
    text = _pdf_text(pdf_path)
    assert "□" not in text
    assert "\ufffd" not in text
    assert not any(0xE000 <= ord(ch) <= 0xF8FF for ch in text), "private-use glyphs in PDF text layer"
    squashed = "".join(text.split())
    for word in REQUIRED_WORDS:
        assert word in squashed, f"missing Hindi word from PDF: {word!r}"
    return text


def test_bundled_devanagari_font_exists_and_covers_required_codepoints():
    path = Path(ldr._font_path())
    assert path.is_file(), f"Devanagari font missing: {path}"
    coverage = ldr._font_coverage(str(path))
    sample = "".join(REQUIRED_WORDS)
    missing = [ch for ch in sample if ord(ch) not in coverage]
    assert missing == [], f"font lacks glyphs for {missing!r}"


def test_pdf_font_registration_uses_bundled_ttf():
    regular, bold, latin, symbols = ldr._register_pdf_fonts()
    assert regular == "VakilDeva"
    assert Path(ldr._font_path()).name == "NotoSansDevanagari-Regular.ttf"
    assert Path(ldr._font_bold_path()).name == "NotoSansDevanagari-Bold.ttf"
    from reportlab.pdfbase import pdfmetrics

    assert regular in pdfmetrics.getRegisteredFontNames()
    face = pdfmetrics.getFont(regular).face
    assert 0x0905 in face.charWidths  # अ
    assert 0x0915 in face.charWidths  # क


def test_reportlab_fallback_pdf_contains_required_hindi(tmp_path):
    pdf_path = tmp_path / "glyphs_reportlab.pdf"
    ldr.render_pdf_reportlab(TEST_5_MIXED, pdf_path, paper="legal")
    _assert_hindi_layer(pdf_path)
    import fitz

    with fitz.open(str(pdf_path)) as doc:
        assert doc.page_count >= 1
        names = {font[3] for page in doc for font in page.get_fonts()}
        assert any("NotoSansDevanagari" in n for n in names), names
        for page in doc:
            assert page.get_text().strip(), "page has no text layer"


def test_render_pdf_contains_required_hindi(tmp_path):
    pdf_path = tmp_path / "glyphs_render.pdf"
    ldr.render_pdf(TEST_5_MIXED, pdf_path, paper="legal")
    _assert_hindi_layer(pdf_path)


def test_short_draft_pdf_has_numbered_hindi_and_prayer_letters(tmp_path):
    pdf_path = tmp_path / "short.pdf"
    ldr.render_pdf(TEST_1_SHORT, pdf_path, paper="legal")
    text = _pdf_text(pdf_path)
    assert "□" not in text and "\ufffd" not in text
    squashed = "".join(text.split())
    for word in ("न्यायालय", "श्रीमान", "बिधूना", "प्रार्थना", "सत्यापन", "एक", "दो", "तीन"):
        assert word in squashed, f"missing Hindi word from PDF: {word!r}"
    assert "1(एक):" in squashed or "1(एक)" in squashed
    assert "(क)" in squashed


@pytest.mark.skipif(not docx_to_pdf.converter_available(), reason="LibreOffice not installed")
def test_canonical_docx_pdf_matches_page_count(tmp_path):
    docx_path, pdf_path = ldr.render_both(TEST_5_MIXED, tmp_path, base_name="canon")
    import fitz
    from tests import parity_helpers as ph

    ref = reference_docx_to_pdf(docx_path, tmp_path / "ref")
    assert pdf_page_count(pdf_path) == pdf_page_count(ref)
    _assert_hindi_layer(pdf_path)
