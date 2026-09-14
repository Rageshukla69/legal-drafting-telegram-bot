"""Semantic legal-case intake.

Labels such as `Plaintiff:` are treated as a convenience, not a requirement.
The extractor is deliberately trained through its system instructions and field
semantics to map ordinary Hindi/English legal narrative into the structured case
fields used by the seven draft types.
"""
from __future__ import annotations
import json, os, re
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "intake_schema.json"

TYPE_GUIDANCE = {
 "dava_plaint": "Plaint/वाद: identify court, plaintiff(s), defendant(s), property, material events, cause-of-action events/dates, and requested final/interim relief.",
 "written_statement": "Written statement/लिखित कथन: identify plaintiff(s), defendant(s), the defendant-side factual narrative, admissions/denials/objections/alternative pleas, documents and any counter-relief/defence.",
 "application": "Application/प्रार्थना पत्र: identify applicant/opposite parties, forum, factual background, purpose, and every relief/prayer, including interim relief.",
 "evidence_pw_affidavit": "PW affidavit/evidence: identify court, deponent/witness, party relationship if supplied, numbered factual testimony, documents/exhibits, dates and places.",
 "affidavit": "Affidavit/शपथपत्र: identify deponent, purpose, sworn factual statements, dates/place and verification material.",
 "legal_notice": "Legal notice/विधिक नोटिस: identify sender, recipient, factual chronology, breach/default, legal demand, compliance deadline if supplied, documents and date/place.",
 "other_civil": "Other civil draft: identify the natural parties, forum, factual narrative, purpose and requested outcome without forcing the text into an inappropriate category.",
}

SYSTEM = """You are the semantic intake engine for an Indian legal drafting system used by an advocate.

Your job is NOT to look for exact labels. Users will describe cases in completely different Hindi, English,
Hinglish, legal, colloquial, chronological, narrative, or mixed-language wording. Map the meaning of their
words into the structured fields in the supplied schema.

SEMANTIC MAPPING PRINCIPLES
1. Labels are optional. A paragraph saying 'the defendant came to the land, threatened the plaintiff and
   tried to interfere with peaceful possession' belongs in facts even if it never says 'Facts'.
2. Recognize ordinary legal synonyms and role words: वादी/दावेदार/claimant/plaintiff; प्रतिवादी/विपक्षी/
   opposite party/respondent/defendant; प्रार्थी/applicant/petitioner; नोटिसदाता/sender and नोटिसी/noticee/
   recipient; शपथकर्ता/deponent; विवादित संपत्ति/भूमि/मकान/गाटा/khasra/plot/property; प्रार्थना/अनुतोष/
   relief/prayer/मांगी गई राहत; वाद हेतुक/cause of action; प्रतिरक्षा/बचाव/denial/objection/defence.
   This list is illustrative, not exhaustive. Use semantic understanding beyond the examples.
3. Distinguish facts from outcomes. 'प्रतिवादी ने कब्जा करने की धमकी दी' is a factual allegation/event;
   'प्रतिवादी को कब्जा करने से रोका जाए' is relief. A threat is not dispossession; an attempt is not completion.
4. Identify cause-of-action events from the chronology and legally operative events, even when the user never
   uses the phrase 'cause of action'. Include dates only when explicitly stated.
5. Identify relief from imperative/request language such as 'अतः प्रार्थना है', 'निवेदन है', 'राहत दी जाए',
   'रोकने का आदेश', 'घोषित किया जाए', 'कब्जा दिलाया जाए', 'राशि दिलाई जाए', 'costs awarded', etc.
6. For written statements, identify denials, admissions, objections, alternative pleas and defence facts as
   defence_points when the meaning supports that classification.
7. Preserve material wording. Prefer short verbatim or near-verbatim source snippets over paraphrase when
   extracting facts. Do not silently normalize names, dates, addresses, survey numbers, amounts or legal status.
8. Never invent a missing field. Never infer ownership, title, possession, relationship, statute, limitation,
   valuation, court fee, jurisdiction, document, witness, or relief merely because it would be typical.
9. The current case facts are already accepted unless the newest user text explicitly corrects them. If the
   newest message corrects a value, output the corrected value.
10. Review the entire supplied user-message history. Information can be split across messages. Do not require
    the latest message to repeat earlier facts.
11. Ignore assistant questions/prompts in the history; only user-authored messages are evidence.
12. Return only JSON matching the schema. Empty arrays/strings are preferred to guessing.
"""

LABELS = {
    "court_name": [r"Court", r"न्यायालय", r"अदालत"],
    "plaintiff_intro": [r"Plaintiff", r"वादी", r"दावेदार"],
    "defendant_intro": [r"Defendant", r"प्रतिवादी", r"विपक्षी"],
    "property_description": [r"Property", r"संपत्ति", r"विवादित भूमि", r"भूमि"],
    "facts": [r"Main Facts", r"Facts", r"मुख्य तथ्य", r"तथ्य"],
    "cause_of_action": [r"Cause of action", r"वाद हेतुक", r"वाद हेतु"],
    "reliefs": [r"Relief", r"Reliefs", r"राहत", r"प्रार्थना", r"अनुतोष"],
    "demands": [r"Demand", r"Demands", r"मांग", r"अनुपालन"],
    "defence_points": [r"Defence", r"प्रतिरक्षा", r"बचाव", r"आपत्ति"],
    "purpose": [r"Purpose", r"उद्देश्य"],
    "sender": [r"Sender", r"प्रेषक", r"नोटिसदाता"],
    "recipient": [r"Recipient", r"प्राप्तकर्ता", r"नोटिसी"],
    "deponent": [r"Deponent", r"शपथकर्ता"],
}

def _extract_labelled(message: str) -> dict[str, Any]:
    """Safety net for explicit Label: blocks; semantic extraction is primary."""
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    variants=[]; canonical={}
    for key, vals in LABELS.items():
        for v in vals:
            variants.append(v); canonical[v.casefold()] = key
    alt="|".join(sorted((re.escape(v) for v in variants), key=len, reverse=True))
    pattern=re.compile(rf"(?im)(?<!\w)(?P<label>{alt})\s*:\s*(?P<value>.*?)(?=\n\s*(?:{alt})\s*:|\Z)",re.S)
    out={}
    for m in pattern.finditer(text):
        key=canonical.get(m.group('label').strip().casefold()); value=m.group('value').strip()
        if not key or not value: continue
        if key in {"facts","reliefs","demands","defence_points"}:
            vals=[re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", x).strip() for x in value.splitlines()]
            out[key]=[x for x in vals if x] or [value]
        else: out[key]=value
    if out.get('plaintiff_intro'): out['plaintiffs']=[out['plaintiff_intro']]
    if out.get('defendant_intro'): out['defendants']=[out['defendant_intro']]
    return out

def _merge_model_and_labelled(model: dict[str,Any], labelled: dict[str,Any]) -> dict[str,Any]:
    merged=dict(model or {})
    # Explicit labelled content wins only for the same field; model remains responsible for unlabeled text.
    for k,v in labelled.items(): merged[k]=v
    if merged.get('plaintiff_intro') and not merged.get('plaintiffs'):
        merged['plaintiffs']=[merged['plaintiff_intro']]
    if merged.get('defendant_intro') and not merged.get('defendants'):
        merged['defendants']=[merged['defendant_intro']]
    return merged

def extract_case_facts(message: str, current_facts: dict[str, Any] | None = None,
                       document_type: str = "dava_plaint", user_history: list[str] | None = None) -> dict[str, Any]:
    schema=json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    history=[str(x).strip() for x in (user_history or []) if str(x).strip()]
    if not history or history[-1] != message.strip(): history.append(message.strip())
    # Large OCR chunks are already complete evidence units. Do not resend many
    # previous OCR chunks to Gemini, which would defeat the chunking safeguard.
    # For ordinary chat, retain recent user history so facts can be split across
    # messages and still be understood semantically.
    if len(message) > 12000:
        history=[message.strip()]
    else:
        history=history[-12:]
    payload={
      "document_type": document_type,
      "document_type_guidance": TYPE_GUIDANCE.get(document_type, TYPE_GUIDANCE['other_civil']),
      "current_accepted_case_facts": current_facts or {},
      "user_messages_in_order": history,
      "newest_user_message": message,
      "task":"Extract any newly supported facts and corrections from the user messages, mapping by meaning rather than labels. Do not output assistant questions.",
    }
    model_result={}
    try:
        model_result=GeminiClient().generate_json(
            system=SYSTEM,
            prompt=json.dumps(payload,ensure_ascii=False,indent=2),
            schema=schema,
            thinking_level=os.getenv('GEMINI_INTAKE_THINKING_LEVEL','medium'),
        ) or {}
    except Exception:
        model_result={}
    labelled=_extract_labelled(message)
    return _merge_model_and_labelled(model_result,labelled)
