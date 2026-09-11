"""Application layer for Dava intake, retrieval, drafting and validation."""
from __future__ import annotations
import json
from pathlib import Path
from .azure_client import draft_with_azure
from .azure_prompt_builder import build_messages
from .case_intake import extract_case_facts
from .conversation_state import CaseState, missing_fields
from .dava_composer import DavaComposer
from .question_generator import questions_for_state
from .retriever import DavaRetriever

ROOT = Path(__file__).resolve().parent

class DavaOrchestrator:
    def __init__(self):
        self.retriever = DavaRetriever(ROOT / "blueprints")
        self.composer = DavaComposer()

    def collect(self, state: CaseState, new_facts: dict) -> dict:
        state.merge_facts(new_facts)
        missing = missing_fields(state.facts)
        state.status = "collecting" if missing else "ready"
        if missing:
            return {"status": "needs_information", "missing": missing, "questions": questions_for_state(state.facts)}
        return {"status": "ready", "facts": state.facts}

    def extract_and_collect(self, state: CaseState, message: str) -> dict:
        extracted = extract_case_facts(message, state.facts)
        return self.collect(state, extracted)

    def prepare_azure_request(self, state: CaseState) -> dict:
        if missing_fields(state.facts):
            raise ValueError("Required facts are still missing.")
        plan = self.composer.plan(state.facts)
        fact_text = " ".join(map(str, state.facts.get("facts", [])))
        relief_text = " ".join(map(str, state.facts.get("reliefs", [])))
        query = " ".join([
            str(state.facts.get("court_name", "")),
            str(state.facts.get("property_description", "")),
            str(state.facts.get("cause_of_action", "")),
            fact_text,
            relief_text,
        ])
        refs = self.retriever.search(query, section="dava", top_k=10)
        context = self.retriever.format_context(refs)
        package = self.composer.build_prompt_package(state.facts, context, plan)
        return build_messages(package, ROOT / "draft_schema.json")

    def draft_live(self, state: CaseState) -> dict:
        request = self.prepare_azure_request(state)
        draft = draft_with_azure(json.loads(request["messages"][1]["content"]), request["json_schema"])
        # Renderer-level determinism is also reinforced here.
        draft["between_label"] = "बनाम"
        return draft
