"""Application layer for Telegram text -> Dava drafting."""
from __future__ import annotations
from pathlib import Path
from .conversation_state import CaseState, missing_fields
from .question_generator import questions_for_state
from .retriever import DavaRetriever
from .dava_composer import DavaComposer
from .azure_prompt_builder import build_messages
from .azure_client import draft_with_azure

ROOT = Path(__file__).resolve().parent

class DavaOrchestrator:
    def __init__(self):
        self.retriever = DavaRetriever(ROOT / "blueprints")
        self.composer = DavaComposer()

    def collect(self, state: CaseState, new_facts: dict):
        state.merge_facts(new_facts)
        missing = missing_fields(state.facts)
        if missing:
            state.status = "collecting"
            return {
                "status": "needs_information",
                "missing": missing,
                "questions": questions_for_state(state.facts),
            }
        state.status = "ready"
        return {"status": "ready", "facts": state.facts}

    def prepare_azure_request(self, state: CaseState):
        if missing_fields(state.facts):
            raise ValueError("Required facts are still missing.")
        plan = self.composer.plan(state.facts)
        query_parts = [
            str(state.facts.get("court_name", "")),
            str(state.facts.get("plaintiff_intro", "")),
            str(state.facts.get("cause_of_action", "")),
            " ".join(x.get("text","") if isinstance(x,dict) else str(x) for x in state.facts.get("facts",[])),
            " ".join(map(str, state.facts.get("reliefs",[]))),
        ]
        refs = self.retriever.search(" ".join(query_parts), section="dava", top_k=10)
        context = self.retriever.format_context(refs)
        package = self.composer.build_prompt_package(state.facts, context, plan)
        return build_messages(package, ROOT / "draft_schema.json")

    def draft_live(self, state: CaseState):
        request = self.prepare_azure_request(state)
        return draft_with_azure(
            {"facts": state.facts, "plan": self.composer.plan(state.facts),
             "retrieved_reference_context": request["messages"][1]["content"]},
            request["json_schema"],
        )
