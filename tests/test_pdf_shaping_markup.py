from app.drafting_engine.renderers.legal_document_renderer import _register_pdf_fonts, _pdf_inline_font_markup


def test_devanagari_is_not_split_character_by_character():
    font, _bold, latin, symbols = _register_pdf_fonts()
    text = "न्यायालय श्रीमान सिविल जज जूनियर डिवीजन बिधूना"
    markup = _pdf_inline_font_markup(text, font, latin, symbols)
    assert markup == text
    assert markup.count('<font name="VakilLatin">') == 0


def test_latin_identifiers_use_latin_font_without_breaking_hindi():
    font, _bold, latin, symbols = _register_pdf_fonts()
    text = "गाटा सं. 1092 A/B/C/D"
    markup = _pdf_inline_font_markup(text, font, latin, symbols)
    assert '<font name="VakilLatin">1092' in markup
    assert 'A/B/C/D</font>' in markup
