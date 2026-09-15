from app.drafting_engine.legal_number_formatter import format_legal_numbers, format_draft_numbers


def test_money_quantity_age_and_date_examples():
    text = "उम्र 80 वर्ष है और 7 डिसमिल भूमि के लिए 10,000 रुपये अदा किए। दिनांक 24.06.1969 है।"
    out = format_legal_numbers(text)
    assert "80 (अस्सी) वर्ष" in out
    assert "7 (सात) डिसमिल" in out
    assert "10,000 (दस हजार) रुपये" in out
    assert "24.06.1969 (चौबीस जून उन्नीस सौ उनहत्तर)" in out


def test_property_number_and_identifier_protection():
    text = "गाटा संख्या 526 है। मोबाइल 9876543210 है। केस AB-526/2026 और PIN 206001 है।"
    out = format_legal_numbers(text)
    assert "526 (पाँच सौ छब्बीस)" in out
    assert "9876543210" in out
    assert "AB-526/2026" in out
    assert "PIN 206001" in out


def test_idempotent_and_already_expanded():
    text = "80 (अस्सी) वर्ष और 10,000 (दस हजार) रुपये।"
    assert format_legal_numbers(text) == text
    assert format_legal_numbers(format_legal_numbers("7 डिसमिल")) == "7 (सात) डिसमिल"


def test_draft_copy_does_not_mutate_source():
    draft = {"title": "Dava", "pleadings": ["गाटा संख्या 526 में 7 डिसमिल भूमि है"]}
    out = format_draft_numbers(draft)
    assert draft["pleadings"][0] == "गाटा संख्या 526 में 7 डिसमिल भूमि है"
    assert out["pleadings"][0] == "गाटा संख्या 526 (पाँच सौ छब्बीस) में 7 (सात) डिसमिल भूमि है"
