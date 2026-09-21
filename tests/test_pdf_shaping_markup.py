"""Font-run markup for the ReportLab fallback PDF.

The fallback mirrors the DOCX renderer's font routing: a character stays in
the Devanagari face whenever Noto Sans Devanagari covers it (this includes ASCII
digits and punctuation), Latin letters go to the Latin face, and a Hindi word is
never split character by character.
"""

from app.drafting_engine.renderers.legal_document_renderer import (
    _font_runs,
    _pdf_inline_font_markup,
    _register_pdf_fonts,
)


def test_devanagari_is_not_split_character_by_character():
    font, _bold, latin, symbols = _register_pdf_fonts()
    text = "न्यायालय श्रीमान सिविल जज जूनियर डिवीजन बिधूना"
    markup = _pdf_inline_font_markup(text, font, latin, symbols)
    assert markup == text
    assert markup.count('<font name="VakilLatin">') == 0


def test_numbering_and_prayer_labels_stay_in_the_devanagari_run():
    """``1 (एक) :`` and ``(क)`` are single runs — the words that rendered as boxes."""
    font, _bold, latin, symbols = _register_pdf_fonts()
    for text in ("1 (एक) : यह कि वादी", "(क) प्रतिवादी को रोका जाए।", "वादी: सीताराम पुत्र रामस्वरूप, निवासी"):
        assert _pdf_inline_font_markup(text, font, latin, symbols) == text
        assert _font_runs(text) == [("primary", text)]


def test_latin_identifiers_use_latin_font_without_breaking_hindi():
    font, _bold, latin, symbols = _register_pdf_fonts()
    text = "गाटा सं. 1092 A/B/C/D"
    markup = _pdf_inline_font_markup(text, font, latin, symbols)
    # Latin letters are routed to the Latin font; digits, "." and "/" are
    # covered by Noto Sans Devanagari and therefore stay in the primary run,
    # exactly as in the DOCX.
    assert markup.startswith("गाटा सं. 1092 ")
    assert '<font name="VakilLatin">A</font>/<font name="VakilLatin">B</font>' in markup
    assert [slot for slot, _ in _font_runs(text)][:3] == ["primary", "latin", "primary"]


def test_markup_escapes_xml_special_characters():
    font, _bold, latin, symbols = _register_pdf_fonts()
    markup = _pdf_inline_font_markup("धारा 5 & <नियम>", font, latin, symbols)
    assert "&amp;" in markup and "&lt;" in markup and "&gt;" in markup
