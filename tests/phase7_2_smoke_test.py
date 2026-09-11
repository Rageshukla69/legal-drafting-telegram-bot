from pathlib import Path
import tempfile
import sys, types
stub = types.ModuleType("app.drafting_engine.azure_client")
stub._azure_client = lambda: None
sys.modules["app.drafting_engine.azure_client"] = stub
from app.drafting_engine.dava_structure_planner import normalize_structure
from app.drafting_engine.dava_validator import validate_dava_draft
from app.drafting_engine.renderers.legal_document_renderer import render_docx

facts = {
    "court_name": "न्यायालय श्रीमान सिविल जज (जूनियर डिवीजन), बिधूना",
    "plaintiffs": ["सीताराम पुत्र रामस्वरूप"],
    "defendants": ["सुरेश कुमार पुत्र हरिनारायण"],
    "facts": [
        "वादी गाटा संख्या 214, रकबा 0.180 हेक्टेयर भूमि पर कई वर्षों से खेती करता है।",
        "प्रतिवादी ने 5 सितंबर 2026 को जबरन कब्जा करने की धमकी दी।",
        "प्रतिवादी ने अभी कब्जा नहीं किया है।",
    ],
    "reliefs": ["स्थायी निषेधाज्ञा"],
    "property_description": "गाटा संख्या 214, रकबा 0.180 हेक्टेयर, ग्राम नगला हरदास",
    "cause_of_action": "5 सितंबर 2026 को ग्राम नगला हरदास में धमकी।",
    "jurisdiction_facts": "विवादित भूमि बिधूना क्षेत्र में स्थित है।",
    "advocate_profile": "वी०डी० शुक्ला एडवोकेट",
}
plan = normalize_structure({
    "document_type": "dava_plaint",
    "nature": "permanent injunction",
    "title": "स्थायी निषेधाज्ञा हेतु भूमि विवाद",
    "opening": "कुछ भी",
    "paragraphs": [
        {"order":1,"section":"intro","purpose":"वादी परिचय","source_fields":["plaintiffs"],"required":True},
        {"order":2,"section":"property","purpose":"विवादित भूमि का विवरण","source_fields":["property_description"],"required":True},
        {"order":3,"section":"right_or_possession","purpose":"वादी का कब्जा/कृषि","source_fields":["facts"],"required":True},
        {"order":4,"section":"event","purpose":"धमकी की घटना","source_fields":["facts"],"required":True},
        {"order":5,"section":"cause_of_action","purpose":"कारण-ए-दावा का उद्भव","source_fields":["cause_of_action"],"required":True},
        {"order":6,"section":"jurisdiction","purpose":"क्षेत्रीय क्षेत्राधिकार के तथ्य","source_fields":["jurisdiction_facts"],"required":True}
    ],
    "prayer_items":["प्रतिवादी को वादी के शांतिपूर्ण कब्जे में हस्तक्षेप करने तथा जबरन कब्जा करने से स्थायी रूप से रोका जाए।"],
    "include_verification":True,
    "warnings":[]
}, facts)
assert plan["title"] == "वादपत्र वास्ते स्थायी निषेधाज्ञा"
assert len(plan["paragraphs"]) == 5
assert plan["opening"] == "वादी निम्नानुसार निवेदन करता है:-"

draft = {
    "court_heading": facts["court_name"],
    "case_heading": "",
    "parties": ["सीताराम पुत्र रामस्वरूप ... वादी", "सुरेश कुमार पुत्र हरिनारायण ... प्रतिवादी"],
    "title": plan["title"],
    "opening_averment": plan["opening"],
    "pleadings": [
        "यह कि विवादित भूमि गाटा संख्या 214, रकबा 0.180 हेक्टेयर है।",
        "यह कि वादी उक्त भूमि पर कई वर्षों से खेती करता चला आ रहा है।",
        "यह कि दिनांक 5 सितंबर 2026 को प्रतिवादी ने जबरन कब्जा करने की धमकी दी।",
        "यह कि वाद का कारण उक्त घटना से उत्पन्न हुआ।",
        "यह कि विवादित भूमि बिधूना क्षेत्र में स्थित है।",
    ],
    "prayer": plan["prayer_items"],
    "verification": "मैं सीताराम सत्यापित करता हूँ कि उपरोक्त कथन मेरे ज्ञान और विश्वास के अनुसार सत्य हैं।",
    "signature_block": [],
}
assert validate_dava_draft(draft, facts, plan) == []
with tempfile.TemporaryDirectory() as td:
    out = Path(td)/"test.docx"
    render_docx(draft, out, paper="legal")
    assert out.exists() and out.stat().st_size > 0
print("PHASE7_2_SMOKE_OK")
