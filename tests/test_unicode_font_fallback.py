from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.drafting_engine.renderers.legal_document_renderer import render_both, _register_pdf_fonts, _pdf_inline_font_markup
from reportlab.pdfbase import pdfmetrics


def test_pdf_fallback_covers_common_symbols_without_tofu():
    deva, bold, latin, symbols = _register_pdf_fonts()
    text = "विधिक नोटिस (Registered Legal Notice) § © • → ⚖ ✓ ⚠ ₹ —"
    markup = _pdf_inline_font_markup(text, deva, latin, symbols)
    assert "□" not in markup
    assert "Registered" in markup and "Legal" in markup and "Notice" in markup
    assert "⚖" in markup
    assert "✓" in markup
    assert "⚠" in markup


def test_render_both_with_mixed_unicode(tmp_path):
    draft = {
        "court_heading": "कार्यालय वी.डी. शुक्ला, एडवोकेट (जिला औरैया) ⚖",
        "case_heading": "रवि कुमार बनाम राजेश शर्मा — Civil Notice §",
        "parties": ["वादी: रवि कुमार", "प्रतिवादी: राजेश शर्मा"],
        "title": "विधिक नोटिस (Registered Legal Notice •)",
        "opening_averment": "यह नोटिस ₹10,00,000 की राशि के संबंध में है ©.",
        "pleadings": ["दिनांक 15.06.2025 को समझौता हुआ। → भुगतान किया गया। ✓"],
        "prayer": ["राशि वापस करने का निर्देश दिया जाए। ⚠"],
        "signature_block": ["रवि कुमार", "द्वारा अधिवक्ता-"],
    }
    docx, pdf = render_both(draft, tmp_path, base_name="unicode_fallback")
    assert docx.exists() and pdf.exists()
    assert pdf.stat().st_size > 1000
