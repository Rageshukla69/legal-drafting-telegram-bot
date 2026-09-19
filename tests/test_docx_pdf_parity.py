"""DOCX/PDF rendering-parity regression tests.

These tests exist because the DOCX and the PDF used to be laid out by two
independent engines (python-docx and ReportLab). The PDF is now a conversion of
the canonical DOCX, and these tests fail if that ever stops being true again.

Requires LibreOffice (``soffice``) plus ``pymupdf``; tests that need the
converter are skipped when it is not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import parity_helpers as ph  # noqa: E402
from dava_fixtures import ALL_FIXTURES, TEST_5_MIXED  # noqa: E402

from app.drafting_engine.renderers import docx_to_pdf  # noqa: E402
from app.drafting_engine.renderers import legal_document_renderer as ldr  # noqa: E402

FIXTURE_IDS = list(ALL_FIXTURES)

requires_converter = pytest.mark.skipif(
    ph.find_soffice() is None, reason="LibreOffice (soffice) is not installed"
)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict[str, dict[str, object]]:
    """Render every fixture once, plus an independent DOCX->PDF reference."""
    out_root = tmp_path_factory.mktemp("parity")
    results: dict[str, dict[str, object]] = {}
    for name, draft in ALL_FIXTURES.items():
        case_dir = out_root / name
        case_dir.mkdir(parents=True, exist_ok=True)
        docx_path, pdf_path = ldr.render_both(draft, case_dir, base_name=name, paper="legal")
        reference = None
        if ph.find_soffice() is not None:
            reference = ph.reference_docx_to_pdf(docx_path, case_dir / "reference")
        results[name] = {
            "draft": draft,
            "docx": docx_path,
            "pdf": pdf_path,
            "reference": reference,
        }
    return results


# ---------------------------------------------------------------------------
# 1. DOCX / PDF structural parity
# ---------------------------------------------------------------------------
@requires_converter
@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_page_count_matches_docx_rendering(rendered, name):
    entry = rendered[name]
    assert ph.pdf_page_count(entry["reference"]) == ph.pdf_page_count(entry["pdf"])


@requires_converter
@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_page_size_matches_docx_rendering(rendered, name):
    assert ph.pdf_page_sizes(rendered[name]["reference"]) == ph.pdf_page_sizes(rendered[name]["pdf"])


@requires_converter
@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_page_breaks_match_docx_rendering(rendered, name):
    """Every page must start with the same content in both renderings."""
    entry = rendered[name]
    assert ph.pdf_first_line_per_page(entry["reference"]) == ph.pdf_first_line_per_page(entry["pdf"])


@requires_converter
@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_is_visually_identical_to_docx_rendering(rendered, name):
    """Rasterised pages must match; different rasterisers are not involved."""
    entry = rendered[name]
    compared, ratio = ph.ink_difference(entry["reference"], entry["pdf"])
    assert compared > 0, "page counts or page sizes diverged"
    assert ratio < 0.005, f"{ratio:.4%} of pixels differ"


@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_text_equals_docx_text(rendered, name):
    entry = rendered[name]
    docx_text = "\n".join(ph.docx_paragraph_texts(entry["docx"]))
    pdf_text = ph.pdf_body_text(entry["pdf"])
    assert ph.squash(pdf_text) == ph.squash(docx_text)


# ---------------------------------------------------------------------------
# 2. PDF text-layer / Devanagari integrity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_text_layer_has_no_corrupted_characters(rendered, name):
    stats = ph.unicode_integrity(rendered[name]["pdf"])
    assert stats["replacement_chars"] == 0, "U+FFFD replacement characters in the PDF text layer"
    assert stats["private_use_chars"] == 0, "private-use codepoints in the PDF text layer"
    assert stats["control_chars"] == 0
    assert stats["orphan_matra_sequences"] == 0, "a matra lost its base consonant"
    assert stats["devanagari_chars"] > 300


@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_hindi_text_is_searchable_devanagari(rendered, name):
    """The exact Hindi strings must be present as real Unicode text."""
    entry = rendered[name]
    text = ph.pdf_body_text(entry["pdf"])
    draft = entry["draft"]
    needles = [
        draft["court_heading"],
        draft["title"],
        "बनाम" if len(draft["parties"]) == 2 else draft["parties"][0],
        "प्रार्थना",
        "सत्यापन",
        "बिधूना",
    ]
    squashed = ph.squash(text)
    for needle in needles:
        assert ph.squash(needle) in squashed, f"missing from PDF text layer: {needle!r}"


@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_pdf_text_is_not_transliterated_or_imaged(rendered, name):
    """Hindi must stay Hindi: mostly Devanagari, and never a scanned image page."""
    entry = rendered[name]
    text = ph.pdf_body_text(entry["pdf"])
    assert ph.devanagari_ratio(text) > 0.4
    # A rasterised page would have no text at all.
    assert len(text.strip()) > 300
    import fitz

    with fitz.open(str(entry["pdf"])) as doc:
        for page in doc:
            assert page.get_text().strip(), "page has no text layer (imaged page)"


# ---------------------------------------------------------------------------
# 3. Legal content must survive rendering unchanged
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", FIXTURE_IDS)
def test_legal_content_is_rendered_verbatim(rendered, name):
    entry = rendered[name]
    draft = entry["draft"]
    squashed_pdf = ph.squash(ph.pdf_body_text(entry["pdf"]))
    for paragraph in draft["pleadings"]:
        assert ph.squash(paragraph) in squashed_pdf
    for item in draft["prayer"]:
        assert ph.squash(item) in squashed_pdf
    assert ph.squash(draft["verification"]) in squashed_pdf


def test_numbering_and_dates_are_preserved(rendered):
    """Numbers, case numbers and dates must not be altered by rendering."""
    entry = rendered["TEST_5_MIXED"]
    text = ph.squash(ph.pdf_body_text(entry["pdf"]))
    for token in ("TEST-0005/2026", "1092A/B/C/D", "5सितंबर2026", "₹10,00,000", "0421/2026"):
        assert ph.squash(token) in text, f"missing from PDF: {token!r}"
    # Renderer-assigned Hindi paragraph numbering.
    assert "1(एक):" in text
    assert "7(सात):" in text


# ---------------------------------------------------------------------------
# 4. Page numbering parity
# ---------------------------------------------------------------------------
@requires_converter
def test_page_numbers_exist_in_both_docx_and_pdf(rendered):
    entry = rendered["TEST_2_TWO_PAGE"]
    from docx import Document

    footer_xml = Document(str(entry["docx"])).sections[0].footer.paragraphs[0]._p.xml
    assert "PAGE" in footer_xml and "fldChar" in footer_xml

    import fitz

    with fitz.open(str(entry["pdf"])) as doc:
        for number, page in enumerate(doc, 1):
            lines = [line.strip() for line in page.get_text().splitlines() if line.strip()]
            assert str(number) in lines[-1]


# ---------------------------------------------------------------------------
# 5. Public interface, engine selection and fallbacks
# ---------------------------------------------------------------------------
def test_render_pdf_public_interface_still_works(tmp_path):
    pdf_path = tmp_path / "interface.pdf"
    result = ldr.render_pdf(TEST_5_MIXED, pdf_path, paper="legal")
    assert Path(result) == pdf_path
    assert pdf_path.stat().st_size > 1000
    if ph.find_soffice() is not None:
        assert ph.unicode_integrity(pdf_path)["private_use_chars"] == 0


def test_render_both_returns_docx_and_pdf(tmp_path):
    docx_path, pdf_path = ldr.render_both(TEST_5_MIXED, tmp_path, base_name="parity_api")
    assert docx_path.exists() and pdf_path.exists()
    assert docx_path.suffix == ".docx" and pdf_path.suffix == ".pdf"


def test_legacy_reportlab_engine_is_still_available(tmp_path):
    """The old layout must remain callable so hosts can fall back safely."""
    pdf_path = tmp_path / "legacy.pdf"
    ldr.render_pdf_reportlab(TEST_5_MIXED, pdf_path, paper="legal")
    assert pdf_path.stat().st_size > 1000
    assert ph.pdf_page_count(pdf_path) >= 1


def test_pdf_engine_env_override_selects_reportlab(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_PDF_ENGINE", "reportlab")
    assert ldr.pdf_engine() == "reportlab"
    assert ldr.canonical_pdf_available() is False
    pdf_path = tmp_path / "forced_legacy.pdf"
    ldr.render_pdf(TEST_5_MIXED, pdf_path, paper="legal")
    assert pdf_path.stat().st_size > 1000


def test_falls_back_to_legacy_when_libreoffice_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_PDF_ENGINE", "canonical")
    monkeypatch.delenv("LEGAL_PDF_REQUIRE_CANONICAL", raising=False)
    monkeypatch.setattr(docx_to_pdf, "find_soffice", lambda: None)
    pdf_path = tmp_path / "fallback.pdf"
    ldr.render_pdf(TEST_5_MIXED, pdf_path, paper="legal")
    assert pdf_path.stat().st_size > 1000


def test_raises_when_canonical_pdf_is_required_but_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGAL_PDF_ENGINE", "canonical")
    monkeypatch.setenv("LEGAL_PDF_REQUIRE_CANONICAL", "1")
    monkeypatch.setattr(docx_to_pdf, "find_soffice", lambda: None)
    with pytest.raises(docx_to_pdf.PdfConverterUnavailable):
        ldr.render_pdf(TEST_5_MIXED, tmp_path / "must_fail.pdf", paper="legal")


@requires_converter
def test_converter_isolates_temporary_state(tmp_path):
    """Conversion must not leak temp files, profiles or a font dependency."""
    docx_path, pdf_path = ldr.render_both(TEST_5_MIXED, tmp_path, base_name="isolation")
    assert docx_to_pdf.convert(docx_path, pdf_path).exists()
    assert not list(tmp_path.glob("legal-docx-pdf-*"))


def test_docx_fonts_are_shipped_font_families(tmp_path):
    """The DOCX must name fonts the repository actually ships."""
    from docx import Document

    docx_path, _ = ldr.render_both(TEST_5_MIXED, tmp_path, base_name="fonts")
    document = Document(str(docx_path))
    families = set()
    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            if run.font.name:
                families.add(run.font.name)
    assert "Noto Sans Devanagari" in families
    assert families <= {"Noto Sans Devanagari", "DejaVu Sans"}, families


def test_converter_pins_the_bundled_fonts(tmp_path):
    """Rendering must not depend on the fonts installed on the host.

    A newer Noto Sans Devanagari build on another machine renders with
    different metrics and a different ToUnicode CMap, which changed both the
    layout and the extracted text before this was pinned.
    """
    config = docx_to_pdf.prepare_fonts(tmp_path)
    assert config is not None, "bundled fonts must be present to pin"
    body = Path(config).read_text(encoding="utf-8")
    font_dir = tmp_path / "fonts"
    assert str(font_dir) in body
    # Staged into the temporary work dir, never referenced in the repository.
    assert str(docx_to_pdf._ASSETS_FONTS_DIR) not in body
    assert (font_dir / "NotoSansDevanagari-Regular.ttf").exists()
    assert (font_dir / "DejaVuSans.ttf").exists()


def test_preparing_fonts_does_not_write_into_the_repository(tmp_path):
    """fontconfig drops a `.uuid` cache id into any directory it scans."""
    assets = docx_to_pdf._ASSETS_FONTS_DIR
    before = {p.name for p in assets.iterdir()}
    docx_to_pdf.prepare_fonts(tmp_path)
    assert {p.name for p in assets.iterdir()} == before
    assert not (assets / ".uuid").exists()


@pytest.mark.skipif(shutil.which("fc-match") is None, reason="fontconfig CLI is not available")
@pytest.mark.parametrize(
    ("family", "expected_file"),
    (
        ("Noto Sans Devanagari", "NotoSansDevanagari-Regular.ttf"),
        ("DejaVu Sans", "DejaVuSans.ttf"),
    ),
)
def test_pinned_fontconfig_resolves_to_the_bundled_files(tmp_path, family, expected_file):
    """The pinned fontconfig must resolve the DOCX's families to shipped files."""
    config = docx_to_pdf.prepare_fonts(tmp_path)
    env = dict(os.environ, FONTCONFIG_FILE=str(config), HOME=str(tmp_path))
    result = subprocess.run(
        ["fc-match", "-f", "%{file}", family], capture_output=True, text=True, env=env
    )
    assert Path(result.stdout.strip()).name == expected_file


def test_bundled_fonts_cover_latin_and_devanagari():
    """The bundled fonts alone must cover the scripts the drafts use."""
    devanagari = ldr._font_coverage(ldr._font_path())
    latin = ldr._font_coverage(ldr._latin_font_path())
    symbols = ldr._font_coverage(ldr._symbol_font_path())
    assert all(ord(ch) in devanagari for ch in "न्यायालयश्रीमानसिविलजज")
    assert all(ord(ch) in latin for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
    assert all(ord(ch) in symbols for ch in "§©→•✓₹⚖")
