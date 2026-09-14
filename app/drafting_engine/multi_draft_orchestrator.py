from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
from .case_intake import extract_case_facts
from .conversation_state import CaseState, missing_fields, next_questions
from .gemini_client import GeminiClient
from .hybrid_retriever import HybridCorpusRetriever

TYPE_META = {
    "dava_plaint": ("dava", "dava_specialist.txt", "Dava / वाद पत्र"),
    "written_statement": ("written_statement", "written_statement_specialist.txt", "Written Statement / लिखित कथन"),
    "application": ("application", "application_specialist.txt", "Application / प्रार्थना पत्र"),
    "evidence_pw_affidavit": ("evidence_pw_affidavit", "evidence_specialist.txt", "Evidence / PW Affidavit"),
    "affidavit": ("affidavit", "affidavit_specialist.txt", "Affidavit / शपथपत्र"),
    "legal_notice": ("legal_notice", "legal_notice_specialist.txt", "Legal Notice / विधिक नोटिस"),
    "other_civil": ("other_civil", "other_civil_specialist.txt", "Other Civil Draft / अन्य सिविल ड्राफ्ट"),
}
ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT.parents[1] / "prompts"
SCHEMA = json.loads((ROOT / "draft_schema.json").read_text(encoding="utf-8"))

BASE_RULES = [
    "CASE_FACTS are the sole authority for case-specific facts.",
    "Retrieved advocate drafts are style/structure references only; never copy their factual details.",
    "Never invent names, dates, addresses, property identifiers, statutes, case law, ownership, title, possession, witnesses, valuation, court fee, limitation or jurisdiction.",
    "Preserve event status exactly: attempt is not completion; threat is not dispossession; allegation is not an established fact.",
    "Do not invent reliefs/demands/defences. Use only supported user-provided material.",
    "Never number paragraphs. The deterministic renderer assigns numbering.",
    "Return only JSON matching the supplied schema.",
]

class MultiDraftOrchestrator:
    def __init__(self):
        self.client = GeminiClient()
        self.retriever = HybridCorpusRetriever()

    def collect(self, state: CaseState, new_facts: dict[str, Any]):
        state.merge_facts(new_facts)
        missing = missing_fields(state.facts, state.document_type)
        if missing:
            state.status = "collecting"
            return {"status":"needs_information", "missing":missing, "questions":next_questions(state.facts,state.document_type)}
        state.status = "ready"
        return {"status":"ready", "facts":state.facts}

    def extract_and_collect(self, state: CaseState, text: str):
        return self.collect(state, extract_case_facts(text, current_facts=state.facts, document_type=state.document_type, user_history=state.user_messages()))

    def _retrieve(self, facts, document_type):
        pieces=[]
        for key in ("court_name","plaintiff_intro","defendant_intro","property_description","cause_of_action","jurisdiction_facts","purpose","sender","recipient","deponent"):
            if facts.get(key): pieces.append(str(facts[key]))
        for key in ("facts","reliefs","demands","defence_points","documents"):
            val=facts.get(key,[])
            pieces.extend(str(x) for x in (val if isinstance(val,list) else [val]))
        query=" ".join(pieces)
        corpus_type=TYPE_META[document_type][0]
        refs=self.retriever.search(query, document_type=corpus_type, top_k=int(os.getenv("CORPUS_TOP_K","8")))
        return self.retriever.format_context(refs)

    def draft_live(self, state: CaseState):
        if missing_fields(state.facts, state.document_type):
            raise ValueError("Required facts are still missing.")
        corpus_type, prompt_file, label = TYPE_META[state.document_type]
        style = (PROMPTS / prompt_file).read_text(encoding="utf-8")
        context = self._retrieve(state.facts, state.document_type)
        package = {
            "DOCUMENT_TYPE": label,
            "CASE_FACTS": state.facts,
            "ORIGINAL_ADVOCATE_DRAFTS": context,
            "SYSTEM_RULES": BASE_RULES,
            "OUTPUT_REQUIREMENTS": {
                "court_heading":"Court heading if supplied/applicable; otherwise empty.",
                "case_heading":"Party/case heading using only supplied facts.",
                "parties":"Party lines only; do not invent.",
                "title":"Correct document title.",
                "opening_averment":"Opening paragraph/statement appropriate to the document type.",
                "pleadings":"Ordered substantive paragraphs; no numbering.",
                "prayer":"Only supported reliefs/demands, empty when inappropriate.",
                "verification":"Only when appropriate and grounded in supplied facts.",
                "signature_block":"Use supplied signer/deponent information; advocate signature is presentation metadata if needed."
            }
        }
        system = style + "\n\nYou are now the FINAL DRAFTING ENGINE, not a planning-only assistant. Generate the actual filing-ready structured draft.\n" + "\n".join(f"- {x}" for x in BASE_RULES)
        for attempt in range(2):
            draft=self.client.generate_json(system=system, prompt=json.dumps(package,ensure_ascii=False,indent=2), schema=SCHEMA, thinking_level=os.getenv("GEMINI_DRAFTING_THINKING_LEVEL","high"))
            errors=self.validate(draft,state.facts,state.document_type)
            if not errors:
                state.draft_version += 1; state.status="drafted"; return draft
            package["SYSTEM_RULES"].append("Previous candidate failed validation: "+"; ".join(errors)+". Regenerate without changing approved facts.")
        raise RuntimeError("Generated draft failed deterministic validation: "+"; ".join(errors))

    def validate(self, draft, facts, document_type):
        errors=[]
        for key in ("title","pleadings","signature_block"):
            if not draft.get(key): errors.append(f"missing {key}")
        # Basic hallucination guard for obvious party names.
        source_names=[]
        for k in ("plaintiffs","defendants","parties","sender","recipient","deponent"):
            v=facts.get(k,[]); source_names.extend(v if isinstance(v,list) else [v])
        text=json.dumps(draft,ensure_ascii=False)
        if source_names and not any(str(n).strip() and str(n).strip() in text for n in source_names):
            errors.append("draft does not contain identifiable supplied party/deponent information")
        return errors
