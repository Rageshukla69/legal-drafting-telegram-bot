from app.drafting_engine.case_intake import _extract_labelled
from app.drafting_engine.conversation_state import CaseState, missing_fields

def test_labelled_parser_is_only_a_safety_net_and_handles_hindi_blocks():
    text="""न्यायालय: माननीय सिविल जज\nवादी: राम\nप्रतिवादी: श्याम\nमुख्य तथ्य: प्रतिवादी ने हस्तक्षेप की धमकी दी।\nराहत: स्थायी निषेधाज्ञा प्रदान की जाए।"""
    got=_extract_labelled(text)
    assert got['court_name'].startswith('माननीय')
    assert got['plaintiffs']==['राम']
    assert got['defendants']==['श्याम']
    assert 'हस्तक्षेप' in got['facts'][0]
    assert 'निषेधाज्ञा' in got['reliefs'][0]

def test_state_editor_fields_are_backward_compatible_and_versioned():
    state=CaseState('tg-1-current')
    assert state.draft=={}
    assert missing_fields(state.facts,'dava_plaint')
    draft={'title':'वाद पत्र','pleadings':['एक'], 'signature_block':['राम']}
    state.set_draft(draft)
    assert state.draft==draft
    assert state.draft_version==1
    assert len(state.draft_versions)==1

def test_user_messages_exclude_assistant_history():
    state=CaseState('x')
    state.record('assistant','कृपया राहत बताइए')
    state.record('user','प्रतिवादी ने धमकी दी')
    assert state.user_messages()==['प्रतिवादी ने धमकी दी']
