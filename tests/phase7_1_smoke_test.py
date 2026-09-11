import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.drafting_engine.dava_structure_planner import normalize_structure
from app.drafting_engine.dava_validator import validate_dava_draft
from app.drafting_engine.renderers.legal_document_renderer import render_both

facts = {
    "court_name": "न्यायालय श्रीमान सिविल जज (जूनियर डिवीजन), बिधूना, जनपद औरैया",
    "plaintiffs": ["सीताराम पुत्र रामस्वरूप, निवासी ग्राम नगला हरदास, थाना बिधूना, जनपद औरैया"],
    "defendants": ["सुरेश कुमार पुत्र हरिनारायण, निवासी ग्राम नगला हरदास, थाना बिधूना, जनपद औरैया"],
    "plaintiff_intro": "सीताराम पुत्र रामस्वरूप, निवासी ग्राम नगला हरदास, थाना बिधूना, जनपद औरैया",
    "property_description": "ग्राम नगला हरदास, परगना बिधूना, जनपद औरैया स्थित गाटा संख्या 214, रकबा 0.180 हेक्टेयर",
    "facts": [
        {"text": "सीताराम उक्त भूमि पर पिछले कई वर्षों से शांतिपूर्वक खेती करता चला आ रहा है।"},
        {"text": "सुरेश कुमार का उक्त भूमि में कोई हिस्सा या अधिकार नहीं है।"},
        {"text": "5 सितंबर 2026 को सुरेश कुमार अपने दो साथियों के साथ भूमि पर आया और जबरन कब्जा करने का प्रयास किया तथा मेड़ तोड़ने की धमकी दी।"},
        {"text": "अभी तक सुरेश कुमार ने भूमि पर कब्जा नहीं किया है और सीताराम का कब्जा बना हुआ है।"},
    ],
    "cause_of_action": ["5 सितंबर 2026 को ग्राम नगला हरदास, जनपद औरैया में कारण उत्पन्न हुआ।"],
    "jurisdiction_facts": ["विवादित भूमि बिधूना क्षेत्र में स्थित है।"],
    "reliefs": ["प्रतिवादी को वादी के शांतिपूर्ण कब्जे में हस्तक्षेप करने तथा भूमि पर जबरन कब्जा करने से स्थायी रूप से रोका जाए।"],
}

raw_plan = {
    "document_type": "dava_plaint",
    "nature": "स्थायी निषेधाज्ञा",
    "title": "वादपत्र वास्ते स्थायी निषेधाज्ञा",
    "opening": "वादी निम्नलिखित निवेदन करता है:-",
    "paragraphs": [
        {"order": 1, "section": "property", "purpose": "विवादित भूमि का विवरण", "source_fields": ["property_description"], "required": True},
        {"order": 2, "section": "right_or_possession", "purpose": "वादी का शांतिपूर्ण कब्जा", "source_fields": ["facts"], "required": True},
        {"order": 3, "section": "defendant", "purpose": "प्रतिवादी के अधिकार का अभाव", "source_fields": ["facts"], "required": True},
        {"order": 4, "section": "event", "purpose": "दिनांकित धमकी और हस्तक्षेप का प्रयास", "source_fields": ["facts"], "required": True},
        {"order": 5, "section": "apprehension", "purpose": "भविष्य के हस्तक्षेप की वास्तविक आशंका", "source_fields": ["facts"], "required": True},
        {"order": 6, "section": "cause_of_action", "purpose": "कारण-ए-दावा", "source_fields": ["cause_of_action"], "required": True},
        {"order": 7, "section": "jurisdiction", "purpose": "क्षेत्राधिकार के उपलब्ध तथ्य", "source_fields": ["jurisdiction_facts"], "required": True},
    ],
    "prayer_items": facts["reliefs"],
    "include_verification": True,
    "warnings": [],
}
plan = normalize_structure(raw_plan, facts)
assert len(plan["paragraphs"]) == 7
assert plan["paragraphs"][0]["order"] == 1

bad = {
    "title": "वादपत्र वास्ते स्थायी निषेधाज्ञा",
    "pleadings": ["1 (एक) : यह कि प्रतिवादी ने भूमि पर कब्जा कर लिया है।"],
    "prayer": facts["reliefs"],
}
errors = validate_dava_draft(bad, facts, plan)
assert "event_status_upgrade:attempted_to_completed" in errors
assert "model_numbered_paragraph:renderer_should_number" in errors

sample = {
    "court_heading": facts["court_name"],
    "case_heading": "मूल वाद संख्या ______ / 2026",
    "parties": [facts["plaintiffs"][0], facts["defendants"][0]],
    "title": plan["title"],
    "opening_averment": plan["opening"],
    "pleadings": [
        "यह कि विवादित भूमि ग्राम नगला हरदास, परगना बिधूना, जनपद औरैया स्थित गाटा संख्या 214, रकबा 0.180 हेक्टेयर है।",
        "यह कि वादी उक्त भूमि पर पिछले कई वर्षों से शांतिपूर्वक खेती करता चला आ रहा है।",
        "यह कि प्रतिवादी का उक्त भूमि में कोई हिस्सा या अधिकार नहीं है।",
        "यह कि दिनांक 5 सितंबर 2026 को प्रतिवादी अपने दो साथियों के साथ वादी की भूमि पर आया और जबरन कब्जा करने तथा मेड़ तोड़ने की धमकी दी।",
        "यह कि प्रतिवादी के आचरण से भविष्य में वादी के कब्जे में हस्तक्षेप किए जाने की वास्तविक आशंका है।",
        "यह कि वाद का कारण दिनांक 5 सितंबर 2026 को ग्राम नगला हरदास, जनपद औरैया में उत्पन्न हुआ।",
        "यह कि विवादित भूमि बिधूना क्षेत्र में स्थित है।",
    ],
    "prayer": facts["reliefs"],
    "verification": "मैं, सीताराम, सत्यापित करता हूँ कि उपरोक्त वादपत्र में उल्लिखित तथ्य मेरे ज्ञान और विश्वास के अनुसार सत्य एवं सही हैं।",
    "signature_block": ["स्थान : बिधूना", "दिनांक : __________", "वादी", "सीताराम", "द्वारा अधिवक्ता-", "वी०डी० शुक्ला एडवोकेट", "बिधूना, औरैया"],
}
errors = validate_dava_draft(sample, facts, plan)
assert not errors, errors
out = ROOT / "tests" / "output"
out.mkdir(exist_ok=True)
render_both(sample, out, base_name="phase7_1_smoke", paper="legal")
print("PHASE7_1_SMOKE_OK")
