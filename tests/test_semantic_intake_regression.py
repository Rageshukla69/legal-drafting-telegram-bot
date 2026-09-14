import app.drafting_engine.case_intake as ci
from app.drafting_engine.conversation_state import CaseState, missing_fields


class FakeGemini:
    calls = []

    def __init__(self):
        pass

    def generate_json(self, *, system, prompt, schema, thinking_level):
        FakeGemini.calls.append(system)
        if "final completeness auditor" in system:
            return {
                "facts": [
                    "दिनांक 10.09.2026 को प्रतिवादी ने वादी के शांतिपूर्ण कब्जे में हस्तक्षेप करने तथा भूमि पर कब्जा करने की धमकी दी।"
                ],
                "reliefs": [
                    "प्रतिवादी को स्थायी निषेधाज्ञा द्वारा वादी के कब्जे में हस्तक्षेप करने से रोका जाए।"
                ],
            }
        return {
            "court_name": "माननीय न्यायालय सिविल जज",
            "plaintiffs": ["रामकिशोर"],
            "defendants": ["सुरेश कुमार"],
        }


def test_arbitrary_narrative_is_recovered_when_first_pass_omits_facts_and_relief(monkeypatch):
    FakeGemini.calls = []
    monkeypatch.setattr(ci, "GeminiClient", FakeGemini)
    message = (
        "दिनांक 10.09.2026 को प्रतिवादी ने वादी के शांतिपूर्ण कब्जे में हस्तक्षेप करने तथा "
        "भूमि पर कब्जा करने की धमकी दी। अतः प्रतिवादी को स्थायी निषेधाज्ञा द्वारा वादी के "
        "कब्जे में हस्तक्षेप करने से रोका जाए।"
    )
    result = ci.extract_case_facts(message, document_type="dava_plaint", user_history=[message])
    assert result["facts"]
    assert result["reliefs"]
    assert result["plaintiffs"] == ["रामकिशोर"]
    assert result["defendants"] == ["सुरेश कुमार"]
    assert any("final completeness auditor" in x for x in FakeGemini.calls)


def test_labelled_blocks_are_only_a_deterministic_override(monkeypatch):
    monkeypatch.setattr(ci, "GeminiClient", FakeGemini)
    text = """न्यायालय: माननीय सिविल जज\nवादी: राम\nप्रतिवादी: श्याम\nमुख्य तथ्य: प्रतिवादी ने धमकी दी।\nराहत: स्थायी निषेधाज्ञा प्रदान की जाए।"""
    result = ci.extract_case_facts(text, document_type="dava_plaint", user_history=[text])
    assert result["court_name"].startswith("माननीय")
    assert result["plaintiffs"] == ["राम"]
    assert result["defendants"] == ["श्याम"]
    assert "धमकी" in result["facts"][0]
    assert "निषेधाज्ञा" in result["reliefs"][0]


def test_state_can_derive_generic_parties_from_semantic_roles():
    state = CaseState("x")
    state.merge_facts({"plaintiffs": ["राम"], "defendants": ["श्याम"]})
    assert state.facts["parties"] == ["राम", "श्याम"]
    assert "facts" in missing_fields(state.facts, "dava_plaint")
