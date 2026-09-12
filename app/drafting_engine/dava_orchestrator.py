from __future__ import annotations
import os
from .case_intake import extract_case_facts
from .conversation_state import CaseState, missing_fields
from .dava_composer import DavaComposer
from .dava_structure_planner import plan_structure
from .dava_validator import validate_dava_draft
from .question_generator import questions_for_state
from .hybrid_retriever import HybridCorpusRetriever
class DavaOrchestrator:
    def __init__(self):
        self.retriever=HybridCorpusRetriever(); self.composer=DavaComposer()
    def collect(self,state,new_facts):
        state.merge_facts(new_facts); missing=missing_fields(state.facts)
        if missing:
            state.status='collecting'; return {'status':'needs_information','missing':missing,'questions':questions_for_state(state.facts)}
        state.status='ready'; return {'status':'ready','facts':state.facts}
    def extract_and_collect(self,state,text):
        return self.collect(state,extract_case_facts(text,current_facts=state.facts))
    def _retrieve(self,facts):
        fact_text=' '.join(item.get('text','') if isinstance(item,dict) else str(item) for item in facts.get('facts',[])); relief=' '.join(map(str,facts.get('reliefs',[])))
        query=' '.join([str(facts.get(k,'')) for k in ('court_name','plaintiff_intro','defendant_intro','property_description','cause_of_action','jurisdiction_facts')]+[fact_text,relief])
        refs=self.retriever.search(query,document_type='dava',top_k=int(os.getenv('CORPUS_TOP_K','8')))
        return self.retriever.format_context(refs)
    def draft_live(self,state):
        if missing_fields(state.facts):raise ValueError('Required facts are still missing.')
        context=self._retrieve(state.facts); structure=plan_structure(state.facts,context); package=self.composer.build_prompt_package(state.facts,context,structure); errors=[]
        for _ in range(2):
            draft=self.composer.draft(package); errors=validate_dava_draft(draft,state.facts,structure)
            if not errors:return draft
            package['SYSTEM_RULES'].append('Prior candidate failed deterministic validation: '+', '.join(errors)+'. Regenerate without changing the approved facts or structure.')
        raise RuntimeError('Generated Dava failed safety/structure validation: '+', '.join(errors))
