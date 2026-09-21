from app.drafting_engine.renderers.legal_document_renderer import _font_runs, _pdf_inline_font_markup, _register_pdf_fonts


def test_ascii_map_labels_use_latin_fallback():
    regular, _bold, latin, symbols = _register_pdf_fonts()
    markup = _pdf_inline_font_markup("नक्शा ABCD 526", regular, latin, symbols)
    # Latin letters come from the Latin face; the digits are covered by the
    # Devanagari face and stay with it, matching the DOCX run split.
    assert markup == 'नक्शा <font name="VakilLatin">ABCD</font> 526'
    assert _font_runs("नक्शा ABCD 526") == [("primary", "नक्शा "), ("latin", "ABCD"), ("primary", " 526")]


def test_symbols_route_to_the_symbol_face_only_when_needed():
    regular, _bold, latin, symbols = _register_pdf_fonts()
    # ₹ and — are covered by Noto Sans Devanagari itself; § and ⚖ are not.
    assert _font_runs("₹10 — धारा") == [("primary", "₹10 — धारा")]
    # Spaces are covered by the Devanagari face too, so they stay primary and
    # only the uncovered symbols leave it (identical to the DOCX run split).
    assert _font_runs("धारा § ⚖") == [("primary", "धारा "), ("latin", "§"), ("primary", " "), ("latin", "⚖")]
