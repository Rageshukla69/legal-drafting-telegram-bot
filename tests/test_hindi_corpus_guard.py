from app.drafting_engine.hindi_corpus_guard import legacy_corruption_score, is_clean_hindi_reference


def test_legacy_artifact_is_detected():
    assert legacy_corruption_score("mपराsDत वादी निEनलिखित निवsदन करतs है") > 0.18


def test_clean_hindi_is_not_rejected():
    text = "प्रार्थनापत्र आधारहीन है और विधि विरूद्ध है।"
    assert legacy_corruption_score(text) < 0.08
    assert is_clean_hindi_reference(text)
