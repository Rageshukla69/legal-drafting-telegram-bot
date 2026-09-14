from app.bot import detect_additional_document_type
from app.drafting_engine.conversation_state import CaseState


def test_natural_language_additional_document_detection():
    assert detect_additional_document_type("इस दावे का affidavit बना दो") == "affidavit"
    assert detect_additional_document_type("इस केस के लिए legal notice तैयार कर दो") == "legal_notice"
    assert detect_additional_document_type("अब इसी मामले का written statement create करो") == "written_statement"
    assert detect_additional_document_type("प्रार्थना में यह लाइन बदल दो") is None


def test_case_can_hold_multiple_documents_without_losing_facts():
    state = CaseState("tg-1-current", document_type="dava_plaint")
    state.merge_facts({
        "court_name": "सिविल जज",
        "plaintiffs": ["रवि कुमार"],
        "defendants": ["राजेश शर्मा"],
        "facts": ["मूल वाद के तथ्य"],
        "reliefs": ["10,00,000 रुपये की वसूली"],
    })
    state.set_draft({"title": "वाद पत्र", "pleadings": ["मूल वाद"], "signature_block": ["वादी"]})
    dava_id = state.active_document_id

    state.begin_new_document("affidavit")
    assert state.facts["plaintiffs"] == ["रवि कुमार"]
    assert state.draft == {}
    assert state.document_type == "affidavit"

    state.facts["deponent"] = "रवि कुमार"
    state.facts["purpose"] = "वाद के समर्थन में"
    state.set_draft({"title": "शपथपत्र", "pleadings": ["शपथपूर्वक कथन"], "signature_block": ["शपथकर्ता"]})
    state.archive_active_document()

    assert len(state.documents) == 2
    assert state.documents[0]["document_id"] == dava_id
    assert state.documents[0]["document_type"] == "dava_plaint"
    assert state.documents[1]["document_type"] == "affidavit"
    assert state.documents[0]["draft"]["title"] == "वाद पत्र"
    assert state.documents[1]["draft"]["title"] == "शपथपत्र"
