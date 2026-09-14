from types import SimpleNamespace

from app.drafting_engine.document_intelligence import _fallback_text


def test_fallback_text_from_pages():
    result = SimpleNamespace(
        pages=[
            SimpleNamespace(
                lines=[
                    SimpleNamespace(content="पहला पैराग्राफ"),
                    SimpleNamespace(content="दूसरी लाइन"),
                ]
            ),
            SimpleNamespace(lines=[SimpleNamespace(content="दूसरा पृष्ठ")]),
        ]
    )
    assert _fallback_text(result) == "पहला पैराग्राफ\nदूसरी लाइन\n\nदूसरा पृष्ठ"
