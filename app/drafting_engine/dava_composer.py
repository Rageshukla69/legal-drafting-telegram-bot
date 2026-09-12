from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient
from .gemini_prompts import DRAFT_SYSTEM
ROOT=Path(__file__).resolve().parent
class DavaComposer:
    def __init__(self):
        self.client=GeminiClient(); self.schema=json.loads((ROOT/'draft_schema.json').read_text(encoding='utf-8'))
    def plan(self,facts):
        return {'document_type':'dava_plaint','style':'advocate_grade_continuous_numbered_pleading','numbering_rule':'Renderer assigns continuous Hindi-numbered paragraphs; model never numbers paragraphs.','required_facts_present':bool(facts)}
    def build_prompt_package(self,facts,retrieval_context,structure):
        return {'CASE_FACTS':facts,'APPROVED_STRUCTURE_PLAN':structure,'ORIGINAL_ADVOCATE_DRAFTS':retrieval_context,'SYSTEM_RULES':['CASE_FACTS are the sole factual authority.','Original drafts are style/structure references only.','Never copy factual details from an original draft.','Never invent legal authorities or procedural facts.','Renderer assigns paragraph numbering.','Preserve event status and chronology.']}
    def draft(self,package):
        return self.client.generate_json(system=DRAFT_SYSTEM,prompt=json.dumps(package,ensure_ascii=False,indent=2),schema=self.schema,thinking_level=os.getenv('GEMINI_DRAFTING_THINKING_LEVEL','high'))
