"""Correct per-font HarfBuzz shaping for the ReportLab fallback PDF.

Why this module exists
----------------------
The canonical PDF is converted from the DOCX by LibreOffice (see
``docx_to_pdf``). When LibreOffice is not available the renderer falls back to
its legacy ReportLab layout, and that layout showed empty boxes for perfectly
valid Hindi::

    1 (□□) : ...        instead of    1 (एक) : ...
    (□) ...             instead of    (क) ...
    रामस्वरूप· निवासी    instead of    रामस्वरूप, निवासी

The bundled Noto Sans Devanagari font is complete and HarfBuzz shapes every one
of those words correctly, so neither the font nor the shaping engine is at
fault. The defect is in ReportLab's ``shapeFragWord`` (ReportLab 4.x and 5.0.x):

* A whitespace-delimited *word* is shaped in **one** HarfBuzz buffer using the
  font of the word's **first** fragment.
* The resulting glyph ids are then mapped back to characters through **each
  fragment's own** font.

A word that mixes fonts is therefore corrupted twice.  ``(एक)`` is three
fragments — ``(`` in the Latin font, ``एक`` in the Devanagari font, ``)`` in the
Latin font — so the whole word is shaped with DejaVu Sans, which has no
Devanagari glyphs: ``एक`` becomes glyph 0 (``.notdef``), drawn as two boxes.
Conversely ``रामस्वरूप,`` is shaped with Noto Sans Devanagari, and Noto's comma
glyph id is then looked up in DejaVu Sans, producing an unrelated glyph.

The fix
-------
:func:`shape_frag_word` shapes every run of consecutive same-font fragments in
its own HarfBuzz buffer with its own font, and only then concatenates the
results.  Glyph ids and character mappings always belong to the same font, so
mixed-font words render exactly like single-font words.  The shaped structures
handed back to ReportLab are the ones it already understands
(``ShapedFragWord`` / ``ShapedStr`` / ``ShapeData``), so line breaking,
justification and drawing are untouched.

:func:`install` swaps the corrected function into ReportLab's paragraph engine.
It is idempotent and verifies that the ReportLab internals it relies on are
present, so an incompatible future ReportLab release is reported instead of
silently producing boxes again.

No text is rewritten, transliterated or rasterised here; the input stays
Unicode and only the glyph selection is corrected.
"""

from __future__ import annotations

import logging

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase import ttfonts as _ttfonts

try:  # uharfbuzz is a hard requirement of the fallback (requirements.txt)
    import uharfbuzz
except ImportError:  # pragma: no cover - exercised only on broken installs
    uharfbuzz = None

log = logging.getLogger(__name__)

# HarfBuzz already applies the script-specific Indic features (nukt, akhn,
# rphf, blwf, half, vatu, cjct, pres, abvs, blws, psts, haln) and the usual
# defaults (ccmp, locl, mark, mkmk, calt, clig, liga, kern) on its own.
# ReportLab additionally switches on *discretionary* ligatures (``dlig``),
# which is why "Test" rendered as "Teﬆ" with DejaVu Sans; word processors do
# not enable dlig by default, so neither does the fallback.
DEFAULT_FEATURES: dict[str, bool] = {"kern": True, "liga": True}

_REQUIRED_INTERNALS = ("ShapedFragWord", "makeShapedFragWord", "ShapedStr", "ShapeData")


class ShapingUnavailable(RuntimeError):
    """The environment cannot shape Devanagari for the ReportLab fallback."""


def internals_available() -> bool:
    """True when this ReportLab exposes the structures the fix builds on."""
    return uharfbuzz is not None and all(hasattr(_ttfonts, name) for name in _REQUIRED_INTERNALS)


def shape_frag_word(w, features: dict[str, bool] | None = None, force: bool = False):
    """Shape one ReportLab frag word, one HarfBuzz buffer per font run.

    ``w`` is ``[width, (frag, text), (frag, text), ...]`` exactly as ReportLab's
    paragraph engine builds it.  The return value has the same shape as the
    value ReportLab's own ``shapeFragWord`` returns: the untouched word when
    shaping changes nothing, otherwise a ``ShapedFragWord`` whose strings are
    ``ShapedStr`` instances carrying per-glyph ``ShapeData``.
    """
    if features is None:
        features = DEFAULT_FEATURES
    if isinstance(w, _ttfonts.ShapedFragWord):
        return w

    # Group consecutive fragments by font.  ``offset`` is the position of the
    # group inside the whole word, so cluster numbers stay word-relative (the
    # drawing code groups glyphs by cluster and specials are re-inserted by
    # cluster position).
    groups: list[tuple[str, list, int]] = []
    specials: dict[int, list] = {}
    text_len = 0
    for frag, chunk in w[1:]:
        if hasattr(frag, "cbDefn"):
            specials.setdefault(text_len, []).append(frag)
            continue
        if not chunk:
            # An empty fragment yields no glyphs; ReportLab drops it as well.
            continue
        if groups and groups[-1][0] == frag.fontName:
            groups[-1][1].append((frag, chunk))
        else:
            groups.append((frag.fontName, [(frag, chunk)], text_len))
        text_len += len(chunk)
    if not groups:
        return w

    new = _ttfonts.makeShapedFragWord(w)([])
    new_width = 0.0
    changed = False
    shaped = False

    for font_name, frags, offset in groups:
        per_char_frag = []
        text = ""
        for frag, chunk in frags:
            per_char_frag.extend(len(chunk) * [frag])
            text += chunk
        ttf = pdfmetrics.getFont(font_name)
        try:
            hb_font = ttf.hbFont(per_char_frag[0].fontSize)
        except AttributeError:
            # Not a shapable TrueType font (e.g. a Type1 standard font): leave
            # the whole word alone exactly as ReportLab would.
            return w

        buf = uharfbuzz.Buffer()
        buf.cluster_level = uharfbuzz.BufferClusterLevel.MONOTONE_CHARACTERS
        buf.add_str(text)
        buf.guess_segment_properties()
        uharfbuzz.shape(hb_font, buf, features)

        ntext = len(text)
        current_frag = None
        shaped_text = ""
        shape_data: list = []
        run_width = 0.0
        size_scale = 0.0
        for i, (info, pos) in enumerate(zip(buf.glyph_infos, buf.glyph_positions)):
            gid = info.codepoint
            cluster = info.cluster
            frag = per_char_frag[cluster]
            if current_frag is not frag:
                if current_frag is not None:
                    new.append((current_frag, _ttfonts.ShapedStr(shaped_text, shapeData=shape_data)))
                    new_width += run_width
                current_frag = frag
                shaped_text = ""
                shape_data = []
                run_width = 0.0
                size_scale = frag.fontSize / 1000.0
            # Glyph id -> the (possibly private-use) character ReportLab uses
            # to address that glyph inside *this* font's subset.
            try:
                uchar = ttf.face.glyphToChar[gid][0]
            except KeyError:
                uchar = ttf.hbAddPrivate(hb_font.glyph_to_string(gid), gid, pos.x_advance)
            uchar_width = ttf.face.charWidths[uchar]
            uchar = chr(uchar)
            shaped_text += uchar
            x_advance = ttf.pdfScale(pos.x_advance)
            x_offset = ttf.pdfScale(pos.x_offset)
            y_advance = ttf.pdfScale(pos.y_advance)
            y_offset = ttf.pdfScale(pos.y_offset)
            if x_advance:
                run_width += x_advance * size_scale
            shaped = shaped or (
                x_offset != 0
                or y_offset != 0
                or i >= ntext
                or (text[i] == uchar and x_advance != uchar_width)
            )
            changed = changed or shaped or i >= ntext or text[i] != uchar or force
            shape_data.append(
                _ttfonts.ShapeData(cluster + offset, x_advance, y_advance, x_offset, y_offset, uchar_width)
            )
        if current_frag is not None:
            new.append((current_frag, _ttfonts.ShapedStr(shaped_text, shapeData=shape_data)))
            new_width += run_width

    if not changed:
        return w
    if not shaped:
        # Same simplification ReportLab performs: plain tuples, no ShapedStr.
        new = new.__class__([tuple(item) for item in new])
    if specials:
        ordered = [((position, position), frags) for position, frags in specials.items()]
        for item in new:
            data = item[1].__shapeData__
            ordered.append(((data[0].cluster, data[-1].cluster), item))
        new[:] = [item for _key, item in sorted(ordered, key=lambda pair: pair[0])]
    new.insert(0, new_width)
    return new


def installed() -> bool:
    """True when ReportLab's paragraph engine currently uses the fixed shaper."""
    import reportlab.platypus.paragraph as paragraph_module

    return getattr(paragraph_module, "shapeFragWord", None) is shape_frag_word


def install() -> None:
    """Route ReportLab's paragraph shaping through :func:`shape_frag_word`.

    Raises :class:`ShapingUnavailable` when uharfbuzz is missing or the
    ReportLab internals changed; the fallback must not silently produce
    unshaped or box-riddled Hindi.
    """
    if installed():
        return
    if uharfbuzz is None:
        raise ShapingUnavailable(
            "uharfbuzz is not installed, so Devanagari cannot be shaped by the "
            "ReportLab fallback (pip install uharfbuzz)."
        )
    missing = [name for name in _REQUIRED_INTERNALS if not hasattr(_ttfonts, name)]
    if missing:
        raise ShapingUnavailable(
            "This ReportLab release no longer exposes "
            + ", ".join(missing)
            + "; the per-font shaping fix cannot be installed."
        )
    import reportlab.pdfgen.canvas as canvas_module
    import reportlab.platypus.paragraph as paragraph_module

    paragraph_module.shapeFragWord = shape_frag_word
    canvas_module.shapeFragWord = shape_frag_word
    log.debug("ReportLab per-font Devanagari shaping installed.")
