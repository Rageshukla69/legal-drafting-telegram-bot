from app.drafting_engine.conversation_state import CaseState, STYLE_NAMES
from app.drafting_engine.draft_editor import DraftEditor
from app.drafting_engine.hybrid_retriever import HybridCorpusRetriever


def test_style_modes_retrieval():
    retriever = HybridCorpusRetriever()

    # 1. Purchased 500+ Legal Templates Mode
    tpl_ctx, tpl_name = retriever.retrieve_for_style(
        "स्थायी निषेधाज्ञा वाद पत्र", document_type="dava", style_mode="template_mode"
    )
    assert "SELECTED MASTER TEMPLATE (FROM PURCHASED 500+ LEGAL TEMPLATES)" in tpl_ctx
    assert tpl_name
    assert "b.d. shukla" not in tpl_name.lower()

    # 2. Dad's 306 Original Files Mode
    dad_ctx, dad_name = retriever.retrieve_for_style(
        "स्थायी निषेधाज्ञा वाद पत्र", document_type="dava", style_mode="dad_style"
    )
    assert "SELECTED ORIGINAL ADVOCATE DRAFT (FROM DAD'S 306 COURT FILES)" in dad_ctx
    assert dad_name
    assert "shukla" in dad_name.lower()


def test_add_and_remove_points():
    editor = DraftEditor()
    draft = {
        "title": "वाद पत्र बाबत स्थायी निषेधाज्ञा",
        "pleadings": [
            "यह कि वादी विवादित भूमि गाटा संख्या 412 का स्वामी है।",
            "यह कि प्रतिवादीगण ने दिनांक 12.10.2026 को अनाधिकार हस्तक्षेप किया।",
            "यह कि कारण वाद दिनांक 12.10.2026 को उत्पन्न हुआ।",
        ],
        "prayer": ["डिक्री स्थायी निषेधाज्ञा पारित की जावे।"],
        "signature_block": ["वादी", "द्वारा अधिवक्ता"],
    }

    # Test Add Point
    add_op = editor.parse_add_point(draft, "यह पॉइंट जोड़ दो कि प्रतिवादी ने 10,000 रुपये बयाना लिया था")
    updated_after_add, add_desc = editor.apply(draft, add_op)
    assert len(updated_after_add["pleadings"]) == 4
    assert any("10,000 रुपये बयाना" in p for p in updated_after_add["pleadings"])

    # Test Remove Point (deterministic paragraph number removal)
    rm_op = editor.parse_remove_point(updated_after_add, "पैराग्राफ 2 हटा दो")
    updated_after_rm, rm_desc = editor.apply(updated_after_add, rm_op)
    assert len(updated_after_rm["pleadings"]) == 3


def test_multi_document_preserves_style_and_facts():
    state = CaseState("tg-99-current", document_type="dava_plaint", style_mode="template_mode", style_selected=True)
    state.merge_facts({
        "court_name": "न्यायालय श्रीमान सिविल जज (प्रवर खंड), औरैया",
        "court_complex": "सिविल कोर्ट कम्पाउण्ड, बिधूना, औरैया",
        "advocate_name": "वी०डी० शुक्ला एडवोकेट",
        "plaintiffs": ["राम किशोर शुक्ल"],
        "defendants": ["दिनेश कुमार यादव"],
        "facts": ["गाटा संख्या 412 का विवाद"],
        "reliefs": ["स्थायी निषेधाज्ञा"],
    })
    state.selected_template_name = "01_Vaad_Patra_Sthayi_Nishedhagy_Permanent_Injunction.docx"
    state.set_draft({"title": "वाद पत्र", "pleadings": ["यह कि वादी स्वामी है।"], "signature_block": ["वादी"]})

    # Create additional document: Jawab Dava (Written Statement)
    state.begin_new_document("written_statement")
    assert state.style_mode == "template_mode"
    assert state.style_selected is True
    assert state.facts["advocate_name"] == "वी०डी० शुक्ला एडवोकेट"
    assert state.document_type == "written_statement"
    assert len(state.documents) == 1


if __name__ == "__main__":
    test_style_modes_retrieval()
    test_add_and_remove_points()
    test_multi_document_preserves_style_and_facts()
    print("test_style_selection_and_points passed successfully!")