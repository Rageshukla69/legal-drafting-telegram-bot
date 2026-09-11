import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.drafting_engine.dava_composer import DavaComposer
from app.drafting_engine.dava_validator import validate_dava_draft
from app.drafting_engine.renderers.legal_document_renderer import render_both

facts = {
    "court_name": "न्यायालय श्रीमान सिविल जज (जूनियर डिवीजन), बिधूना, जनपद औरैया",
    "plaintiffs": ["राम कुमार पुत्र मोहन लाल, निवासी ग्राम किशनपुर, थाना बिधूना, जिला औरैया, उत्तर प्रदेश"],
    "defendants": ["श्याम कुमार पुत्र राजेंद्र सिंह, निवासी ग्राम किशनपुर, थाना बिधूना, जिला औरैया, उत्तर प्रदेश"],
    "plaintiff_intro": "राम कुमार पुत्र मोहन लाल, निवासी ग्राम किशनपुर, थाना बिधूना, जिला औरैया, उत्तर प्रदेश",
    "property_description": "ग्राम किशनपुर, परगना बिधूना, जिला औरैया स्थित गाटा संख्या 125, क्षेत्रफल 0.250 हेक्टेयर",
    "facts": [
        {"text": "यह कि वादी उक्त भूमि पर शांतिपूर्वक कब्जे में है और स्वयं खेती करता चला आ रहा है।"},
        {"text": "यह कि प्रतिवादी का उक्त भूमि में कोई स्वत्व, अधिकार या हिस्सा नहीं है।"},
        {"text": "यह कि दिनांक 10 अगस्त 2026 को प्रतिवादी ने वादी की भूमि पर जबरन कब्जा करने का प्रयास किया।"},
        {"text": "यह कि प्रतिवादी ने वादी को धमकी दी कि वह भूमि पर कब्जा कर लेगा।"},
    ],
    "cause_of_action": ["दिनांक 10 अगस्त 2026 को ग्राम किशनपुर, जिला औरैया में प्रतिवादी के हस्तक्षेप और धमकी से वाद का कारण उत्पन्न हुआ।"],
    "jurisdiction_facts": ["विवादित संपत्ति ग्राम किशनपुर, तहसील बिधूना, जनपद औरैया में स्थित है।"],
    "limitation_facts": [],
    "valuation": [],
    "court_fee": [],
    "reliefs": ["प्रतिवादी को वादी के शांतिपूर्ण कब्जे में हस्तक्षेप करने तथा भूमि पर जबरन कब्जा करने से स्थायी रूप से रोका जाए।"],
}

composer = DavaComposer()
plan = composer.plan(facts)
assert plan["style"] == "continuous_numbered_pleading"
assert "वादी का परिचय" not in " ".join(p["title"] for p in plan["parts"])

bad = {
    "title": "वादपत्र वास्ते स्थायी निषेधाज्ञा",
    "pleadings": ["यह कि प्रतिवादी ने भूमि पर अवैध कब्जा कर लिया है।"],
    "prayer": facts["reliefs"],
}
errors = validate_dava_draft(bad, facts)
assert "event_status_upgrade:attempted_to_completed" in errors

sample = {
    "court_heading": facts["court_name"],
    "case_heading": "मूल वाद संख्या ______ / 2026",
    "parties": facts["plaintiffs"] + facts["defendants"],
    "title": "वादपत्र वास्ते स्थायी निषेधाज्ञा",
    "opening_averment": "वादी निम्नलिखित निवेदन करता है:-",
    "pleadings": [x["text"] for x in facts["facts"]],
    "cause_of_action": facts["cause_of_action"],
    "jurisdiction": facts["jurisdiction_facts"],
    "limitation": [],
    "valuation_court_fee": [],
    "prayer": facts["reliefs"],
    "verification": "मैं, राम कुमार, यह सत्यापित करता हूँ कि वादपत्र में उल्लिखित तथ्य मेरी जानकारी के अनुसार सत्य एवं सही हैं।",
    "signature_block": ["स्थान : बिधूना", "दिनांक : __________", "वादी", "राम कुमार", "द्वारा अधिवक्ता-", "वी०डी० शुक्ला एडवोकेट", "बिधूना, औरैया"],
}
out = ROOT / "tests" / "output"
out.mkdir(exist_ok=True)
render_both(sample, out, base_name="phase7_smoke", paper="legal")
print("PHASE7_SMOKE_OK")
