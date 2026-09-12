from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient
from .gemini_prompts import PLANNER_SYSTEM
ROOT=Path(__file__).resolve().parent
PARTY_SECTIONS={'intro','party_identity','metadata'}
PARTY_PURPOSES={'वादी परिचय','प्रतिवादी परिचय','वादी का परिचय','प्रतिवादी का परिचय','plaintiff introduction','defendant introduction','party introduction'}
def plan_structure(facts:dict[str,Any],retrieval_context:str)->dict[str,Any]:
    result=GeminiClient().generate_json(system=PLANNER_SYSTEM,prompt=json.dumps({'CASE_FACTS':facts,'ORIGINAL_ADVOCATE_DRAFTS':retrieval_context,'TASK':'Create only the case-specific pleading architecture. Do not draft final paragraphs.'},ensure_ascii=False,indent=2),schema=json.loads((ROOT/'structure_schema.json').read_text(encoding='utf-8')),thinking_level=os.getenv('GEMINI_PLANNING_THINKING_LEVEL','medium'))
    return normalize_structure(result,facts)
def _standard_title(plan_title,nature,reliefs):
    title=plan_title.strip(); low=title.casefold(); rt=' '.join(map(str,reliefs or [])).casefold()
    if 'निषेधाज्ञा' in title or 'injunction' in low or 'injunction' in nature.casefold() or 'निषेधाज्ञा' in rt:return 'वादपत्र वास्ते स्थायी निषेधाज्ञा'
    return title if title not in {'','वादपत्र','दावा','दावापत्र'} else 'वादपत्र'
def normalize_structure(plan,facts):
    paragraphs=[]; seen=set()
    for raw in plan.get('paragraphs',[]):
        item=dict(raw); purpose=str(item.get('purpose','')).strip(); section=str(item.get('section','other')).strip()
        if section in PARTY_SECTIONS or purpose.casefold() in {x.casefold() for x in PARTY_PURPOSES} or not purpose:continue
        key=(section,purpose.casefold())
        if key in seen:continue
        seen.add(key); item['section']=section; item['purpose']=purpose; item['source_fields']=[str(x) for x in item.get('source_fields',[]) if str(x).strip()]; item['required']=bool(item.get('required',False)); paragraphs.append(item)
    present=lambda k: bool(facts.get(k) not in (None,'',[],{}))
    filtered=[]
    for item in paragraphs:
        s=item['section']
        if s=='jurisdiction' and not present('jurisdiction_facts'):continue
        if s=='limitation' and not present('limitation_facts'):continue
        if s=='valuation_court_fee' and not (present('valuation') or present('court_fee')):continue
        filtered.append(item)
    filtered.sort(key=lambda x:int(x.get('order',9999)))
    for i,item in enumerate(filtered,1):item['order']=i
    opening=str(plan.get('opening','')).strip() or 'वादी निम्नानुसार निवेदन करता है:-'
    return {'document_type':'dava_plaint','nature':str(plan.get('nature','civil plaint')).strip(),'title':_standard_title(str(plan.get('title','')),str(plan.get('nature','')),facts.get('reliefs')),'opening':opening,'paragraphs':filtered,'prayer_items':[str(x).strip() for x in plan.get('prayer_items',[]) if str(x).strip()],'include_verification':bool(plan.get('include_verification',False)),'warnings':[str(x).strip() for x in plan.get('warnings',[]) if str(x).strip()]}
