from app.drafting_engine.renderers.legal_document_renderer import _register_pdf_fonts, _pdf_inline_font_markup


def test_ascii_map_labels_use_latin_fallback():
    regular, _bold, latin, symbols = _register_pdf_fonts()
    markup = _pdf_inline_font_markup("नक्शा ABCD 526", regular, latin, symbols)
    assert '<font name="VakilLatin">ABCD</font>' in markup
