#!/usr/bin/env python3
"""Devanagari PDF font / shaping test and PDF-pipeline diagnostic.

Renders a fixed Hindi test document through the *production* renderer and then
inspects the PDF at glyph level, so that an empty box (``□``), a missing glyph,
a wrongly substituted font, or an unshaped conjunct fails the run instead of
being noticed by a reader.

The test document contains exactly these words::

    न्यायालय  श्रीमान  सिविल जज  जूनियर डिवीजन  बिधूना
    (एक) (दो) (तीन) (चार) (पाँच) (छह) (क) (ख) (ग)
    संशोधन प्रार्थनापत्र एवं आपत्ति पत्र
    अंतर्गत धारा 5 म्याद अधिनियम
    वादीगण  प्रतिवादीगण  प्रार्थना  सत्यापन

laid out with the same code path, numbering (``1 (एक) :``) and prayer labels
(``(क)`` …) a real draft uses.

What is verified for every produced PDF
---------------------------------------
* the embedded fonts are exactly the bundled Noto Sans Devanagari / DejaVu Sans
  faces (no silent substitution by the host);
* no glyph 0 (``.notdef`` — the empty box) is drawn anywhere;
* every Devanagari character is drawn with the Devanagari font;
* every expected word appears in the PDF as the *same glyph sequence* HarfBuzz
  produces for it from the bundled font. Embedded subset fonts renumber their
  glyphs, so each drawn glyph is identified by its outline against the original
  font before comparing (this is what proves ``एक`` is ``ए`` + ``क`` and
  ``न्यायालय`` starts with the ``न्`` half-form rather than being boxes);
* the PDF has real text: for the canonical LibreOffice PDF every expected word
  must also be extractable as Unicode text;
* page count, page size (Legal) and that all ink stays inside the DOCX margins.

Usage
-----
    python scripts/pdf_devanagari_font_test.py                 # both engines when possible
    python scripts/pdf_devanagari_font_test.py --require-libreoffice   # Heroku / CI check
    python scripts/pdf_devanagari_font_test.py --engine reportlab      # fallback only

Artifacts (PDF, DOCX, PNG rasterisations, JSON report) go to ``--out``
(default ``tests/output/font_test``, which is git-ignored).  Exit status is 0
only when every produced PDF passes; ``--require-libreoffice`` also fails when
``soffice`` is missing.  ``heroku run python scripts/pdf_devanagari_font_test.py
--require-libreoffice`` is therefore the deployment check for the PDF pipeline.

Glyph-level inspection needs PyMuPDF (``pip install pymupdf``, listed in
requirements-dev.txt).  Without it the script still renders the documents and
reports the engine and embedded font names, but exits non-zero because the
glyphs could not be verified.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.drafting_engine.renderers import docx_to_pdf  # noqa: E402
from app.drafting_engine.renderers import legal_document_renderer as ldr  # noqa: E402
from app.drafting_engine.renderers import reportlab_shaping  # noqa: E402

# ---------------------------------------------------------------------------
# The test material
# ---------------------------------------------------------------------------
WORD_GROUPS: list[list[str]] = [
    ["न्यायालय", "श्रीमान", "सिविल जज", "जूनियर डिवीजन", "बिधूना"],
    ["(एक)", "(दो)", "(तीन)", "(चार)", "(पाँच)", "(छह)", "(क)", "(ख)", "(ग)"],
    ["संशोधन प्रार्थनापत्र एवं आपत्ति पत्र", "अंतर्गत धारा 5 म्याद अधिनियम"],
    ["वादीगण", "प्रतिवादीगण", "प्रार्थना", "सत्यापन"],
]
EXPECTED_WORDS: list[str] = [word for group in WORD_GROUPS for word in group]

# Words the user's report showed as boxes, with the exact production prefixes.
NUMBERED_HEADINGS = ["1 (एक) :", "2 (दो) :", "3 (तीन) :", "4 (चार) :", "5 (पाँच) :", "6 (छह) :"]
PRAYER_LABELS = ["(क)", "(ख)", "(ग)"]

# A Dava-shaped draft that makes the production renderer emit exactly the words
# above (the renderer itself adds the numbering, बनाम, प्रार्थना and सत्यापन).
FONT_TEST_DRAFT: dict[str, Any] = {
    "court_heading": "न्यायालय श्रीमान सिविल जज जूनियर डिवीजन बिधूना",
    "case_heading": "संशोधन प्रार्थनापत्र एवं आपत्ति पत्र",
    "parties": ["वादीगण", "प्रतिवादीगण"],
    "title": "अंतर्गत धारा 5 म्याद अधिनियम",
    "opening_averment": "",
    "pleadings": ["न्यायालय", "श्रीमान", "सिविल जज", "जूनियर डिवीजन", "बिधूना", "प्रार्थना"],
    "prayer": ["वादीगण", "प्रतिवादीगण", "सत्यापन"],
    "signature_block": ["वादीगण"],
    "verification": "सत्यापन",
}

# Mixed-font words: the exact ReportLab defect (a word whose fragments use
# different fonts) plus punctuation glued to Hindi words.
MIXED_FONT_DRAFT: dict[str, Any] = {
    "court_heading": "न्यायालय श्रीमान सिविल जज जूनियर डिवीजन बिधूना",
    "case_heading": "मूल वाद संख्या TEST-0002/2026 — परीक्षण / Test Draft (नमूना सामग्री — वास्तविक वाद नहीं)",
    "parties": [
        "वादी: सीताराम पुत्र रामस्वरूप, निवासी ग्राम नगला हरदास, थाना बिधूना, जनपद औरैया",
        "प्रतिवादी: सुरेश कुमार पुत्र हरिनारायण, निवासी ग्राम नगला हरदास, थाना बिधूना, जनपद औरैया",
    ],
    "title": "वादपत्र वास्ते स्थायी निषेधाज्ञा",
    "opening_averment": "वादी निम्नलिखित निवेदन करता है:-",
    "pleadings": [
        "यह कि गाटा सं. 1092 A/B/C/D, रकबा 0.180 हेक्टेयर, Section-धारा 5 म्याद अधिनियम, PW1द्वारा ₹10,00,000 की राशि।",
        "यह कि दिनांक 5 सितंबर 2026 को प्रतिवादी ने वादी के क्षेत्राधिकार में हस्तक्षेप किया।",
    ],
    "prayer": ["प्रतिवादी को रोका जाए।", "वाद व्यय दिलाया जाए।", "अन्य अनुतोष।"],
    "verification": "मैं, सीताराम, सत्यापित करता हूँ कि उपरोक्त तथ्य सत्य हैं।",
}

LEGAL_PAGE = (612.0, 1008.0)  # 8.5in x 14in in PDF points
# left, top, right, bottom bounds (points) that every drawn glyph must respect:
# the body sits inside the DOCX's 1.25in side margins (2pt tolerance for the
# advance box of a justified line's last glyph); vertically the header/footer
# band is allowed because the page number is drawn ~0.45-0.5in from the edge.
MARGIN_BOX = (1.25 * 72 - 2, 0.35 * 72, 612.0 - 1.25 * 72 + 2, 1008.0 - 0.25 * 72)

DEVANAGARI = re.compile(r"[\u0900-\u097F\uA8E0-\uA8FF]")


def is_devanagari_cp(cp: int) -> bool:
    return 0x0900 <= cp <= 0x097F or 0xA8E0 <= cp <= 0xA8FF


# ---------------------------------------------------------------------------
# Environment report
# ---------------------------------------------------------------------------
def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def font_report(path: str | Path) -> dict[str, Any]:
    from fontTools.ttLib import TTFont

    font = TTFont(str(path), lazy=True)
    try:
        names = {rec.nameID: rec.toUnicode() for rec in font["name"].names if rec.platformID == 3}
        cmap = font.getBestCmap()
        deva = sum(1 for cp in cmap if 0x0900 <= cp <= 0x097F)
        gsub = font["GSUB"].table if "GSUB" in font else None
        features = sorted({fr.FeatureTag for fr in gsub.FeatureList.FeatureRecord}) if gsub else []
        return {
            "file": str(path),
            "family": names.get(1),
            "style": names.get(2),
            "version": names.get(5),
            "glyphs": font["maxp"].numGlyphs,
            "devanagari_block_coverage": f"{deva}/128",
            "gsub_features": features,
            "sha256": _sha256(path)[:16],
        }
    finally:
        font.close()


def environment_report() -> dict[str, Any]:
    import reportlab

    status = ldr.pdf_pipeline_status()
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "reportlab": reportlab.Version,
        "uharfbuzz": getattr(reportlab_shaping.uharfbuzz, "__version__", None),
        "pdf_engine_setting": status["engine_setting"],
        "expected_engine": status["expected_engine"],
        "soffice": status["soffice"],
        "soffice_version": docx_to_pdf.soffice_version() if status["soffice"] else None,
        "require_canonical": status["require_canonical"],
        "fonts": {slot: font_report(path) for slot, path in status["fonts"].items()},
    }
    try:
        import fitz  # noqa: F401

        report["pymupdf"] = fitz.VersionBind
    except ImportError:
        report["pymupdf"] = None
    return report


# ---------------------------------------------------------------------------
# Reference shaping with HarfBuzz
# ---------------------------------------------------------------------------
def shape_reference(text: str, font_path: str | Path) -> list[int]:
    """Glyph ids HarfBuzz selects for ``text`` from ``font_path``."""
    import uharfbuzz as hb

    blob = hb.Blob.from_file_path(str(font_path))
    font = hb.Font(hb.Face(blob))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, dict(reportlab_shaping.DEFAULT_FEATURES))
    return [info.codepoint for info in buf.glyph_infos]


# ---------------------------------------------------------------------------
# Glyph identification: embedded subset glyph -> original font glyph
# ---------------------------------------------------------------------------
def _outline_key(glyph_set, name: str) -> tuple | None:
    """Hashable outline of a glyph (composites decomposed); None when empty."""
    from fontTools.pens.recordingPen import DecomposingRecordingPen

    pen = DecomposingRecordingPen(glyph_set)
    try:
        glyph_set[name].draw(pen)
    except Exception:  # pragma: no cover - defensive against odd subsets
        return None
    if not pen.value:
        return None
    key = []
    for op, args in pen.value:
        flat = []
        for arg in args:
            if isinstance(arg, tuple):
                flat.append(tuple(round(float(v), 1) for v in arg))
            else:
                flat.append(arg)
        key.append((op, tuple(flat)))
    return tuple(key)


class OriginalFont:
    """Outline index of one bundled font."""

    def __init__(self, path: str | Path):
        from fontTools.ttLib import TTFont

        self.path = str(path)
        self.ttfont = TTFont(self.path)
        self.order = self.ttfont.getGlyphOrder()
        self.postscript_name = next(
            (rec.toUnicode() for rec in self.ttfont["name"].names if rec.nameID == 6 and rec.platformID == 3),
            Path(self.path).stem,
        )
        glyph_set = self.ttfont.getGlyphSet()
        self.by_outline: dict[tuple, set[int]] = {}
        self.empty: set[int] = set()
        for gid, name in enumerate(self.order):
            key = _outline_key(glyph_set, name)
            if key is None:
                self.empty.add(gid)
            else:
                self.by_outline.setdefault(key, set()).add(gid)

    def glyph_name(self, gid: int) -> str:
        return self.order[gid] if 0 <= gid < len(self.order) else f"gid{gid}"


def bundled_original_fonts() -> dict[str, OriginalFont]:
    """Original fonts keyed by PostScript name (as embedded fonts are named)."""
    fonts: dict[str, OriginalFont] = {}
    for path in (ldr._font_path(), ldr._font_bold_path(), ldr._latin_font_path(), ldr._symbol_font_path()):
        if path in {f.path for f in fonts.values()}:
            continue
        original = OriginalFont(path)
        fonts[original.postscript_name] = original
    return fonts


def _base_font_name(name: str) -> str:
    # "AAAAAA+NotoSansDevanagari-Regular" -> "NotoSansDevanagari-Regular"
    return name.split("+", 1)[1] if "+" in name else name


@dataclass
class DrawnGlyph:
    page: int
    font: str            # embedded base font name
    subset_gid: int
    unicode: int
    origin: tuple[float, float]
    bbox: tuple[float, float, float, float]
    candidates: set[int] | None = None   # original gids with this outline (None = unknown)
    empty: bool = False                  # no outline (space / joiner)


@dataclass
class PdfInspection:
    pdf: str
    engine: str
    pages: int = 0
    page_sizes: list[tuple[float, float]] = field(default_factory=list)
    embedded_fonts: list[str] = field(default_factory=list)
    glyphs_drawn: int = 0
    notdef_glyphs: list[str] = field(default_factory=list)
    devanagari_in_wrong_font: list[str] = field(default_factory=list)
    unexpected_fonts: list[str] = field(default_factory=list)
    unidentified_glyphs: int = 0
    words_missing_as_glyphs: list[str] = field(default_factory=list)
    words_missing_in_text: list[str] = field(default_factory=list)
    ink_outside_margins: list[str] = field(default_factory=list)
    text_chars: int = 0
    devanagari_text_chars: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _embedded_subset_maps(doc, originals: dict[str, OriginalFont]) -> tuple[dict[str, list], list[str]]:
    """For every embedded font: (base name -> [(subset_glyph -> candidates, empty_gids)])."""
    from fontTools.ttLib import TTFont

    maps: dict[str, list[tuple[dict[int, set[int]], set[int]]]] = {}
    names: list[str] = []
    seen_xrefs: set[int] = set()
    for page in doc:
        for entry in page.get_fonts(full=True):
            xref, ext, _ftype, name = entry[0], entry[1], entry[2], entry[3]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            base = _base_font_name(name)
            names.append(base)
            original = originals.get(base)
            if original is None or ext not in {"ttf", "otf", "cff"}:
                continue
            try:
                _bn, _ext, _t, buffer = doc.extract_font(xref)
                subset = TTFont(io.BytesIO(buffer))
                glyph_set = subset.getGlyphSet()
                order = subset.getGlyphOrder()
            except Exception as exc:  # pragma: no cover - depends on producer
                maps.setdefault(base, []).append(({}, set()))
                continue
            mapping: dict[int, set[int]] = {}
            empty: set[int] = set()
            for gid, gname in enumerate(order):
                key = _outline_key(glyph_set, gname)
                if key is None:
                    empty.add(gid)
                else:
                    mapping[gid] = set(original.by_outline.get(key, set()))
            maps.setdefault(base, []).append((mapping, empty))
    return maps, sorted(set(names))


def _resolve_font_name(trace_name: str, embedded: Iterable[str]) -> str:
    """Map a (possibly truncated) texttrace font name to the embedded base name.

    MuPDF truncates long names: ``AAAAAA+NotoSansDevanagari-Regular`` is
    reported as ``NotoSansDevanagari-Regul``.
    """
    base = _base_font_name(trace_name)
    if base in embedded:
        return base
    matches = [name for name in embedded if name.startswith(base)]
    return matches[0] if len(matches) == 1 else base


def _drawn_glyphs(doc, maps, embedded: Iterable[str]) -> list[DrawnGlyph]:
    glyphs: list[DrawnGlyph] = []
    embedded = list(embedded)
    for page_index, page in enumerate(doc):
        last_key = None
        for span in page.get_texttrace():
            font = _resolve_font_name(span["font"], embedded)
            for ch in span["chars"]:
                unicode_cp, gid, origin, bbox = ch[0], ch[1], ch[2], ch[3]
                key = (font, gid, round(origin[0], 2), round(origin[1], 2))
                if key == last_key:
                    # One glyph mapped to several Unicode characters (ToUnicode
                    # ligature entry): the same glyph is reported once per char.
                    continue
                last_key = key
                drawn = DrawnGlyph(page_index + 1, font, gid, unicode_cp, (origin[0], origin[1]), tuple(bbox))
                subsets = maps.get(font)
                if subsets:
                    candidates: set[int] = set()
                    empty = False
                    known = False
                    for mapping, empty_gids in subsets:
                        if gid in mapping:
                            candidates |= mapping[gid]
                            known = True
                        if gid in empty_gids:
                            empty = True
                            known = True
                    drawn.candidates = candidates if known else None
                    drawn.empty = empty and not candidates
                glyphs.append(drawn)
    return glyphs


def _find_sequence(stream: list[DrawnGlyph], reference: list[int], original: OriginalFont) -> bool:
    """True when ``reference`` (original gids) occurs contiguously in ``stream``."""
    ref = [g for g in reference if g not in original.empty]
    seq = [g for g in stream if not g.empty]
    if not ref:
        return True
    n = len(ref)
    for start in range(0, len(seq) - n + 1):
        ok = True
        for offset, gid in enumerate(ref):
            cands = seq[start + offset].candidates
            if cands is None or gid not in cands:
                ok = False
                break
        if ok:
            return True
    return False


def inspect_pdf(
    pdf_path: str | Path,
    engine: str,
    expected_words: Iterable[str] = EXPECTED_WORDS,
    *,
    expect_text_layer: bool,
    expected_pages: int | None = None,
    require_glyph_sequences: bool = True,
) -> PdfInspection:
    """Glyph-level verification of a produced PDF (needs PyMuPDF)."""
    result = PdfInspection(pdf=str(pdf_path), engine=engine)
    try:
        import fitz
    except ImportError:
        result.errors.append("PyMuPDF (fitz) is not installed; glyphs could not be verified. pip install pymupdf")
        return result

    originals = bundled_original_fonts()
    allowed_fonts = set(originals)
    words = list(expected_words)

    with fitz.open(str(pdf_path)) as doc:
        result.pages = doc.page_count
        result.page_sizes = [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in doc]
        maps, result.embedded_fonts = _embedded_subset_maps(doc, originals)
        glyphs = _drawn_glyphs(doc, maps, result.embedded_fonts)
        text = "\n".join(page.get_text() for page in doc)

    result.glyphs_drawn = len(glyphs)
    result.text_chars = len(text)
    result.devanagari_text_chars = len(DEVANAGARI.findall(text))
    # Only fonts that actually draw glyphs matter: ReportLab always lists the
    # standard Helvetica resource even when no glyph uses it.
    drawing_fonts = sorted({g.font for g in glyphs})
    result.unexpected_fonts = sorted(f for f in drawing_fonts if f not in allowed_fonts)

    for g in glyphs:
        label = f"page {g.page} font {g.font} gid {g.subset_gid} U+{g.unicode:04X} at ({g.origin[0]:.0f},{g.origin[1]:.0f})"
        if g.subset_gid == 0 or (g.candidates is not None and g.candidates == {0}):
            result.notdef_glyphs.append(label)
        if is_devanagari_cp(g.unicode) and "Devanagari" not in g.font:
            result.devanagari_in_wrong_font.append(label)
        if g.candidates is None and not g.empty:
            result.unidentified_glyphs += 1
        x0, y0, x1, y1 = g.bbox
        if not g.empty and (x0 < MARGIN_BOX[0] - 0.5 or x1 > MARGIN_BOX[2] + 0.5 or y0 < MARGIN_BOX[1] or y1 > MARGIN_BOX[3]):
            result.ink_outside_margins.append(label)

    # Expected words as glyph sequences (either face, any page).
    for word in words:
        found = False
        for ps_name, original in originals.items():
            if "Devanagari" not in ps_name:
                continue
            stream = [g for g in glyphs if g.font == ps_name]
            if not stream:
                continue
            # LibreOffice shapes the whole text portion; ReportLab shapes each
            # whitespace-delimited word on its own (so a lone "1" or ":" is
            # shaped as script-neutral text). Accept either glyph selection.
            whole = shape_reference(word, original.path)
            per_word = [gid for part in word.split() for gid in shape_reference(part, original.path)]
            if _find_sequence(stream, whole, original) or _find_sequence(stream, per_word, original):
                found = True
                break
        if not found:
            result.words_missing_as_glyphs.append(word)

    squashed = re.sub(r"\s+", "", text)
    for word in words:
        if re.sub(r"\s+", "", word) not in squashed:
            result.words_missing_in_text.append(word)

    # ---- verdicts -------------------------------------------------------
    if result.pages == 0:
        result.errors.append("PDF has no pages")
    if expected_pages is not None and result.pages != expected_pages:
        result.errors.append(f"expected {expected_pages} page(s), got {result.pages}")
    for size in result.page_sizes:
        if abs(size[0] - LEGAL_PAGE[0]) > 1 or abs(size[1] - LEGAL_PAGE[1]) > 1:
            result.errors.append(f"page size {size} is not Legal {LEGAL_PAGE}")
            break
    if not any("Devanagari" in f for f in drawing_fonts):
        result.errors.append("no Devanagari font draws any glyph in the PDF (font not found / substituted)")
    if result.unexpected_fonts:
        result.errors.append(f"unexpected fonts embedded (substitution): {result.unexpected_fonts}")
    if result.glyphs_drawn == 0:
        result.errors.append("no glyphs drawn (image-only or empty PDF)")
    if result.notdef_glyphs:
        result.errors.append(f"{len(result.notdef_glyphs)} .notdef (empty box) glyph(s) drawn, e.g. {result.notdef_glyphs[:3]}")
    if result.devanagari_in_wrong_font:
        result.errors.append(f"{len(result.devanagari_in_wrong_font)} Devanagari char(s) drawn with a non-Devanagari font")
    if result.unidentified_glyphs:
        result.warnings.append(f"{result.unidentified_glyphs} glyph(s) could not be matched to a bundled font outline")
    if result.words_missing_as_glyphs:
        message = f"words not found as correctly shaped glyph sequences: {result.words_missing_as_glyphs}"
        (result.errors if require_glyph_sequences else result.warnings).append(message)
    if expect_text_layer:
        if result.words_missing_in_text:
            result.errors.append(f"words missing from the PDF text layer: {result.words_missing_in_text}")
        if result.devanagari_text_chars < 50:
            result.errors.append("PDF text layer contains almost no Devanagari (rasterised or transliterated?)")
    elif result.words_missing_in_text:
        result.warnings.append(
            "text layer is not Unicode-faithful (expected for the ReportLab fallback): "
            f"{len(result.words_missing_in_text)} words not extractable"
        )
    if result.ink_outside_margins:
        result.errors.append(f"{len(result.ink_outside_margins)} glyph(s) outside the page margins, e.g. {result.ink_outside_margins[:2]}")
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def rasterize(pdf_path: Path, out_dir: Path, dpi: int = 110) -> list[Path]:
    try:
        import fitz
    except ImportError:
        return []
    pngs: list[Path] = []
    with fitz.open(str(pdf_path)) as doc:
        for index, page in enumerate(doc, 1):
            png = out_dir / f"{pdf_path.stem}.page{index}.png"
            page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72)).save(str(png))
            pngs.append(png)
    return pngs


def render_documents(out_dir: Path, engines: list[str], draft: dict[str, Any] = FONT_TEST_DRAFT, stem: str = "devanagari_font_test") -> list[dict[str, Any]]:
    """Render ``draft`` with the requested engines; returns per-engine entries."""
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    docx_path = out_dir / f"{stem}.docx"
    ldr.render_docx(draft, docx_path, paper="legal")

    if "libreoffice" in engines:
        pdf_path = out_dir / f"{stem}.libreoffice.pdf"
        entry: dict[str, Any] = {"engine": "libreoffice", "docx": str(docx_path), "pdf": str(pdf_path)}
        started = time.time()
        try:
            docx_to_pdf.convert(docx_path, pdf_path)
            entry["seconds"] = round(time.time() - started, 2)
        except docx_to_pdf.PdfConversionError as exc:
            entry["error"] = str(exc)
        entries.append(entry)

    if "reportlab" in engines:
        pdf_path = out_dir / f"{stem}.reportlab.pdf"
        entry = {"engine": "reportlab", "docx": None, "pdf": str(pdf_path)}
        started = time.time()
        try:
            ldr.render_pdf_reportlab(draft, pdf_path, paper="legal")
            entry["seconds"] = round(time.time() - started, 2)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the diagnostic
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Heroku dyno simulation helpers (used by scripts/heroku_dyno_simulation.sh)
# ---------------------------------------------------------------------------
def write_dyno_script(sim_dir: Path, host_prefix: str, dyno_prefix: str, app_dir: str = "/app") -> Path:
    """Stage everything a *shell-only* dyno needs to run the exact conversion.

    The CI job cannot run this Python code inside the Heroku run image (it has
    no Python), so the DOCX, the pinned fontconfig and the exact ``soffice``
    command/environment the converter would use are written to ``sim_dir``.
    ``host_prefix`` is rewritten to ``dyno_prefix`` because the directory is
    bind-mounted at a different path inside the container; ``app_dir`` is the
    dyno's ``$HOME`` under which the apt buildpack unpacked ``.apt``.
    """
    sim_dir.mkdir(parents=True, exist_ok=True)
    docs = []
    for stem, draft in (("devanagari_font_test", FONT_TEST_DRAFT), ("devanagari_mixed_font", MIXED_FONT_DRAFT)):
        docs.append(ldr.render_docx(draft, sim_dir / f"{stem}.docx", paper="legal"))
    fontconfig = docx_to_pdf.prepare_fonts(sim_dir / "fontwork")
    assert fontconfig

    def dyno(path: Path | str) -> str:
        text = str(path)
        return text.replace(host_prefix, dyno_prefix, 1) if text.startswith(host_prefix) else text

    # fonts.conf embeds absolute paths: rewrite them for the container.
    config = Path(fontconfig)
    config.write_text(config.read_text(encoding="utf-8").replace(host_prefix, dyno_prefix), encoding="utf-8")

    home = Path(dyno(sim_dir / "home"))
    profile = Path(dyno(sim_dir / "profile"))
    outdir = Path(dyno(sim_dir / "out"))
    (sim_dir / "home").mkdir(exist_ok=True)
    (sim_dir / "profile").mkdir(exist_ok=True)
    (sim_dir / "out").mkdir(exist_ok=True)
    soffice = f"{app_dir}/.apt/usr/bin/soffice"
    # Environment exactly as conversion_environment() computes it for a
    # relocated (.apt) install, minus this host's own variables.
    env = docx_to_pdf.conversion_environment(soffice, home, dyno(config))
    keep = ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "FONTCONFIG_FILE", "LD_LIBRARY_PATH", "PATH")
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", "# generated by scripts/pdf_devanagari_font_test.py --write-dyno-script"]
    for key in keep:
        value = env.get(key, "")
        if key == "PATH":
            value = f"{app_dir}/.apt/usr/bin:/usr/local/bin:/usr/bin:/bin"
        elif key == "LD_LIBRARY_PATH":
            value = ":".join(part for part in value.split(":") if part.startswith(app_dir))
        lines.append(f'export {key}="{value}"')
    lines.append(f'"{soffice}" --headless "-env:UserInstallation={profile.as_uri()}" --version')
    lines.append('echo "--- ldd check (libraries the relocated LibreOffice cannot find) ---"')
    lines.append(f'for lib in "{app_dir}"/.apt/usr/lib/libreoffice/program/soffice.bin "{app_dir}"/.apt/usr/lib/libreoffice/program/lib{{swlo,vcllo,pdffilterlo,mergedlo}}.so; do [ -e "$lib" ] && ldd "$lib" | grep "not found" | sed "s|^|$(basename "$lib"): |" || true; done')
    for doc in docs:
        command = docx_to_pdf.conversion_command(soffice, profile, outdir, Path(dyno(doc)))
        lines.append(" ".join(f'"{part}"' for part in command))
    lines.append(f'ls -l "{outdir}"')
    script = sim_dir / "run_conversion.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _annotate(level: str, message: str) -> None:
    """GitHub Actions workflow command (shows up as a check annotation)."""
    print(f"::{level} title=Devanagari PDF font test::{message}")


def _print(title: str, payload: Any) -> None:
    print(f"\n== {title} ==")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "tests" / "output" / "font_test"), help="artifact directory")
    parser.add_argument("--engine", choices=["both", "canonical", "libreoffice", "reportlab"], default="both")
    parser.add_argument("--require-libreoffice", action="store_true", help="fail when soffice is not usable (Heroku/CI check)")
    parser.add_argument("--no-raster", action="store_true", help="skip PNG rasterisation")
    parser.add_argument("--github-annotations", action="store_true", help="emit ::notice/::error workflow commands")
    parser.add_argument("--write-dyno-script", metavar="DIR", help="stage DOCX + fonts + soffice command for a shell-only dyno and exit")
    parser.add_argument("--dyno-prefix", default="/app/sim", help="path DIR is mounted at inside the dyno (with --write-dyno-script)")
    parser.add_argument("--dyno-app-dir", default="/app", help="dyno $HOME containing .apt (with --write-dyno-script)")
    parser.add_argument("--inspect-pdf", metavar="PDF", nargs="+", help="only inspect existing PDF(s) produced by the canonical engine")
    args = parser.parse_args(argv)

    if args.write_dyno_script:
        sim_dir = Path(args.write_dyno_script).resolve()
        script = write_dyno_script(sim_dir, str(sim_dir), args.dyno_prefix.rstrip("/"), args.dyno_app_dir.rstrip("/"))
        print(f"dyno simulation staged in {sim_dir}; run {script.name} inside the dyno image")
        return 0

    if args.inspect_pdf:
        failures = []
        for pdf in args.inspect_pdf:
            mixed = "mixed" in Path(pdf).name
            expected = ["1 (एक) :", "(नमूना", "नहीं)", "रामस्वरूप,", "वादी:", "है:-", "क्षेत्राधिकार", "निषेधाज्ञा", "श्रीमान"] if mixed else EXPECTED_WORDS
            inspection = inspect_pdf(pdf, "libreoffice", expected, expect_text_layer=True, expected_pages=None if mixed else 1)
            _print(Path(pdf).name, {k: v for k, v in asdict(inspection).items() if k not in {"notdef_glyphs", "devanagari_in_wrong_font", "ink_outside_margins"} or v})
            if not inspection.ok:
                failures.append(f"{Path(pdf).name}: " + "; ".join(inspection.errors))
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            if not args.no_raster:
                rasterize(Path(pdf), out_dir)
        if failures:
            if args.github_annotations:
                for failure in failures:
                    _annotate("error", failure)
            print("RESULT: FAIL")
            return 1
        if args.github_annotations:
            _annotate("notice", f"PASS: {len(args.inspect_pdf)} canonical PDF(s) draw the Hindi test words correctly")
        print("RESULT: PASS")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = environment_report()
    _print("environment", env)

    failures: list[str] = []
    engines: list[str]
    wanted = "libreoffice" if args.engine == "canonical" else args.engine
    if wanted == "both":
        engines = (["libreoffice"] if env["soffice"] else []) + ["reportlab"]
    else:
        engines = [wanted]
    if "libreoffice" in engines and not env["soffice"]:
        message = "LibreOffice (soffice) not found: the canonical DOCX -> PDF conversion is unavailable on this host."
        if args.require_libreoffice or wanted == "libreoffice":
            failures.append(message)
        print("\n!! " + message)
        engines = [e for e in engines if e != "libreoffice"]
    elif args.require_libreoffice and "libreoffice" not in engines:
        engines.insert(0, "libreoffice")

    report: dict[str, Any] = {"environment": env, "documents": []}
    for draft_name, draft in (("font_test", FONT_TEST_DRAFT), ("mixed_font", MIXED_FONT_DRAFT)):
        entries = render_documents(out_dir, engines, draft=draft, stem=f"devanagari_{draft_name}")
        for entry in entries:
            if entry.get("error"):
                failures.append(f"{draft_name}/{entry['engine']}: rendering failed: {entry['error']}")
                report["documents"].append({"draft": draft_name, **entry})
                _print(f"{draft_name} / {entry['engine']}", entry)
                continue
            expected = EXPECTED_WORDS if draft_name == "font_test" else (
                ["1 (एक) :", "(नमूना", "नहीं)", "रामस्वरूप,", "वादी:", "है:-", "क्षेत्राधिकार", "निषेधाज्ञा", "श्रीमान"]
            )
            inspection = inspect_pdf(
                entry["pdf"],
                entry["engine"],
                expected,
                expect_text_layer=entry["engine"] == "libreoffice",
                expected_pages=1 if draft_name == "font_test" else None,
            )
            entry["inspection"] = asdict(inspection)
            entry["ok"] = inspection.ok
            if not args.no_raster:
                entry["png"] = [str(p) for p in rasterize(Path(entry["pdf"]), out_dir)]
            report["documents"].append({"draft": draft_name, **entry})
            summary = {
                "pdf": entry["pdf"],
                "pages": inspection.pages,
                "page_sizes": inspection.page_sizes,
                "embedded_fonts": inspection.embedded_fonts,
                "glyphs_drawn": inspection.glyphs_drawn,
                "notdef_glyphs": len(inspection.notdef_glyphs),
                "devanagari_in_wrong_font": len(inspection.devanagari_in_wrong_font),
                "words_missing_as_glyphs": inspection.words_missing_as_glyphs,
                "words_missing_in_text": inspection.words_missing_in_text,
                "text_chars": inspection.text_chars,
                "devanagari_text_chars": inspection.devanagari_text_chars,
                "warnings": inspection.warnings,
                "errors": inspection.errors,
                "verdict": "PASS" if inspection.ok else "FAIL",
            }
            _print(f"{draft_name} / {entry['engine']}", summary)
            if not inspection.ok:
                failures.append(f"{draft_name}/{entry['engine']}: " + "; ".join(inspection.errors))

    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nArtifacts written to {out_dir}")
    engines_run = sorted({d["engine"] for d in report["documents"] if d.get("ok")})
    if failures:
        print("\nRESULT: FAIL")
        for failure in failures:
            print(" - " + failure)
            if args.github_annotations:
                _annotate("error", failure)
        return 1
    verdict = (
        "PASS — every produced PDF draws the Hindi test words with the bundled Devanagari font, no empty boxes "
        f"(engines verified: {', '.join(engines_run)}; soffice: {env['soffice_version'] or 'not installed'})"
    )
    print("\nRESULT: " + verdict)
    if args.github_annotations:
        _annotate("notice", verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
