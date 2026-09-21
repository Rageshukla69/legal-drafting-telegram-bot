"""Regression tests: Hindi must render as real, correctly shaped glyphs in the PDF.

Background
----------
The DOCX is the canonical document and displayed Hindi correctly, while the PDF
showed ``1 (□□) :`` / ``(□)`` instead of ``1 (एक) :`` / ``(क)``.  Two things
combined to produce that:

1. On the affected host LibreOffice was not usable, so the renderer silently
   fell back to its independent ReportLab layout.
2. ReportLab's HarfBuzz integration shapes a whitespace-delimited word with the
   font of the word's *first* fragment.  ``(एक)`` is ``(`` [Latin font] +
   ``एक`` [Devanagari font] + ``)`` [Latin font], so ``एक`` was shaped with
   DejaVu Sans, which has no Devanagari glyphs -> glyph 0 -> empty boxes.

These tests fail when

* a bundled font file is missing or cannot be registered,
* HarfBuzz cannot shape the Hindi test words with the bundled font,
* a PDF (canonical LibreOffice conversion *or* the ReportLab fallback) draws a
  ``.notdef`` box, draws Devanagari with a substituted font, or does not contain
  the expected words as the exact glyph sequences HarfBuzz selects,
* the fallback silently runs without shaping,
* the DOCX renderer's output changes (golden hashes), or the fallback PDF stops
  using the same font routing as the DOCX.

The glyph-level inspection lives in ``scripts/pdf_devanagari_font_test.py`` so
the same check can be run on a deployment (``heroku run``).
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import parity_helpers as ph  # noqa: E402
import pdf_devanagari_font_test as fonttest  # noqa: E402
from dava_fixtures import ALL_FIXTURES, TEST_5_MIXED  # noqa: E402

from app.drafting_engine.renderers import docx_to_pdf  # noqa: E402
from app.drafting_engine.renderers import legal_document_renderer as ldr  # noqa: E402
from app.drafting_engine.renderers import reportlab_shaping  # noqa: E402

pytest.importorskip("fitz", reason="PyMuPDF is required for glyph-level PDF checks (requirements-dev.txt)")

requires_converter = pytest.mark.skipif(
    docx_to_pdf.find_soffice() is None, reason="LibreOffice (soffice) is not installed"
)

WORDS = fonttest.EXPECTED_WORDS
SINGLE_WORDS = [
    "न्यायालय", "श्रीमान", "सिविल", "जज", "जूनियर", "डिवीजन", "बिधूना",
    "एक", "दो", "तीन", "चार", "पाँच", "छह", "क", "ख", "ग", "प्रार्थना", "सत्यापन",
]


# ---------------------------------------------------------------------------
# 1. Font availability, registration and shaping coverage
# ---------------------------------------------------------------------------
def test_bundled_devanagari_fonts_exist_and_register():
    regular = Path(ldr._font_path())
    bold = Path(ldr._font_bold_path())
    assert regular.is_file() and regular.name == "NotoSansDevanagari-Regular.ttf"
    assert bold.is_file() and bold.name == "NotoSansDevanagari-Bold.ttf"
    assert ldr._font_family_name(str(regular)) == "Noto Sans Devanagari"
    names = ldr._register_pdf_fonts()
    assert names == ("VakilDeva", "VakilDevaBold", "VakilLatin", "VakilSymbols")
    from reportlab.pdfbase import pdfmetrics

    for name in names:
        font = pdfmetrics.getFont(name)
        assert font.face.name  # parsed TrueType face
    assert pdfmetrics.getFont("VakilDeva").shapable, "uharfbuzz must be importable so Devanagari can be shaped"


def test_bundled_font_covers_every_test_character():
    coverage = ldr._font_coverage(ldr._font_path())
    for word in WORDS + fonttest.NUMBERED_HEADINGS + fonttest.PRAYER_LABELS:
        for ch in word:
            if ch.isspace():
                continue
            assert ord(ch) in coverage, f"U+{ord(ch):04X} ({ch}) missing from Noto Sans Devanagari"


def test_harfbuzz_shapes_test_words_without_notdef_or_visible_virama():
    from fontTools.ttLib import TTFont

    font_path = ldr._font_path()
    order = TTFont(font_path).getGlyphOrder()
    virama_gid = order.index("viramadeva") if "viramadeva" in order else None
    for word in WORDS + fonttest.NUMBERED_HEADINGS:
        gids = fonttest.shape_reference(word, font_path)
        assert gids, word
        assert 0 not in gids, f"{word!r} shaped with a .notdef glyph"
        # Every conjunct in the test words has a half-form/ligature in the font,
        # so no explicit virama glyph may survive shaping (that would be an
        # unshaped consonant cluster such as न + ् + य drawn separately).
        if virama_gid is not None:
            assert virama_gid not in gids, f"{word!r} left an unshaped virama"
    # Conjuncts actually merge: fewer glyphs than codepoints.
    assert len(fonttest.shape_reference("न्यायालय", font_path)) < len("न्यायालय")
    assert len(fonttest.shape_reference("श्रीमान", font_path)) < len("श्रीमान")
    # The i-matra is reordered before its consonant: सि -> [i-matra, sa].
    names = [order[g] for g in fonttest.shape_reference("सिविल", font_path)]
    assert names[0].startswith("ivowelsign") and names[1] == "sadeva", names


# ---------------------------------------------------------------------------
# 2. ReportLab fallback: the exact defect from the bug report
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fallback_pdfs(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("fallback")
    entries = fonttest.render_documents(out, ["reportlab"], draft=fonttest.FONT_TEST_DRAFT, stem="font_test")
    assert not entries[0].get("error"), entries[0]
    mixed = fonttest.render_documents(out, ["reportlab"], draft=fonttest.MIXED_FONT_DRAFT, stem="mixed")
    assert not mixed[0].get("error"), mixed[0]
    return {"font_test": Path(entries[0]["pdf"]), "mixed": Path(mixed[0]["pdf"])}


def test_fallback_pdf_draws_all_test_words_with_real_devanagari_glyphs(fallback_pdfs):
    result = fonttest.inspect_pdf(fallback_pdfs["font_test"], "reportlab", WORDS, expect_text_layer=False, expected_pages=1)
    assert result.errors == [], result.errors
    assert result.notdef_glyphs == []
    assert result.devanagari_in_wrong_font == []
    assert result.words_missing_as_glyphs == []
    assert result.unidentified_glyphs == 0
    assert {"NotoSansDevanagari-Regular", "NotoSansDevanagari-Bold"} <= set(result.embedded_fonts)
    assert result.page_sizes == [(612.0, 1008.0)]  # Legal, like the DOCX


def test_fallback_pdf_numbered_headings_and_prayer_labels_are_not_boxes(fallback_pdfs):
    """``1 (एक) :`` … ``6 (छह) :`` and ``(क)`` ``(ख)`` ``(ग)`` exactly as produced."""
    result = fonttest.inspect_pdf(
        fallback_pdfs["font_test"],
        "reportlab",
        fonttest.NUMBERED_HEADINGS + fonttest.PRAYER_LABELS + SINGLE_WORDS,
        expect_text_layer=False,
    )
    assert result.notdef_glyphs == []
    assert result.words_missing_as_glyphs == []


def test_fallback_pdf_mixed_font_words_regression(fallback_pdfs):
    """Words that switch fonts inside the word were shaped with the wrong font."""
    result = fonttest.inspect_pdf(
        fallback_pdfs["mixed"],
        "reportlab",
        ["1 (एक) :", "(नमूना", "नहीं)", "रामस्वरूप,", "वादी:", "है:-", "Section-धारा", "PW1द्वारा", "₹10,00,000", "क्षेत्राधिकार", "निषेधाज्ञा"],
        expect_text_layer=False,
    )
    assert result.notdef_glyphs == [], result.notdef_glyphs[:5]
    assert result.devanagari_in_wrong_font == []
    # Latin letters and the rupee sign legitimately come from DejaVu Sans, so
    # those words are checked for the absence of boxes only; the pure Hindi and
    # Hindi+punctuation words must match HarfBuzz glyph for glyph.
    latin_words = {"Section-धारा", "PW1द्वारा", "₹10,00,000"}
    assert [w for w in result.words_missing_as_glyphs if w not in latin_words] == []


def test_fallback_shaper_is_the_corrected_per_font_shaper(fallback_pdfs):
    import reportlab.platypus.paragraph as paragraph_module

    assert reportlab_shaping.installed()
    assert paragraph_module.shapeFragWord is reportlab_shaping.shape_frag_word


def test_corrected_shaper_shapes_each_fragment_with_its_own_font():
    """Direct unit test of the fix on the reported word ``(एक)``."""
    from reportlab.lib.abag import ABag
    from reportlab.pdfbase import pdfmetrics

    deva, _bold, latin, _sym = ldr._register_pdf_fonts()
    latin_frag = ABag(fontName=latin, fontSize=14)
    deva_frag = ABag(fontName=deva, fontSize=14)
    deva_font = pdfmetrics.getFont(deva)
    latin_font = pdfmetrics.getFont(latin)

    # ``(एक)``: plain cmap glyphs everywhere, so shaping changes nothing and the
    # word is handed back untouched — ReportLab then draws each fragment with
    # its own font, which is correct. ``force=True`` returns the shaped form.
    word = [0, (latin_frag, "("), (deva_frag, "एक"), (latin_frag, ")")]
    assert reportlab_shaping.shape_frag_word(word) is word
    shaped = reportlab_shaping.shape_frag_word(word, force=True)
    fragments = shaped[1:]
    assert [f.fontName for f, _ in fragments] == [latin, deva, latin]
    deva_text = fragments[1][1]
    assert [ord(c) for c in deva_text] == [ord("ए"), ord("क")]
    assert all(deva_font.face.charToGlyph[ord(c)] != 0 for c in deva_text)
    assert fragments[0][1] == "(" and fragments[2][1] == ")"
    assert latin_font.face.charToGlyph[ord("(")] != 0

    # ``(न्यायालय)`` needs real shaping (न् half-form): the Devanagari fragment
    # must be shaped with Noto — never with DejaVu, the font of the first
    # fragment — and the parentheses must remain DejaVu's.
    from fontTools.ttLib import TTFont

    order = TTFont(ldr._font_path()).getGlyphOrder()
    word = [0, (latin_frag, "("), (deva_frag, "न्यायालय"), (latin_frag, ")")]
    shaped = reportlab_shaping.shape_frag_word(word)
    assert shaped is not word
    fragments = shaped[1:]
    assert [f.fontName for f, _ in fragments] == [latin, deva, latin]
    deva_run = fragments[1][1]
    assert len(deva_run) == 7  # 8 code points -> 7 glyphs (न् + य merge)
    glyph_names = []
    for ch in deva_run:
        gid = deva_font.face.charToGlyph.get(ord(ch), 0)
        assert gid != 0, f"U+{ord(ch):04X} is not a glyph of Noto Sans Devanagari"
        glyph_names.append(order[gid])
    assert glyph_names[0] == "naprehalfdeva", glyph_names
    assert fragments[0][1] == "(" and fragments[2][1] == ")"


def test_fallback_refuses_to_render_unshaped_devanagari(tmp_path, monkeypatch):
    monkeypatch.setattr(reportlab_shaping, "installed", lambda: False)
    monkeypatch.setattr(reportlab_shaping, "uharfbuzz", None)
    with pytest.raises(reportlab_shaping.ShapingUnavailable):
        ldr.render_pdf_reportlab(TEST_5_MIXED, tmp_path / "unshaped.pdf")


# ---------------------------------------------------------------------------
# 3. Canonical LibreOffice conversion of the DOCX
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def canonical_pdfs(tmp_path_factory) -> dict[str, Path]:
    if docx_to_pdf.find_soffice() is None:
        pytest.skip("LibreOffice (soffice) is not installed")
    out = tmp_path_factory.mktemp("canonical")
    entries = fonttest.render_documents(out, ["libreoffice"], draft=fonttest.FONT_TEST_DRAFT, stem="font_test")
    assert not entries[0].get("error"), entries[0]
    mixed = fonttest.render_documents(out, ["libreoffice"], draft=fonttest.MIXED_FONT_DRAFT, stem="mixed")
    assert not mixed[0].get("error"), mixed[0]
    return {
        "font_test": Path(entries[0]["pdf"]),
        "font_test_docx": Path(entries[0]["docx"]),
        "mixed": Path(mixed[0]["pdf"]),
    }


@requires_converter
def test_canonical_pdf_draws_all_test_words_with_real_devanagari_glyphs(canonical_pdfs):
    result = fonttest.inspect_pdf(canonical_pdfs["font_test"], "libreoffice", WORDS, expect_text_layer=True, expected_pages=1)
    assert result.errors == [], result.errors
    assert result.notdef_glyphs == []
    assert result.devanagari_in_wrong_font == []
    assert result.unexpected_fonts == [], "LibreOffice substituted a host font for a bundled one"
    assert result.words_missing_as_glyphs == []
    assert result.words_missing_in_text == []
    assert result.page_sizes == [(612.0, 1008.0)]


@requires_converter
def test_canonical_pdf_numbered_headings_and_prayer_labels(canonical_pdfs):
    result = fonttest.inspect_pdf(
        canonical_pdfs["font_test"],
        "libreoffice",
        fonttest.NUMBERED_HEADINGS + fonttest.PRAYER_LABELS + SINGLE_WORDS,
        expect_text_layer=True,
    )
    assert result.notdef_glyphs == []
    assert result.words_missing_as_glyphs == []
    assert result.words_missing_in_text == []


@requires_converter
def test_canonical_pdf_mixed_font_words(canonical_pdfs):
    result = fonttest.inspect_pdf(
        canonical_pdfs["mixed"],
        "libreoffice",
        ["1 (एक) :", "(नमूना", "नहीं)", "रामस्वरूप,", "वादी:", "है:-", "क्षेत्राधिकार", "निषेधाज्ञा", "Section-धारा", "₹10,00,000"],
        expect_text_layer=True,
    )
    assert result.notdef_glyphs == []
    assert result.devanagari_in_wrong_font == []
    assert result.words_missing_in_text == []
    latin_words = {"Section-धारा", "₹10,00,000"}
    assert [w for w in result.words_missing_as_glyphs if w not in latin_words] == []


@requires_converter
def test_canonical_pdf_layout_matches_docx_rendering(canonical_pdfs, tmp_path):
    """Page count, page size and page-start lines equal an independent conversion."""
    reference = ph.reference_docx_to_pdf(canonical_pdfs["font_test_docx"], tmp_path / "reference")
    assert reference is not None
    assert ph.pdf_page_count(reference) == ph.pdf_page_count(canonical_pdfs["font_test"]) == 1
    assert ph.pdf_page_sizes(reference) == ph.pdf_page_sizes(canonical_pdfs["font_test"])
    assert ph.pdf_first_line_per_page(reference) == ph.pdf_first_line_per_page(canonical_pdfs["font_test"])
    compared, ratio = ph.ink_difference(reference, canonical_pdfs["font_test"])
    assert compared == 1 and ratio < 0.005


@requires_converter
def test_render_both_reports_canonical_engine(tmp_path):
    report: dict = {}
    _docx, pdf = ldr.render_both(fonttest.FONT_TEST_DRAFT, tmp_path, base_name="engine_report", report=report)
    assert report["engine"] == ldr.PDF_ENGINE_LIBREOFFICE and report["canonical"] is True
    assert report["warning"] is None
    result = fonttest.inspect_pdf(pdf, "libreoffice", WORDS, expect_text_layer=True)
    assert result.errors == [], result.errors


# ---------------------------------------------------------------------------
# 4. Engine reporting / pipeline status
# ---------------------------------------------------------------------------
def test_render_both_reports_fallback_when_libreoffice_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_PDF_ENGINE", "canonical")
    monkeypatch.delenv("LEGAL_PDF_REQUIRE_CANONICAL", raising=False)
    monkeypatch.setattr(docx_to_pdf, "find_soffice", lambda: None)
    report: dict = {}
    _docx, pdf = ldr.render_both(fonttest.FONT_TEST_DRAFT, tmp_path, base_name="fallback_report", report=report)
    assert report["engine"] == ldr.PDF_ENGINE_REPORTLAB and report["canonical"] is False
    assert "LibreOffice" in report["warning"]
    # And even the fallback must draw real glyphs.
    result = fonttest.inspect_pdf(pdf, "reportlab", WORDS, expect_text_layer=False, expected_pages=1)
    assert result.errors == [], result.errors


def test_pdf_pipeline_status_describes_engine(monkeypatch):
    status = ldr.pdf_pipeline_status()
    assert status["expected_engine"] in {ldr.PDF_ENGINE_LIBREOFFICE, ldr.PDF_ENGINE_REPORTLAB}
    assert status["uharfbuzz"] is True
    assert Path(status["fonts"]["devanagari_regular"]).is_file()
    monkeypatch.setattr(docx_to_pdf, "find_soffice", lambda: None)
    status = ldr.pdf_pipeline_status()
    assert status["expected_engine"] == ldr.PDF_ENGINE_REPORTLAB and status["canonical"] is False
    assert "heroku-community/apt" in status["reason"]


def test_find_soffice_discovers_heroku_apt_relocation(tmp_path, monkeypatch):
    """The apt buildpack unpacks LibreOffice under $HOME/.apt on a dyno."""
    fake = tmp_path / ".apt" / "usr" / "bin" / "soffice"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SOFFICE_BIN", raising=False)
    monkeypatch.setattr(docx_to_pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(docx_to_pdf, "_COMMON_BINARIES", ())
    assert docx_to_pdf.find_soffice() == str(fake)
    env = docx_to_pdf.apt_environment({"PATH": "/usr/bin", "LD_LIBRARY_PATH": ""}, fake)
    prefix = tmp_path / ".apt"
    assert env["PATH"].split(":")[0] == str(prefix / "usr" / "bin")
    assert env["LD_LIBRARY_PATH"].split(":")[:3] == [
        str(prefix / "usr" / "lib" / "x86_64-linux-gnu"),
        str(prefix / "usr" / "lib" / "i386-linux-gnu"),
        str(prefix / "usr" / "lib"),
    ]
    # Applied only once even if called twice.
    again = docx_to_pdf.apt_environment(dict(env), fake)
    assert again["LD_LIBRARY_PATH"] == env["LD_LIBRARY_PATH"]


# ---------------------------------------------------------------------------
# 5. The DOCX stays the canonical, unchanged document
# ---------------------------------------------------------------------------
# sha256 of word/document.xml as produced by python-docx 1.2.x before this fix.
# The PDF work must not alter the DOCX at all.
GOLDEN_DOCUMENT_XML = {
    "TEST_1_SHORT": "f0c6a2a4e8d9e153",
    "TEST_2_TWO_PAGE": "bf419a6486deeecf",
    "TEST_3_LONG": "0733b57ba66d733b",
    "TEST_4_DEVANAGARI_HEAVY": "601d07aff9a797ed",
    "TEST_5_MIXED": "f94b13c02b3a294a",
}
GOLDEN_FOOTER_XML = "4c82334c604b74cd"


def _docx_part_hash(path: Path, part: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return hashlib.sha256(archive.read(part)).hexdigest()[:16]


@pytest.mark.parametrize("name", sorted(GOLDEN_DOCUMENT_XML))
def test_docx_output_is_byte_identical_to_golden(tmp_path, name):
    import docx as python_docx

    if not str(getattr(python_docx, "__version__", "")).startswith("1.2"):
        pytest.skip("golden hashes were recorded with python-docx 1.2.x")
    docx_path = ldr.render_docx(ALL_FIXTURES[name], tmp_path / f"{name}.docx", paper="legal")
    assert _docx_part_hash(docx_path, "word/document.xml") == GOLDEN_DOCUMENT_XML[name]
    assert _docx_part_hash(docx_path, "word/footer1.xml") == GOLDEN_FOOTER_XML


def test_fallback_font_routing_matches_the_docx_runs(tmp_path):
    """The fallback PDF must put each character in the font the DOCX uses."""
    from docx import Document

    docx_path = ldr.render_docx(TEST_5_MIXED, tmp_path / "routing.docx", paper="legal")
    primary, latin, symbol, _cov = ldr._docx_font_plan()
    family = {ldr.FONT_SLOT_PRIMARY: primary, ldr.FONT_SLOT_LATIN: latin, ldr.FONT_SLOT_SYMBOL: symbol}
    paragraphs = [p for p in Document(str(docx_path)).paragraphs if p.runs]
    assert paragraphs
    checked = 0
    for paragraph in paragraphs:
        docx_runs = [(run.font.name, run.text) for run in paragraph.runs]
        pdf_runs = [(family[slot], chunk) for slot, chunk in ldr._font_runs(paragraph.text)]
        assert pdf_runs == docx_runs, paragraph.text
        checked += len(docx_runs)
    assert checked > 20
